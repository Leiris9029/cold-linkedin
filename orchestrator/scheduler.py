"""
Scheduler - checks for due followups and triggers generation + sending.

Runs periodically (via cron, Windows Task Scheduler, or within main.py loop).
"""
import logging
from datetime import datetime, timedelta
from config import FOLLOWUP_SCHEDULE, MAX_FOLLOWUP_STAGES, NO_OPEN_THRESHOLD_DAYS
import db
from claude_client import ClaudeClient
from gmass_client import GMassClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self):
        self.claude = ClaudeClient()
        self.gmass = GMassClient()

    def check_and_send_followups(self, campaign_id: int):
        """
        Check all recipients in a campaign and generate/send followups as needed.

        Logic:
        - For each followup stage (1, 2, 3):
          - Find recipients who haven't replied/bounced and are past the threshold
          - Generate followup email via Claude
          - Send via GMass
        """
        campaign = db.get_campaign(campaign_id)
        if not campaign:
            logger.error(f"Campaign {campaign_id} not found")
            return

        for stage in range(1, MAX_FOLLOWUP_STAGES + 1):
            days = FOLLOWUP_SCHEDULE.get(stage, 5)
            recipients = db.get_recipients_needing_followup(campaign_id, stage, days)

            if not recipients:
                continue

            logger.info(
                f"Campaign {campaign_id}: {len(recipients)} recipients need stage-{stage} followup"
            )

            for recipient in recipients:
                try:
                    self._process_followup(recipient, campaign, stage)
                except Exception as e:
                    logger.error(
                        f"Failed to process followup for {recipient['email']}: {e}"
                    )

    def _process_followup(self, recipient: dict, campaign: dict, stage: int):
        """Generate and send a single followup email."""
        # Determine response status for tone adjustment
        if recipient["status"] == "opened":
            response_status = "opened"
        else:
            response_status = "no_response"

        # Generate followup via Claude
        logger.info(
            f"Generating stage-{stage} followup for {recipient['name']} ({recipient['company']})"
        )
        followup_content = self.claude.generate_followup(
            original_email=recipient["body"],
            company=recipient["company"],
            contact_name=recipient["name"],
            stage=stage,
            language=recipient.get("language", "ja"),
            response_status=response_status,
        )

        # Parse subject and body from Claude's response
        subject, body = self._parse_email_output(followup_content)

        if not subject or not body:
            logger.warning(f"Could not parse followup for {recipient['email']}, skipping")
            return

        # Send via GMass transactional
        result = self.gmass.send_single(
            to=recipient["email"],
            subject=subject,
            body=body,
        )
        logger.info(f"Followup sent to {recipient['email']}: {result}")

        # Update database
        db.update_recipient(recipient["id"], followup_stage=stage)
        db.schedule_followup(
            recipient_id=recipient["id"],
            campaign_id=campaign["id"],
            stage=stage,
            subject=subject,
            body=body,
            scheduled_at=datetime.now().isoformat(),
        )

    def check_no_opens_for_abtest(self, campaign_id: int) -> list[dict]:
        """
        Find recipients who haven't opened the email after threshold days.
        These are candidates for A/B test re-send with a different subject.

        Returns list of recipients who need A/B re-send.
        """
        recipients = db.get_recipients_needing_followup(
            campaign_id, stage=1, days_since=NO_OPEN_THRESHOLD_DAYS
        )
        # Filter to only those who haven't opened at all
        no_opens = [r for r in recipients if r["status"] == "sent"]
        if no_opens:
            logger.info(
                f"Campaign {campaign_id}: {len(no_opens)} recipients with no opens "
                f"(>{NO_OPEN_THRESHOLD_DAYS} days)"
            )
        return no_opens

    def _parse_email_output(self, claude_output: str) -> tuple[str, str]:
        """
        Parse Claude's email output to extract subject and body.
        Handles both structured (제목: / 본문:) and freeform output.
        """
        subject = ""
        body = ""
        lines = claude_output.split("\n")

        in_body = False
        body_lines = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("제목:") or stripped.startswith("Subject:"):
                subject = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("본문:") or stripped.startswith("Body:"):
                in_body = True
            elif in_body or (subject and not stripped.startswith(("━", "📧", "📌", "💡", "⏰", "🏷"))):
                if stripped.startswith(("━", "💡", "⏰")):
                    in_body = False
                    continue
                body_lines.append(line)
                in_body = True

        body = "\n".join(body_lines).strip()
        return subject, body


def run_scheduler(campaign_id: int):
    """Run a single scheduler check."""
    scheduler = Scheduler()
    scheduler.check_and_send_followups(campaign_id)
    no_opens = scheduler.check_no_opens_for_abtest(campaign_id)
    return no_opens
