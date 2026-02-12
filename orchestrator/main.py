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
from config import OUTPUT_DIR, HUNTER_API_KEY
from apollo_client import ApolloClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ── Phase 1: Content Generation ─────────────────────────

def phase1_generate(csv_path: str, product_number: int = 1) -> str:
    """
    Generate cold emails using Claude API.

    Input: raw CSV with contacts
    Output: mailmerge CSV path (contact_name, email, company, subject, body)
    """
    logger.info(f"Phase 1: Generating cold emails from {csv_path}")
    from claude_client import ClaudeClient
    claude = ClaudeClient()

    # Read CSV content
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        csv_content = f.read()

    # Generate emails via Claude (uses /coldmail skill)
    result = claude.generate_coldmail(
        csv_content=csv_content,
        product_number=product_number,
    )

    today = datetime.now().strftime("%y%m%d")

    # Save raw output (MD)
    md_path = OUTPUT_DIR / f"coldmails_{today}.md"
    md_path.write_text(result, encoding="utf-8")
    logger.info(f"Generated emails saved to {md_path}")

    # Extract CSV block from Claude output
    csv_path_out = OUTPUT_DIR / f"{today}final.csv"
    csv_block = _extract_csv_block(result)
    if csv_block:
        csv_path_out.write_text(csv_block, encoding="utf-8-sig")
        logger.info(f"Mailmerge CSV extracted to {csv_path_out}")
    else:
        logger.warning("Could not extract CSV block from Claude output. Manual CSV creation needed.")

    return str(md_path)


def _extract_csv_block(text: str) -> str | None:
    """Extract the CSV block from Claude's markdown output."""
    import re
    # Look for ```csv ... ``` block
    pattern = r"```csv\s*\n(.*?)```"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        csv_text = match.group(1).strip()
        # Validate it has a header row
        if csv_text.startswith("contact_name,"):
            return csv_text
    return None


def phase1_review(email_content_path: str, auto_fix: bool = True) -> str:
    """
    Review generated emails using Claude API (/review skill).
    """
    logger.info(f"Phase 1: Reviewing emails from {email_content_path}")
    from claude_client import ClaudeClient
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
    Also tracks recipients in local DB for status monitoring.

    Input: mailmerge CSV (contact_name, email, company, subject, body)
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

    # 3. Upload to Google Sheets (shared spreadsheet, new worksheet tab)
    from sheets_client import SheetsClient
    sheets = SheetsClient()
    spreadsheet_id, worksheet_id = sheets.upload_mailmerge_csv(csv_path, campaign_name)
    db.update_campaign(campaign_id, spreadsheet_id=spreadsheet_id, worksheet_id=worksheet_id)
    logger.info(f"Uploaded to Google Sheets: worksheet '{campaign_name}'")

    # 4. Create GMass list from the sheet
    from gmass_client import GMassClient
    gmass = GMassClient()
    list_result = gmass.create_list(spreadsheet_id, worksheet_id)
    list_address = list_result.get("listAddress", "")
    db.update_campaign(campaign_id, gmass_list_id=list_address)
    logger.info(f"GMass list created: {list_address}")

    # 5. Create GMass campaign draft (mail merge with {subject} and {body})
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
    from scheduler import run_scheduler
    no_opens = run_scheduler(campaign_id)

    if no_opens:
        logger.info(f"Found {len(no_opens)} no-open recipients for potential A/B re-send")
        # A/B test logic could be triggered here
        # For now, log them for manual review
        for r in no_opens:
            logger.info(f"  No open: {r['name']} ({r['company']}) - {r['email']}")


# ── Status Check ────────────────────────────────────────

def show_status(campaign_id: int, verbose: bool = False):
    """Display campaign status summary. Fetches live data from GMass if available."""
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        print(f"Campaign {campaign_id} not found.")
        return

    # Try to fetch live stats from GMass API
    gmass_cid = campaign.get("gmass_campaign_id", "")
    report = None
    if gmass_cid:
        try:
            from gmass_client import GMassClient
            gmass = GMassClient()
            report = gmass.get_campaign(gmass_cid)
        except Exception:
            pass

    if report and "statistics" in report:
        stats = report["statistics"]
        print(f"\n{'='*55}")
        print(f"  Campaign : {campaign['name']}")
        print(f"  GMass ID : {gmass_cid}")
        print(f"  Status   : {report.get('status', 'N/A')}")
        print(f"  Sent at  : {campaign.get('sent_at', 'N/A')}")
        print(f"{'='*55}")
        for key in ["recipients", "opens", "clicks", "replies", "bounces", "unsubscribes", "blocks"]:
            if key in stats:
                count = stats[key]
                bar = "#" * min(count, 50)
                print(f"  {key:14s} | {count:3d} | {bar}")
        print(f"{'='*55}")
    else:
        # Fallback to local DB
        recipients = db.get_recipients(campaign_id)
        status_counts = {}
        for r in recipients:
            s = r["status"]
            status_counts[s] = status_counts.get(s, 0) + 1

        print(f"\n{'='*55}")
        print(f"  Campaign : {campaign['name']}")
        print(f"  Status   : {campaign['status']}")
        print(f"  Sent at  : {campaign.get('sent_at', 'N/A')}")
        print(f"  Total    : {len(recipients)}")
        print(f"{'='*55}")
        for status, count in sorted(status_counts.items()):
            bar = "#" * count
            print(f"  {status:12s} | {count:3d} | {bar}")
        print(f"{'='*55}")

    if verbose:
        recipients = db.get_recipients(campaign_id)
        print(f"\n  {'Name':<20s} {'Company':<20s} {'Status':<10s} {'Email'}")
        print(f"  {'-'*70}")
        for r in recipients:
            name = (r.get('name') or '')[:19]
            company = (r.get('company') or '')[:19]
            print(f"  {name:<20s} {company:<20s} {r['status']:<10s} {r['email']}")
    print()


# ── Prospect Search Pipeline ──────────────────────────────

def prospect_search(
    industry: str | None = None,
    titles: list[str] | None = None,
    locations: list[str] | None = None,
    companies: list[str] | None = None,
    keywords: str | None = None,
    search_name: str | None = None,
    max_results: int = 100,
    enrich: bool = True,
    min_fit_score: float = 5.0,
    hunter_lookup: bool = True,
    industry_research: bool = True,
    verify_emails: bool = True,
    therapeutic_area: str | None = None,
) -> int:
    """
    Run the prospect finding pipeline:
    Phase 1: Apollo.io bulk search
    Phase 2: Hunter.io email lookup (missing emails)
    Phase 3: Industry research (ClinicalTrials + PubMed)
    Phase 4: Claude enrichment (email inference, fit scoring)
    Phase 5: Email verification (Hunter.io verify)
    Phase 6: Dedup & export CSV

    Returns: search_id from database.
    """
    import json

    if not search_name:
        today = datetime.now().strftime("%Y%m%d")
        search_name = f"Search_{today}"

    search_params = {
        "industry": industry,
        "titles": titles,
        "locations": locations,
        "companies": companies,
        "keywords": keywords,
    }

    search_id = db.create_prospect_search(
        name=search_name,
        search_params=json.dumps(search_params, ensure_ascii=False),
    )
    db.update_prospect_search(search_id, status="searching")

    # ── Phase 1: Apollo bulk search ───────────────────
    logger.info(f"Phase 1: Apollo search - {search_params}")
    apollo = ApolloClient()
    all_people: list[dict] = []

    if companies and len(companies) > 1:
        # Multi-org search handles all companies in one call (no pagination)
        try:
            result = apollo.search_people(
                person_titles=titles,
                person_locations=locations,
                organization_names=companies,
                q_keywords=keywords,
                per_page=max_results,
            )
            all_people = result.get("people", [])
        except Exception as e:
            logger.error(f"Apollo multi-org search failed: {e}")
    else:
        # Single company or no company: paginate
        page = 1
        while len(all_people) < max_results:
            try:
                result = apollo.search_people(
                    person_titles=titles,
                    person_locations=locations,
                    organization_names=companies,
                    q_keywords=keywords,
                    per_page=min(25, max_results - len(all_people)),
                    page=page,
                )
            except Exception as e:
                logger.error(f"Apollo search failed on page {page}: {e}")
                break

            people = result.get("people", [])
            if not people:
                break
            all_people.extend(people)
            page += 1

    logger.info(f"Found {len(all_people)} prospects from Apollo")
    db.update_prospect_search(search_id, total_found=len(all_people))

    # Store raw results in DB
    for person in all_people:
        normalized = ApolloClient.normalize_person(person)
        db.add_prospect(
            search_id=search_id,
            contact_name=normalized["contact_name"],
            email=normalized.get("email", ""),
            company=normalized.get("company", ""),
            title=normalized.get("title", ""),
            linkedin_url=normalized.get("linkedin_url", ""),
            location=normalized.get("location", ""),
            email_confidence="verified" if normalized.get("email") else "unknown",
            source="apollo",
            source_data=json.dumps(person, ensure_ascii=False, default=str),
        )

    # ── Phase 2: Hunter.io email lookup ────────────────
    if hunter_lookup and HUNTER_API_KEY and all_people:
        logger.info("Phase 2: Hunter.io email lookup for missing emails")
        db.update_prospect_search(search_id, status="hunter_lookup")

        from hunter_client import HunterClient
        hunter = HunterClient()
        prospects_missing = db.get_prospects_missing_email(search_id)

        if prospects_missing:
            all_prospects_for_domain = db.get_prospects(search_id=search_id)
            logger.info(f"  {len(prospects_missing)} prospects missing email, running Hunter.io lookup...")
            hunter_results = hunter.batch_find_emails(prospects_missing,
                                                      all_prospects=all_prospects_for_domain)

            for hr in hunter_results:
                pid = hr["prospect_id"]
                db.update_prospect(pid,
                    email=hr["email"],
                    email_confidence=hr["confidence"],
                    hunter_email=hr["email"],
                    hunter_confidence=hr.get("hunter_score", 0),
                    source="apollo+hunter",
                )
            logger.info(f"  Hunter.io found {len(hunter_results)} emails")
        else:
            logger.info("  All prospects already have emails, skipping Hunter.io")

        db.update_prospect_search(search_id, hunter_completed=1)

    # ── Phase 3: Industry research data ──────────────
    research_context_data = []
    if industry_research and all_people:
        logger.info("Phase 3: Fetching industry research data (ClinicalTrials + PubMed)")
        db.update_prospect_search(search_id, status="researching")

        from research_client import ResearchClient
        research = ResearchClient()
        unique_companies = list(set(
            p["company"] for p in db.get_prospects(search_id=search_id) if p.get("company")
        ))

        for company in unique_companies[:20]:  # cap at 20 companies
            ctx = research.get_company_research_context(
                company=company,
                therapeutic_area=therapeutic_area or keywords,
            )
            if ctx.get("active_trials") or ctx.get("recent_publications"):
                research_context_data.append(ctx)
                # Store on each prospect record for that company
                prospects_at_company = [
                    p for p in db.get_prospects(search_id=search_id)
                    if p.get("company", "").lower() == company.lower()
                ]
                for p in prospects_at_company:
                    db.update_prospect(p["id"],
                        research_context=json.dumps(ctx, ensure_ascii=False, default=str)
                    )

        logger.info(f"  Research data collected for {len(research_context_data)}/{len(unique_companies)} companies")
        db.update_prospect_search(search_id, research_completed=1)

    # ── Phase 4: Claude enrichment ───────────────────
    if enrich and all_people:
        db.update_prospect_search(search_id, status="enriching")
        logger.info("Phase 4: Claude enrichment (email inference + fit scoring)")

        from claude_client import ClaudeClient
        claude = ClaudeClient()
        prospects_for_enrichment = db.get_prospects(search_id=search_id)

        existing_emails = [
            {"email": p["email"], "company": p["company"]}
            for p in prospects_for_enrichment
            if p.get("email")
        ]

        try:
            enriched = claude.enrich_prospects(
                prospects_json=json.dumps(
                    [{"name": p["contact_name"], "email": p["email"], "company": p["company"],
                      "title": p["title"], "linkedin": p.get("linkedin_url", ""),
                      "location": p.get("location", "")}
                     for p in prospects_for_enrichment],
                    ensure_ascii=False,
                ),
                search_criteria=search_params,
                existing_emails_for_pattern=existing_emails,
                research_context=research_context_data if research_context_data else None,
            )
            _apply_enrichment(search_id, enriched)
            db.update_prospect_search(search_id, total_enriched=len(prospects_for_enrichment))
        except Exception as e:
            logger.error(f"Claude enrichment failed: {e}")

    # ── Phase 5: Email verification ──────────────────
    if verify_emails and HUNTER_API_KEY:
        logger.info("Phase 5: Verifying emails via Hunter.io")
        db.update_prospect_search(search_id, status="verifying")

        from hunter_client import HunterClient
        hunter = HunterClient()
        unverified = db.get_unverified_prospects(search_id)
        emails_to_verify = [p["email"] for p in unverified if p.get("email")]

        if emails_to_verify:
            logger.info(f"  Verifying {len(emails_to_verify)} email addresses...")
            verification_results = hunter.batch_verify_emails(emails_to_verify)

            for p in unverified:
                email_addr = p.get("email", "")
                if email_addr in verification_results:
                    vr = verification_results[email_addr]
                    db.update_prospect(p["id"],
                        verification_status=vr["status"],
                        verification_score=vr.get("score", 0),
                    )
                    db.add_email_verification(
                        prospect_id=p["id"],
                        email=email_addr,
                        status=vr["status"],
                        score=vr.get("score", 0),
                        raw_response=json.dumps(vr, default=str),
                    )

            deliverable = sum(1 for v in verification_results.values() if v["status"] == "deliverable")
            risky = sum(1 for v in verification_results.values() if v["status"] == "risky")
            undeliverable_count = sum(1 for v in verification_results.values() if v["status"] == "undeliverable")
            logger.info(f"  Verification: {deliverable} deliverable, {risky} risky, {undeliverable_count} undeliverable")
        else:
            logger.info("  No unverified emails to check")

        db.update_prospect_search(search_id, verification_completed=1)

    # ── Phase 6: Export ──────────────────────────────
    db.update_prospect_search(search_id, status="completed", completed_at=datetime.now().isoformat())

    csv_content = db.export_prospects_to_csv(search_id, min_fit_score=min_fit_score)
    if csv_content.strip():
        today = datetime.now().strftime("%y%m%d")
        csv_path = OUTPUT_DIR / f"prospects_{today}.csv"
        csv_path.write_text(csv_content, encoding="utf-8-sig")
        qualified = db.get_prospects(search_id=search_id, min_fit_score=min_fit_score)
        logger.info(f"Exported {len(qualified)} qualified prospects to {csv_path}")

    logger.info(f"Prospect search complete. Search ID: {search_id}")
    return search_id


def _apply_enrichment(search_id: int, enriched_text: str):
    """Parse Claude's enriched CSV and update prospect records."""
    import re as re_mod
    import io

    # Extract CSV block
    pattern = r"```csv\s*\n(.*?)```"
    match = re_mod.search(pattern, enriched_text, re_mod.DOTALL)
    if not match:
        logger.warning("Could not extract CSV from enrichment output")
        return

    csv_block = match.group(1).strip()
    reader = csv.DictReader(io.StringIO(csv_block))

    prospects_in_db = {
        (p["contact_name"].strip(), p["company"].strip()): p["id"]
        for p in db.get_prospects(search_id=search_id)
    }

    for row in reader:
        key = (row.get("contact_name", "").strip(), row.get("company", "").strip())
        pid = prospects_in_db.get(key)
        if not pid:
            continue

        updates: dict = {"status": "enriched"}
        if row.get("email"):
            updates["email"] = row["email"]
        if row.get("email_confidence"):
            updates["email_confidence"] = row["email_confidence"]
        if row.get("fit_score"):
            try:
                updates["fit_score"] = float(row["fit_score"])
            except ValueError:
                pass
        if row.get("fit_reason"):
            updates["fit_reason"] = row["fit_reason"]
        db.update_prospect(pid, **updates)


# ── Full Pipeline ────────────────────────────────────────

def full_pipeline(csv_path: str, product_number: int = 1, campaign_name: str | None = None,
                   skip_send: bool = False):
    """
    Run the complete pipeline:
    1. Generate cold emails (Claude API + /coldmail skill)
    2. Review & auto-fix (Claude API + /review skill)
    3. Send via GMass API (unless skip_send=True)
    """
    logger.info("Starting full pipeline...")

    # Phase 1a: Generate (outputs MD + extracts CSV)
    md_path = phase1_generate(csv_path, product_number)

    # Phase 1b: Review
    phase1_review(md_path, auto_fix=True)

    # Phase 2: Send
    today = datetime.now().strftime("%y%m%d")
    final_csv = OUTPUT_DIR / f"{today}final.csv"
    mailmerge_csv = OUTPUT_DIR / "coldmails_mailmerge.csv"

    csv_to_send = None
    if final_csv.exists():
        csv_to_send = final_csv
    elif mailmerge_csv.exists():
        csv_to_send = mailmerge_csv

    if not csv_to_send:
        logger.warning(
            f"No sendable CSV found. Expected {final_csv} or {mailmerge_csv}."
        )
        return

    if skip_send:
        logger.info(f"skip_send=True. CSV ready at {csv_to_send}. Skipping GMass send.")
        return

    campaign_id = phase2_send(str(csv_to_send), campaign_name)
    logger.info(f"Pipeline complete. Campaign ID: {campaign_id}")
    show_status(campaign_id)


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
    p_run.add_argument("--skip-send", action="store_true", help="Generate & review only, skip sending")

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
    p_status.add_argument("-v", "--verbose", action="store_true", help="Show per-recipient details")

    # prospect: search for new prospects
    p_prospect = subparsers.add_parser("prospect", help="Search for prospects via Apollo.io")
    p_prospect.add_argument("--industry", help="Industry filter (e.g., 'pharmaceutical')")
    p_prospect.add_argument("--titles", nargs="+", help="Job title filters")
    p_prospect.add_argument("--locations", nargs="+", help="Location filters")
    p_prospect.add_argument("--companies", nargs="+", help="Specific company names")
    p_prospect.add_argument("--keywords", help="Keyword search")
    p_prospect.add_argument("--name", help="Search name")
    p_prospect.add_argument("--max-results", type=int, default=100)
    p_prospect.add_argument("--min-fit", type=float, default=5.0)
    p_prospect.add_argument("--no-enrich", action="store_true", help="Skip Claude enrichment")
    p_prospect.add_argument("--no-hunter", action="store_true", help="Skip Hunter.io email lookup")
    p_prospect.add_argument("--no-research", action="store_true", help="Skip ClinicalTrials/PubMed research")
    p_prospect.add_argument("--no-verify", action="store_true", help="Skip email verification")
    p_prospect.add_argument("--therapeutic-area", help="Therapeutic area for PubMed search (e.g., 'CNS', 'oncology')")

    # webhook: start webhook server
    subparsers.add_parser("webhook", help="Start webhook server")

    args = parser.parse_args()

    if args.command == "run":
        full_pipeline(args.csv, args.product, args.name, skip_send=args.skip_send)
    elif args.command == "send":
        phase2_send(args.csv, args.name)
    elif args.command == "followup":
        phase3_followup(args.campaign_id)
    elif args.command == "status":
        show_status(args.campaign_id, verbose=getattr(args, 'verbose', False))
    elif args.command == "prospect":
        prospect_search(
            industry=args.industry,
            titles=args.titles,
            locations=args.locations,
            companies=args.companies,
            keywords=args.keywords,
            search_name=args.name,
            max_results=args.max_results,
            min_fit_score=args.min_fit,
            enrich=not args.no_enrich,
            hunter_lookup=not args.no_hunter,
            industry_research=not args.no_research,
            verify_emails=not args.no_verify,
            therapeutic_area=args.therapeutic_area,
        )
    elif args.command == "webhook":
        from webhook_server import app
        from config import WEBHOOK_HOST, WEBHOOK_PORT
        app.run(host=WEBHOOK_HOST, port=WEBHOOK_PORT, debug=True)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
