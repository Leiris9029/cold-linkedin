"""
Configuration module - loads API keys from .env and defines campaign settings.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

# ── API Keys ──────────────────────────────────────────────
GMASS_API_KEY = os.getenv("GMASS_API_KEY", "")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID", "")

# ── GMass ─────────────────────────────────────────────────
GMASS_BASE_URL = "https://api.gmass.co/api"
GMASS_FROM_EMAIL = os.getenv("GMASS_FROM_EMAIL", "")
GMASS_FROM_NAME = os.getenv("GMASS_FROM_NAME", "")

# ── Gmail IMAP (for reading replies) ─────────────────────
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", os.getenv("GMASS_FROM_EMAIL", ""))
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# ── Apollo.io (Prospect Search) ──────────────────────────
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "")

# ── Hunter.io (Email Lookup & Verification) ──────────────
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")

# ── Findymail (Email Finder) ──────────────────────────────
FINDYMAIL_API_KEY = os.getenv("FINDYMAIL_API_KEY", "")

# ── Tavily (Web Search for Agents) ───────────────────────
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# ── Claude ────────────────────────────────────────────────
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
CLAUDE_MODEL_LIGHT = os.getenv("CLAUDE_MODEL_LIGHT", "claude-haiku-4-5-20251001")

# ── Webhook ───────────────────────────────────────────────
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "5000"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # optional verification token

# ── Paths ─────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"
DB_PATH = PROJECT_ROOT / "orchestrator" / "campaign.db"

# ── Campaign Defaults ─────────────────────────────────────
FOLLOWUP_SCHEDULE = {
    1: 3,   # 1st followup: 3 days after initial send
    2: 5,   # 2nd followup: 5 days after 1st followup
    3: 7,   # 3rd followup: 7 days after 2nd followup (breakup)
}

# Days to wait before considering "no open" for A/B re-send
NO_OPEN_THRESHOLD_DAYS = 5

# Maximum followup stages
MAX_FOLLOWUP_STAGES = 3
