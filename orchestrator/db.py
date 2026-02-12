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
            transactional_email_id TEXT,  -- GMass transactional API response ID
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

        CREATE TABLE IF NOT EXISTS prospect_searches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            search_params   TEXT,              -- JSON: {industry, titles, locations, ...}
            source          TEXT DEFAULT 'apollo',
            total_found     INTEGER DEFAULT 0,
            total_enriched  INTEGER DEFAULT 0,
            status          TEXT DEFAULT 'pending',  -- pending|searching|enriching|completed|failed
            created_at      TEXT DEFAULT (datetime('now')),
            completed_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS prospects (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            search_id       INTEGER REFERENCES prospect_searches(id),
            contact_name    TEXT NOT NULL,
            email           TEXT,
            email_confidence TEXT DEFAULT 'unknown',  -- verified|high|medium|low|unknown
            company         TEXT,
            title           TEXT,
            linkedin_url    TEXT,
            location        TEXT,
            fit_score       REAL DEFAULT 0,
            fit_reason      TEXT,
            source          TEXT DEFAULT 'apollo',
            source_data     TEXT,              -- JSON: raw API response
            status          TEXT DEFAULT 'new',  -- new|enriched|exported|excluded
            exported_to_campaign INTEGER REFERENCES campaigns(id),
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_prospects_search ON prospects(search_id);
        CREATE INDEX IF NOT EXISTS idx_prospects_email ON prospects(email);
        CREATE INDEX IF NOT EXISTS idx_prospects_company ON prospects(company);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_prospects_dedup ON prospects(email, company)
            WHERE email IS NOT NULL AND email != '';

        CREATE TABLE IF NOT EXISTS email_verifications (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            prospect_id     INTEGER REFERENCES prospects(id),
            email           TEXT NOT NULL,
            provider        TEXT DEFAULT 'hunter',
            status          TEXT NOT NULL,
            score           INTEGER DEFAULT 0,
            raw_response    TEXT,
            verified_at     TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_verifications_prospect ON email_verifications(prospect_id);
        CREATE INDEX IF NOT EXISTS idx_verifications_email ON email_verifications(email);

        CREATE TABLE IF NOT EXISTS search_presets (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL UNIQUE,
            industry        TEXT,
            titles          TEXT,
            locations       TEXT,
            companies       TEXT,
            keywords        TEXT,
            max_results     INTEGER DEFAULT 100,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS clay_enrichments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id        TEXT NOT NULL,
            search_id       INTEGER REFERENCES prospect_searches(id),
            input_name      TEXT,
            input_company   TEXT,
            enriched_data   TEXT,
            status          TEXT DEFAULT 'pending',
            pushed_at       TEXT DEFAULT (datetime('now')),
            enriched_at     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_clay_batch ON clay_enrichments(batch_id);
        CREATE INDEX IF NOT EXISTS idx_clay_status ON clay_enrichments(status);

        CREATE TABLE IF NOT EXISTS sender_profiles (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL UNIQUE,
            name_en         TEXT,
            name_ja         TEXT,
            title_en        TEXT,
            title_ja        TEXT,
            company_en      TEXT,
            company_ja      TEXT,
            email           TEXT,
            phone           TEXT,
            signature_ja    TEXT,
            signature_en    TEXT,
            extra_info      TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS campaign_profiles (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            name                TEXT NOT NULL UNIQUE,
            product_name        TEXT,
            product_description TEXT,
            sales_goal          TEXT,
            target_titles       TEXT,
            target_region       TEXT,
            language            TEXT DEFAULT 'en',
            tone                TEXT,
            cta_type            TEXT,
            sender_context      TEXT,
            extra_notes         TEXT,
            created_at          TEXT DEFAULT (datetime('now')),
            updated_at          TEXT
        );
    """)
    conn.commit()

    # target_feedback — per-profile (or global) feedback for Agent 1
    conn.execute("""
        CREATE TABLE IF NOT EXISTS target_feedback (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id      INTEGER,   -- NULL = global, else campaign_profiles.id
            feedback        TEXT NOT NULL,
            product_summary TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_target_feedback_profile
            ON target_feedback(profile_id)
    """)
    conn.commit()

    # -- Schema migration: add columns for v2 pipeline --
    _migration_columns = [
        ("prospects", "hunter_email", "TEXT"),
        ("prospects", "hunter_confidence", "INTEGER DEFAULT 0"),
        ("prospects", "verification_status", "TEXT DEFAULT 'pending'"),
        ("prospects", "verification_score", "INTEGER DEFAULT 0"),
        ("prospects", "research_context", "TEXT"),
        ("prospect_searches", "hunter_completed", "INTEGER DEFAULT 0"),
        ("prospect_searches", "research_completed", "INTEGER DEFAULT 0"),
        ("prospect_searches", "verification_completed", "INTEGER DEFAULT 0"),
        ("search_presets", "feedback_hash", "TEXT"),
        ("search_presets", "product_description", "TEXT"),
        ("search_presets", "target_hint", "TEXT"),
        ("search_presets", "target_region", "TEXT"),
    ]
    for table, column, col_type in _migration_columns:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    conn.close()


# ── Sender Profiles CRUD ───────────────────────────────

def save_sender_profile(
    name: str,
    name_en: str = "",
    name_ja: str = "",
    title_en: str = "",
    title_ja: str = "",
    company_en: str = "",
    company_ja: str = "",
    email: str = "",
    phone: str = "",
    signature_ja: str = "",
    signature_en: str = "",
    extra_info: str = "",
) -> int:
    conn = get_connection()
    cur = conn.execute("""
        INSERT INTO sender_profiles
            (name, name_en, name_ja, title_en, title_ja,
             company_en, company_ja, email, phone,
             signature_ja, signature_en, extra_info)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            name_en=excluded.name_en, name_ja=excluded.name_ja,
            title_en=excluded.title_en, title_ja=excluded.title_ja,
            company_en=excluded.company_en, company_ja=excluded.company_ja,
            email=excluded.email, phone=excluded.phone,
            signature_ja=excluded.signature_ja, signature_en=excluded.signature_en,
            extra_info=excluded.extra_info,
            updated_at=datetime('now')
    """, (name, name_en, name_ja, title_en, title_ja,
          company_en, company_ja, email, phone,
          signature_ja, signature_en, extra_info))
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid


def get_sender_profiles() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM sender_profiles ORDER BY updated_at DESC, id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_sender_profile(profile_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM sender_profiles WHERE id = ?", (profile_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_sender_profile(profile_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM sender_profiles WHERE id = ?", (profile_id,))
    conn.commit()
    conn.close()


def render_sender_profile_md(profile: dict) -> str:
    """Render a sender profile dict as markdown (same format as sender_profile.md)."""
    if not profile:
        return ""
    lines = ["# Sender Profile", ""]
    lines.append("## 기본 정보")
    if profile.get("name_en"):
        lines.append(f"- **이름 (영문)**: {profile['name_en']}")
    if profile.get("name_ja"):
        lines.append(f"- **이름 (일본어)**: {profile['name_ja']}")
    if profile.get("title_en"):
        lines.append(f"- **직함 (영문)**: {profile['title_en']}")
    if profile.get("title_ja"):
        lines.append(f"- **직함 (일본어)**: {profile['title_ja']}")
    if profile.get("company_en"):
        lines.append(f"- **회사명 (영문)**: {profile['company_en']}")
    if profile.get("company_ja"):
        lines.append(f"- **회사명 (일본어)**: {profile['company_ja']}")
    if profile.get("email"):
        lines.append(f"- **이메일**: {profile['email']}")
    if profile.get("phone"):
        lines.append(f"- **전화번호**: {profile['phone']}")
    if profile.get("extra_info"):
        lines.append(f"- **추가 정보**: {profile['extra_info']}")
    if profile.get("signature_ja"):
        lines.append("")
        lines.append("## 서명 (일본어 메일용)")
        lines.append("```")
        lines.append(profile["signature_ja"])
        lines.append("```")
    if profile.get("signature_en"):
        lines.append("")
        lines.append("## 서명 (영문 메일용)")
        lines.append("```")
        lines.append(profile["signature_en"])
        lines.append("```")
    return "\n".join(lines)


# ── Campaign Profiles CRUD ──────────────────────────────

def save_campaign_profile(
    name: str,
    product_name: str = "",
    product_description: str = "",
    sales_goal: str = "",
    target_titles: str = "",
    target_region: str = "",
    language: str = "en",
    tone: str = "",
    cta_type: str = "",
    sender_context: str = "",
    extra_notes: str = "",
) -> int:
    conn = get_connection()
    cur = conn.execute("""
        INSERT INTO campaign_profiles
            (name, product_name, product_description, sales_goal,
             target_titles, target_region, language, tone, cta_type,
             sender_context, extra_notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            product_name=excluded.product_name,
            product_description=excluded.product_description,
            sales_goal=excluded.sales_goal,
            target_titles=excluded.target_titles,
            target_region=excluded.target_region,
            language=excluded.language,
            tone=excluded.tone,
            cta_type=excluded.cta_type,
            sender_context=excluded.sender_context,
            extra_notes=excluded.extra_notes,
            updated_at=datetime('now')
    """, (name, product_name, product_description, sales_goal,
          target_titles, target_region, language, tone, cta_type,
          sender_context, extra_notes))
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid


def get_campaign_profiles() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM campaign_profiles ORDER BY updated_at DESC, id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_campaign_profile(profile_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM campaign_profiles WHERE id = ?", (profile_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_campaign_profile(profile_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM campaign_profiles WHERE id = ?", (profile_id,))
    conn.commit()
    conn.close()


# ── Search Presets CRUD ──────────────────────────────────

def save_preset(name: str, industry: str = "", titles: str = "",
                locations: str = "", companies: str = "",
                keywords: str = "", max_results: int = 100,
                feedback_hash: str = "",
                product_description: str = "",
                target_hint: str = "",
                target_region: str = "") -> int:
    conn = get_connection()
    cur = conn.execute("""
        INSERT INTO search_presets
            (name, industry, titles, locations, companies, keywords,
             max_results, feedback_hash, product_description, target_hint, target_region)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            industry=excluded.industry, titles=excluded.titles,
            locations=excluded.locations, companies=excluded.companies,
            keywords=excluded.keywords, max_results=excluded.max_results,
            feedback_hash=excluded.feedback_hash,
            product_description=excluded.product_description,
            target_hint=excluded.target_hint,
            target_region=excluded.target_region,
            updated_at=datetime('now')
    """, (name, industry, titles, locations, companies, keywords,
          max_results, feedback_hash, product_description, target_hint, target_region))
    conn.commit()
    preset_id = cur.lastrowid
    conn.close()
    return preset_id


def get_presets() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM search_presets ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_preset(preset_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM search_presets WHERE id = ?", (preset_id,))
    conn.commit()
    conn.close()


# ── Target Feedback CRUD ──────────────────────────────────

def add_target_feedback(
    feedback: str,
    product_summary: str = "",
    profile_id: int | None = None,
) -> int:
    """Add a feedback entry. profile_id=None → global feedback."""
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO target_feedback (profile_id, feedback, product_summary)
           VALUES (?, ?, ?)""",
        (profile_id, feedback.strip(), product_summary),
    )
    conn.commit()
    fid = cur.lastrowid
    conn.close()
    return fid


def get_target_feedback(profile_id: int | None = None) -> list[dict]:
    """Get feedback entries for a specific profile (or global if None)."""
    conn = get_connection()
    if profile_id is None:
        rows = conn.execute(
            "SELECT * FROM target_feedback WHERE profile_id IS NULL ORDER BY created_at"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM target_feedback WHERE profile_id = ? ORDER BY created_at",
            (profile_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_combined_feedback_text(profile_id: int | None = None) -> str:
    """Get combined feedback: global + profile-specific, formatted as text."""
    conn = get_connection()
    # Global feedback
    global_rows = conn.execute(
        "SELECT feedback, product_summary, created_at FROM target_feedback "
        "WHERE profile_id IS NULL ORDER BY created_at"
    ).fetchall()
    # Profile-specific feedback
    profile_rows = []
    if profile_id is not None:
        profile_rows = conn.execute(
            "SELECT feedback, product_summary, created_at FROM target_feedback "
            "WHERE profile_id = ? ORDER BY created_at",
            (profile_id,),
        ).fetchall()
    conn.close()

    lines = []
    if global_rows:
        lines.append("## 글로벌 피드백 (모든 프로필 공통)")
        for r in global_rows:
            ts = r["created_at"][:16] if r["created_at"] else ""
            ps = f"({r['product_summary']}) " if r["product_summary"] else ""
            lines.append(f"- [{ts}] {ps}{r['feedback']}")
    if profile_rows:
        lines.append("")
        lines.append("## 캠페인 프로필 전용 피드백")
        for r in profile_rows:
            ts = r["created_at"][:16] if r["created_at"] else ""
            ps = f"({r['product_summary']}) " if r["product_summary"] else ""
            lines.append(f"- [{ts}] {ps}{r['feedback']}")

    return "\n".join(lines)


def delete_target_feedback(feedback_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM target_feedback WHERE id = ?", (feedback_id,))
    conn.commit()
    conn.close()


def clear_target_feedback(profile_id: int | None = None):
    """Clear all feedback for a profile (or global if None)."""
    conn = get_connection()
    if profile_id is None:
        conn.execute("DELETE FROM target_feedback WHERE profile_id IS NULL")
    else:
        conn.execute("DELETE FROM target_feedback WHERE profile_id = ?", (profile_id,))
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


# ── Prospect Search CRUD ─────────────────────────────────

def create_prospect_search(name: str, search_params: str, source: str = "apollo") -> int:
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO prospect_searches (name, search_params, source) VALUES (?, ?, ?)",
        (name, search_params, source),
    )
    conn.commit()
    search_id = cur.lastrowid
    conn.close()
    return search_id


def update_prospect_search(search_id: int, **kwargs):
    conn = get_connection()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [search_id]
    conn.execute(f"UPDATE prospect_searches SET {sets} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def get_prospect_searches() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM prospect_searches ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_prospect_search(search_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM prospect_searches WHERE id = ?", (search_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_prospect_search(search_id: int):
    """Delete a search and all its prospects and related verifications."""
    conn = get_connection()
    # Delete verifications for prospects in this search
    conn.execute("""
        DELETE FROM email_verifications
        WHERE prospect_id IN (SELECT id FROM prospects WHERE search_id = ?)
    """, (search_id,))
    # Delete prospects
    conn.execute("DELETE FROM prospects WHERE search_id = ?", (search_id,))
    # Delete the search record
    conn.execute("DELETE FROM prospect_searches WHERE id = ?", (search_id,))
    conn.commit()
    conn.close()


# ── Prospect CRUD ────────────────────────────────────────

def add_prospect(search_id: int, contact_name: str, email: str, company: str,
                 title: str, linkedin_url: str = "", location: str = "",
                 fit_score: float = 0, fit_reason: str = "",
                 email_confidence: str = "unknown", source: str = "apollo",
                 source_data: str = "") -> int | None:
    """Add a prospect, skipping if duplicate email+company exists."""
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO prospects
               (search_id, contact_name, email, company, title, linkedin_url,
                location, fit_score, fit_reason, email_confidence, source, source_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (search_id, contact_name, email, company, title, linkedin_url,
             location, fit_score, fit_reason, email_confidence, source, source_data),
        )
        conn.commit()
        pid = cur.lastrowid if cur.rowcount > 0 else None
    except Exception:
        pid = None
    conn.close()
    return pid


def get_prospects(search_id: int | None = None, status: str | None = None,
                  min_fit_score: float | None = None) -> list[dict]:
    conn = get_connection()
    query = "SELECT * FROM prospects WHERE 1=1"
    params: list = []
    if search_id is not None:
        query += " AND search_id = ?"
        params.append(search_id)
    if status:
        query += " AND status = ?"
        params.append(status)
    if min_fit_score is not None:
        query += " AND fit_score >= ?"
        params.append(min_fit_score)
    query += " ORDER BY fit_score DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_prospect(prospect_id: int, **kwargs):
    conn = get_connection()
    kwargs["updated_at"] = datetime.now().isoformat()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [prospect_id]
    try:
        conn.execute(f"UPDATE prospects SET {sets} WHERE id = ?", vals)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()  # email+company already exists in another row, skip
    conn.close()


def get_prospects_missing_email(search_id: int) -> list[dict]:
    """Get prospects that have no email (for Hunter.io lookup)."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT * FROM prospects
           WHERE search_id = ?
             AND (email IS NULL OR email = '')""",
        (search_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_unverified_prospects(search_id: int) -> list[dict]:
    """Get prospects with emails that haven't been verified yet."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT * FROM prospects
           WHERE search_id = ?
             AND email IS NOT NULL AND email != ''
             AND (verification_status IS NULL OR verification_status = 'pending')""",
        (search_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_email_verification(prospect_id: int, email: str, status: str,
                           score: int = 0, provider: str = "hunter",
                           raw_response: str = "") -> int:
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO email_verifications
           (prospect_id, email, provider, status, score, raw_response)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (prospect_id, email, provider, status, score, raw_response),
    )
    conn.commit()
    vid = cur.lastrowid
    conn.close()
    return vid


def export_prospects_to_csv(search_id: int, min_fit_score: float = 5.0) -> str:
    """Export qualified prospects as CSV string ready for /coldmail pipeline."""
    import io
    import csv as csv_mod

    prospects = get_prospects(search_id=search_id, min_fit_score=min_fit_score)
    prospects = [p for p in prospects if p.get("email")
                 and p.get("verification_status") != "undeliverable"]

    output = io.StringIO()
    writer = csv_mod.DictWriter(output, fieldnames=[
        "contact_name", "email", "company", "contact_title", "linkedin_url", "language",
    ])
    writer.writeheader()
    for p in prospects:
        writer.writerow({
            "contact_name": p["contact_name"],
            "email": p["email"],
            "company": p["company"],
            "contact_title": p.get("title", ""),
            "linkedin_url": p.get("linkedin_url", ""),
            "language": _infer_language(p.get("location", "")),
        })
    return output.getvalue()


# ── Clay Enrichments ────────────────────────────────────

def create_clay_batch(batch_id: str, companies: list[dict], search_id: int | None = None):
    """Record a batch of companies pushed to Clay for enrichment.

    Each company dict: {Company, Domain, Title Keywords}.
    Creates one 'pending' row per company to track completion.
    input_name column stores Title Keywords (no person name at push time).
    """
    conn = get_connection()
    for c in companies:
        conn.execute(
            """INSERT INTO clay_enrichments (batch_id, search_id, input_company, input_name)
               VALUES (?, ?, ?, ?)""",
            (batch_id, search_id, c.get("Company", ""), c.get("Title Keywords", "")),
        )
    conn.commit()
    conn.close()


def save_clay_callback(data: dict) -> bool:
    """Save enriched contact from Clay webhook callback.

    Clay sends one callback per enriched person found.
    Multiple people per company are possible.

    For each callback:
    1. Find the batch by company name → get batch_id, search_id
    2. Mark the pending company row as 'completed'
    3. INSERT a new 'enriched' row with the person's data
    """
    import json as _json
    conn = get_connection()
    company = data.get("Company", data.get("company", data.get("company_name", "")))
    if not company:
        conn.close()
        return False

    # Find the most recent batch row for this company to get batch_id
    ref = conn.execute(
        """SELECT batch_id, search_id FROM clay_enrichments
           WHERE input_company = ? ORDER BY pushed_at DESC LIMIT 1""",
        (company,),
    ).fetchone()

    if not ref:
        conn.close()
        return False

    batch_id = ref["batch_id"]
    search_id = ref["search_id"]

    # Mark pending company row as completed (idempotent)
    conn.execute(
        """UPDATE clay_enrichments SET status = 'completed', enriched_at = datetime('now')
           WHERE batch_id = ? AND input_company = ? AND status = 'pending'""",
        (batch_id, company),
    )

    # INSERT new enriched row for this person
    name = data.get("Name", data.get("name", data.get("Contact Name", "")))
    conn.execute(
        """INSERT INTO clay_enrichments
           (batch_id, search_id, input_name, input_company, enriched_data, status, enriched_at)
           VALUES (?, ?, ?, ?, ?, 'enriched', datetime('now'))""",
        (batch_id, search_id, name, company, _json.dumps(data, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
    return True


def get_clay_results(batch_id: str) -> list[dict]:
    """Get enriched contacts for a batch (one row per person found)."""
    import json as _json
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM clay_enrichments WHERE batch_id = ? AND status = 'enriched'",
        (batch_id,),
    ).fetchall()
    conn.close()
    results = []
    for r in rows:
        d = dict(r)
        if d.get("enriched_data"):
            d["enriched_data"] = _json.loads(d["enriched_data"])
        results.append(d)
    return results


def get_clay_batch_status(batch_id: str) -> dict:
    """Get status counts for a batch.

    Statuses:
      pending   — company pushed, waiting for Clay callback
      completed — company got at least one callback
      enriched  — individual person row (one per person found)
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM clay_enrichments WHERE batch_id = ? GROUP BY status",
        (batch_id,),
    ).fetchall()
    conn.close()
    counts = {r["status"]: r["cnt"] for r in rows}
    return {
        "total_companies": counts.get("pending", 0) + counts.get("completed", 0),
        "pending": counts.get("pending", 0),
        "completed": counts.get("completed", 0),
        "enriched_contacts": counts.get("enriched", 0),
    }


def _infer_language(location: str) -> str:
    """Infer email language from prospect location."""
    loc = location.lower()
    if any(kw in loc for kw in ["japan", "tokyo", "osaka", "jp"]):
        return "ja"
    if any(kw in loc for kw in ["korea", "seoul", "kr"]):
        return "ko"
    return "en"
