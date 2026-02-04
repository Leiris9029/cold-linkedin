"""
Database module - SQLite-based campaign and recipient state management.

Tracks: campaigns, recipients, events (open/reply/bounce), followup stages.
"""
import sqlite3
from datetime import datetime
from pathlib import Path
from config import DB_PATH


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS campaigns (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            csv_path        TEXT,
            spreadsheet_id  TEXT,
            worksheet_id    TEXT,
            gmass_list_id   TEXT,
            gmass_draft_id  TEXT,
            gmass_campaign_id TEXT,
            product_number  INTEGER DEFAULT 1,
            status          TEXT DEFAULT 'draft',  -- draft|sent|tracking|completed
            created_at      TEXT DEFAULT (datetime('now')),
            sent_at         TEXT
        );

        CREATE TABLE IF NOT EXISTS recipients (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id     INTEGER NOT NULL REFERENCES campaigns(id),
            email           TEXT NOT NULL,
            name            TEXT,
            company         TEXT,
            language        TEXT DEFAULT 'ja',
            subject         TEXT,
            body            TEXT,
            status          TEXT DEFAULT 'pending',  -- pending|sent|opened|replied|bounced
            followup_stage  INTEGER DEFAULT 0,       -- 0=initial, 1/2/3=followup stage
            last_event_at   TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_id    INTEGER NOT NULL REFERENCES recipients(id),
            campaign_id     INTEGER NOT NULL REFERENCES campaigns(id),
            event_type      TEXT NOT NULL,  -- sent|open|click|reply|bounce|unsubscribe
            event_data      TEXT,           -- JSON: webhook payload
            received_at     TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS followups (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_id    INTEGER NOT NULL REFERENCES recipients(id),
            campaign_id     INTEGER NOT NULL REFERENCES campaigns(id),
            stage           INTEGER NOT NULL,  -- 1, 2, 3
            subject         TEXT,
            body            TEXT,
            scheduled_at    TEXT,
            sent_at         TEXT,
            status          TEXT DEFAULT 'pending'  -- pending|sent|cancelled
        );

        CREATE INDEX IF NOT EXISTS idx_recipients_campaign ON recipients(campaign_id);
        CREATE INDEX IF NOT EXISTS idx_recipients_status ON recipients(status);
        CREATE INDEX IF NOT EXISTS idx_events_recipient ON events(recipient_id);
        CREATE INDEX IF NOT EXISTS idx_followups_scheduled ON followups(scheduled_at);
    """)
    conn.commit()
    conn.close()


# ── Campaign CRUD ────────────────────────────────────────

def create_campaign(name: str, csv_path: str, product_number: int = 1) -> int:
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO campaigns (name, csv_path, product_number) VALUES (?, ?, ?)",
        (name, csv_path, product_number),
    )
    conn.commit()
    campaign_id = cur.lastrowid
    conn.close()
    return campaign_id


def update_campaign(campaign_id: int, **kwargs):
    conn = get_connection()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [campaign_id]
    conn.execute(f"UPDATE campaigns SET {sets} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def get_campaign(campaign_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Recipient CRUD ───────────────────────────────────────

def add_recipient(campaign_id: int, email: str, name: str, company: str,
                  language: str, subject: str, body: str) -> int:
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO recipients
           (campaign_id, email, name, company, language, subject, body)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (campaign_id, email, name, company, language, subject, body),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def get_recipients(campaign_id: int, status: str | None = None) -> list[dict]:
    conn = get_connection()
    if status:
        rows = conn.execute(
            "SELECT * FROM recipients WHERE campaign_id = ? AND status = ?",
            (campaign_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM recipients WHERE campaign_id = ?", (campaign_id,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_recipient(recipient_id: int, **kwargs):
    conn = get_connection()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [recipient_id]
    conn.execute(f"UPDATE recipients SET {sets} WHERE id = ?", vals)
    conn.commit()
    conn.close()


# ── Event Logging ────────────────────────────────────────

def log_event(recipient_id: int, campaign_id: int, event_type: str, event_data: str = ""):
    conn = get_connection()
    conn.execute(
        "INSERT INTO events (recipient_id, campaign_id, event_type, event_data) VALUES (?, ?, ?, ?)",
        (recipient_id, campaign_id, event_type, event_data),
    )
    # Update recipient status
    status_map = {
        "sent": "sent",
        "open": "opened",
        "reply": "replied",
        "bounce": "bounced",
    }
    if event_type in status_map:
        conn.execute(
            "UPDATE recipients SET status = ?, last_event_at = datetime('now') WHERE id = ?",
            (status_map[event_type], recipient_id),
        )
    conn.commit()
    conn.close()


# ── Followup Management ─────────────────────────────────

def schedule_followup(recipient_id: int, campaign_id: int, stage: int,
                      subject: str, body: str, scheduled_at: str) -> int:
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO followups
           (recipient_id, campaign_id, stage, subject, body, scheduled_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (recipient_id, campaign_id, stage, subject, body, scheduled_at),
    )
    conn.commit()
    fid = cur.lastrowid
    conn.close()
    return fid


def get_pending_followups(before: str | None = None) -> list[dict]:
    """Get followups that are due for sending."""
    conn = get_connection()
    if before:
        rows = conn.execute(
            "SELECT * FROM followups WHERE status = 'pending' AND scheduled_at <= ?",
            (before,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM followups WHERE status = 'pending'"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recipients_needing_followup(campaign_id: int, stage: int, days_since: int) -> list[dict]:
    """
    Find recipients who:
    - Have not replied
    - Have not bounced
    - Are at followup_stage < stage
    - Last event was more than `days_since` days ago
    """
    conn = get_connection()
    rows = conn.execute(
        """SELECT r.* FROM recipients r
           WHERE r.campaign_id = ?
             AND r.status NOT IN ('replied', 'bounced')
             AND r.followup_stage < ?
             AND julianday('now') - julianday(COALESCE(r.last_event_at, r.created_at)) >= ?
        """,
        (campaign_id, stage, days_since),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
