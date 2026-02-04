"""
Webhook server - receives GMass event callbacks (open, click, reply, bounce, unsubscribe).

GMass sends webhooks in near-real-time batches (every few minutes).
This server logs events to the database and triggers follow-up actions.

Run with: python webhook_server.py
Expose via ngrok for local dev: ngrok http 5000
"""
import json
import logging
from flask import Flask, request, jsonify
from config import WEBHOOK_HOST, WEBHOOK_PORT, WEBHOOK_SECRET
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)


def _verify_secret():
    """Optional: verify webhook secret if configured."""
    if WEBHOOK_SECRET:
        token = request.headers.get("X-Webhook-Secret", "")
        if token != WEBHOOK_SECRET:
            return False
    return True


def _find_recipient(email: str, campaign_id: int | None = None) -> dict | None:
    """Look up a recipient by email address."""
    conn = db.get_connection()
    if campaign_id:
        row = conn.execute(
            "SELECT * FROM recipients WHERE email = ? AND campaign_id = ?",
            (email, campaign_id),
        ).fetchone()
    else:
        # Get most recent recipient with this email
        row = conn.execute(
            "SELECT * FROM recipients WHERE email = ? ORDER BY created_at DESC LIMIT 1",
            (email,),
        ).fetchone()
    conn.close()
    return dict(row) if row else None


def _handle_event(event_type: str):
    """Generic event handler for all GMass webhook types."""
    if not _verify_secret():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    email = data.get("emailAddress", data.get("email", ""))
    campaign_id = data.get("campaignId")

    logger.info(f"Webhook [{event_type}] email={email} campaign={campaign_id}")

    if not email:
        return jsonify({"error": "no email in payload"}), 400

    recipient = _find_recipient(email, campaign_id)
    if not recipient:
        logger.warning(f"Recipient not found for email: {email}")
        return jsonify({"status": "recipient_not_found"}), 200

    # Log the event
    db.log_event(
        recipient_id=recipient["id"],
        campaign_id=recipient["campaign_id"],
        event_type=event_type,
        event_data=json.dumps(data, ensure_ascii=False),
    )

    logger.info(
        f"Event logged: {event_type} for {recipient['name']} ({recipient['company']})"
    )

    return jsonify({"status": "ok"}), 200


# ── Webhook Endpoints ────────────────────────────────────

@app.route("/webhook/send", methods=["POST"])
def on_send():
    return _handle_event("sent")


@app.route("/webhook/open", methods=["POST"])
def on_open():
    return _handle_event("open")


@app.route("/webhook/click", methods=["POST"])
def on_click():
    return _handle_event("click")


@app.route("/webhook/reply", methods=["POST"])
def on_reply():
    return _handle_event("reply")


@app.route("/webhook/bounce", methods=["POST"])
def on_bounce():
    return _handle_event("bounce")


@app.route("/webhook/unsubscribe", methods=["POST"])
def on_unsubscribe():
    return _handle_event("unsubscribe")


# ── Health Check ─────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


# ── Dashboard (simple status view) ──────────────────────

@app.route("/status/<int:campaign_id>", methods=["GET"])
def campaign_status(campaign_id: int):
    """Quick status check for a campaign."""
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        return jsonify({"error": "campaign not found"}), 404

    recipients = db.get_recipients(campaign_id)
    status_counts = {}
    for r in recipients:
        s = r["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    return jsonify({
        "campaign": campaign["name"],
        "total_recipients": len(recipients),
        "status_breakdown": status_counts,
    })


# ── Run ──────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()
    logger.info(f"Webhook server starting on {WEBHOOK_HOST}:{WEBHOOK_PORT}")
    app.run(host=WEBHOOK_HOST, port=WEBHOOK_PORT, debug=True)
