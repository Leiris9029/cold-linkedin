"""
Main Orchestrator - end-to-end cold email campaign automation.

Usage:
    # Full pipeline: research → generate → review → send
    python main.py run --csv data/260202japan.csv --product 1

    # Send only (CSV already has subject/body columns)
    python main.py send --csv output/coldmails_mailmerge.csv

    # Check followups for a campaign
    python main.py followup --campaign-id 1

    # Start webhook server
    python main.py webhook

    # Check campaign status
    python main.py status --campaign-id 1
"""
import argparse
import csv
import sys
import logging
from datetime import datetime
from pathlib import Path

import db
from config import OUTPUT_DIR
from claude_client import ClaudeClient
from gmass_client import GMassClient
from sheets_client import SheetsClient
from scheduler import Scheduler, run_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ── Phase 1: Content Generation ─────────────────────────

def phase1_generate(csv_path: str, product_number: int = 1) -> str:
    """
    Generate cold emails using Claude API.

    Input: raw CSV with contacts
    Output: mailmerge CSV (to_name, to_email, company, subject, body)
    """
    logger.info(f"Phase 1: Generating cold emails from {csv_path}")
    claude = ClaudeClient()

    # Read CSV content
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        csv_content = f.read()

    # Generate emails via Claude (uses /coldmail skill)
    result = claude.generate_coldmail(
        csv_content=csv_content,
        product_number=product_number,
    )

    # Save raw output
    today = datetime.now().strftime("%Y%m%d")
    output_path = OUTPUT_DIR / f"coldmails_{today}.md"
    output_path.write_text(result, encoding="utf-8")
    logger.info(f"Generated emails saved to {output_path}")

    return str(output_path)


def phase1_review(email_content_path: str, auto_fix: bool = True) -> str:
    """
    Review generated emails using Claude API (/review skill).
    """
    logger.info(f"Phase 1: Reviewing emails from {email_content_path}")
    claude = ClaudeClient()

    content = Path(email_content_path).read_text(encoding="utf-8")
    result = claude.review(content, auto_fix=auto_fix)

    # Save review report
    today = datetime.now().strftime("%Y%m%d")
    report_path = OUTPUT_DIR / f"review_{today}.md"
    report_path.write_text(result, encoding="utf-8")
    logger.info(f"Review report saved to {report_path}")

    return result


# ── Phase 2: Send via GMass ─────────────────────────────

def phase2_send(csv_path: str, campaign_name: str | None = None) -> int:
    """
    Upload CSV to Google Sheets → Create GMass list → Create draft → Send.

    Input: mailmerge CSV (to_name, to_email, company, subject, body)
    Output: campaign_id in local DB
    """
    if not campaign_name:
        today = datetime.now().strftime("%Y%m%d")
        campaign_name = f"ColdMail_{today}"

    logger.info(f"Phase 2: Sending campaign '{campaign_name}' from {csv_path}")

    # 1. Create campaign in local DB
    campaign_id = db.create_campaign(name=campaign_name, csv_path=csv_path)

    # 2. Load recipients into DB
    _load_recipients_to_db(csv_path, campaign_id)

    # 3. Upload to Google Sheets
    sheets = SheetsClient()
    spreadsheet_id, worksheet_id = sheets.upload_mailmerge_csv(csv_path, campaign_name)
    db.update_campaign(campaign_id, spreadsheet_id=spreadsheet_id, worksheet_id=worksheet_id)
    logger.info(f"Uploaded to Google Sheets: {spreadsheet_id}")

    # 4. Create GMass list
    gmass = GMassClient()
    list_result = gmass.create_list(spreadsheet_id, worksheet_id)
    list_address = list_result.get("listAddress", "")
    db.update_campaign(campaign_id, gmass_list_id=list_address)
    logger.info(f"GMass list created: {list_address}")

    # 5. Create GMass campaign draft
    # For mail merge, we use {subject} and {body} as merge fields
    draft_result = gmass.create_draft(
        list_address=list_address,
        subject="{subject}",
        message="{body}",
    )
    draft_id = draft_result.get("campaignDraftId", "")
    db.update_campaign(campaign_id, gmass_draft_id=draft_id)
    logger.info(f"GMass draft created: {draft_id}")

    # 6. Send campaign
    send_result = gmass.send_campaign(draft_id)
    gmass_campaign_id = send_result.get("campaignId", "")
    db.update_campaign(
        campaign_id,
        gmass_campaign_id=gmass_campaign_id,
        status="sent",
        sent_at=datetime.now().isoformat(),
    )
    logger.info(f"Campaign sent! GMass campaign ID: {gmass_campaign_id}")

    return campaign_id


def _load_recipients_to_db(csv_path: str, campaign_id: int):
    """Load CSV recipients into the database."""
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            db.add_recipient(
                campaign_id=campaign_id,
                email=row.get("to_email", row.get("email", "")),
                name=row.get("to_name", row.get("contact_name", "")),
                company=row.get("company", ""),
                language=row.get("language", "ja"),
                subject=row.get("subject", ""),
                body=row.get("body", ""),
            )


# ── Phase 3-4: Followup & A/B Test ─────────────────────

def phase3_followup(campaign_id: int):
    """Check and send followups for a campaign."""
    logger.info(f"Phase 3: Checking followups for campaign {campaign_id}")
    no_opens = run_scheduler(campaign_id)

    if no_opens:
        logger.info(f"Found {len(no_opens)} no-open recipients for potential A/B re-send")
        # A/B test logic could be triggered here
        # For now, log them for manual review
        for r in no_opens:
            logger.info(f"  No open: {r['name']} ({r['company']}) - {r['email']}")


# ── Status Check ────────────────────────────────────────

def show_status(campaign_id: int):
    """Display campaign status summary."""
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        print(f"Campaign {campaign_id} not found.")
        return

    recipients = db.get_recipients(campaign_id)
    status_counts = {}
    for r in recipients:
        s = r["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    print(f"\n{'='*50}")
    print(f"Campaign: {campaign['name']}")
    print(f"Status: {campaign['status']}")
    print(f"Sent at: {campaign.get('sent_at', 'N/A')}")
    print(f"Total recipients: {len(recipients)}")
    print(f"{'='*50}")
    for status, count in sorted(status_counts.items()):
        bar = "#" * count
        print(f"  {status:12s} | {count:3d} | {bar}")
    print(f"{'='*50}\n")


# ── Full Pipeline ────────────────────────────────────────

def full_pipeline(csv_path: str, product_number: int = 1, campaign_name: str | None = None):
    """
    Run the complete pipeline:
    1. Generate cold emails (Claude API + /coldmail skill)
    2. Review & auto-fix (Claude API + /review skill)
    3. Send via GMass API
    """
    logger.info("Starting full pipeline...")

    # Phase 1a: Generate
    md_path = phase1_generate(csv_path, product_number)

    # Phase 1b: Review
    # NOTE: In practice, you'd parse the .md output into a CSV first.
    # For now, review the raw output.
    phase1_review(md_path, auto_fix=True)

    # Phase 2: Send
    # NOTE: This assumes a mailmerge CSV exists.
    # In a full implementation, phase1 would produce the CSV.
    mailmerge_csv = OUTPUT_DIR / "coldmails_mailmerge.csv"
    if mailmerge_csv.exists():
        campaign_id = phase2_send(str(mailmerge_csv), campaign_name)
        logger.info(f"Pipeline complete. Campaign ID: {campaign_id}")
        show_status(campaign_id)
    else:
        logger.warning(
            f"Mailmerge CSV not found at {mailmerge_csv}. "
            "Generate it first or provide a ready CSV."
        )


# ── CLI ──────────────────────────────────────────────────

def main():
    db.init_db()

    parser = argparse.ArgumentParser(description="Cold Email Campaign Orchestrator")
    subparsers = parser.add_subparsers(dest="command")

    # run: full pipeline
    p_run = subparsers.add_parser("run", help="Full pipeline: generate → review → send")
    p_run.add_argument("--csv", required=True, help="Input CSV path")
    p_run.add_argument("--product", type=int, default=1, help="Product number (default: 1)")
    p_run.add_argument("--name", help="Campaign name")

    # send: send only
    p_send = subparsers.add_parser("send", help="Send a ready mailmerge CSV")
    p_send.add_argument("--csv", required=True, help="Mailmerge CSV path")
    p_send.add_argument("--name", help="Campaign name")

    # followup: check & send followups
    p_followup = subparsers.add_parser("followup", help="Check and send followups")
    p_followup.add_argument("--campaign-id", type=int, required=True)

    # status: show campaign status
    p_status = subparsers.add_parser("status", help="Show campaign status")
    p_status.add_argument("--campaign-id", type=int, required=True)

    # webhook: start webhook server
    subparsers.add_parser("webhook", help="Start webhook server")

    args = parser.parse_args()

    if args.command == "run":
        full_pipeline(args.csv, args.product, args.name)
    elif args.command == "send":
        phase2_send(args.csv, args.name)
    elif args.command == "followup":
        phase3_followup(args.campaign_id)
    elif args.command == "status":
        show_status(args.campaign_id)
    elif args.command == "webhook":
        from webhook_server import app
        from config import WEBHOOK_HOST, WEBHOOK_PORT
        app.run(host=WEBHOOK_HOST, port=WEBHOOK_PORT, debug=True)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
