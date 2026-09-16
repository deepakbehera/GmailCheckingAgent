import os
from pathlib import Path
from dotenv import load_dotenv

# Base directory
BASE_DIR = Path(__file__).resolve().parent
# Test isolation runs BEFORE loading .env: when JOB_AGENT_DB_PATH is set we
# are under pytest/unittest and must not inherit DATABASE_URL (which would
# point the tests at the production Neon database).
_IS_TEST_RUN = bool(os.getenv("JOB_AGENT_DB_PATH", "").strip())
if _IS_TEST_RUN:
    os.environ.pop("DATABASE_URL", None)
load_dotenv(BASE_DIR / ".env")
if _IS_TEST_RUN:
    os.environ.pop("DATABASE_URL", None)

# App configuration
APP_TITLE = "AI Gmail Job Hunter & Notification Agent"
VERSION = "1.0.0"

# Target Email to monitor
DEFAULT_TARGET_EMAIL = os.getenv("TARGET_EMAIL", "deepak.gvit@gmail.com")

# Checking interval in minutes (default 15 minutes)
DEFAULT_CHECK_INTERVAL_MINS = int(os.getenv("CHECK_INTERVAL_MINUTES", "15"))

# Database path
# Vercel serverless functions have a read-only filesystem except /tmp,
# so store the SQLite DB there when running on Vercel.
if os.getenv("VERCEL"):
    DB_PATH = Path("/tmp") / "gmail_jobs.db"
else:
    DB_PATH = BASE_DIR / "data" / "gmail_jobs.db"
# Test isolation: tests set JOB_AGENT_DB_PATH to a temp file so they never
# touch the real dashboard data (local SQLite or the Neon production DB).
_db_override = os.getenv("JOB_AGENT_DB_PATH", "").strip()
if _db_override:
    DB_PATH = Path(_db_override)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Gemini API Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DEFAULT_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

# Notification Configuration
# Default ntfy.sh topic for free, instant mobile push alerts
DEFAULT_NTFY_TOPIC = os.getenv("NTFY_TOPIC", "deepak-job-hunter-alerts")
DEFAULT_DESKTOP_NOTIFY = os.getenv("DESKTOP_NOTIFY", "true").lower() == "true"
DEFAULT_MOBILE_NOTIFY = os.getenv("MOBILE_NOTIFY", "true").lower() == "true"

# Email Authentication
# Modes: "imap", "oauth", "simulator"
DEFAULT_AUTH_MODE = os.getenv("AUTH_MODE", "simulator")
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
GMAIL_CREDENTIALS_FILE = BASE_DIR / "credentials.json"
GMAIL_TOKEN_FILE = BASE_DIR / "token.json"

# Server Host and Port
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

# --- Tailored Resume Generation -------------------------------------------
# Base resume PDF (the master copy to tailor from). Kept in the gitignored
# data/ folder so the PII never lands in the public GitHub repo.
RESUME_BASE_PDF = Path(os.getenv("RESUME_BASE_PDF", BASE_DIR / "data" / "Deepak_Kumar_Behera_Resume.pdf"))

# Where tailored PDFs are saved on this machine (D:\TaileredResume\ locally).
# On Vercel the filesystem is read-only except /tmp, so PDFs are only cached
# there; the user downloads them from the dashboard instead.
RESUME_OUTPUT_DIR = Path(os.getenv("RESUME_OUTPUT_DIR", "D:/TaileredResume"))

# Repo-side cache folder for tailored PDFs (gitignored via data/). The
# dashboard View/Download buttons stream the PDF from here.
RESUME_CACHE_DIR = Path(os.getenv("RESUME_CACHE_DIR", BASE_DIR / "data" / "resumes"))

# --- Public dashboard URL ------------------------------------------------
# Public dashboard URL used in push-notification action buttons (ntfy
# "Open Dashboard"). The Vercel deployment URL must always win here: on the
# serverless deployment there is no cloudflared tunnel, but the settings table
# may still hold a stale trycloudflare.com URL written by an old local run.
# Override with PUBLIC_DASHBOARD_URL env var if the domain ever changes.
PUBLIC_DASHBOARD_URL = os.getenv("PUBLIC_DASHBOARD_URL", "https://gmail-checking-agent.vercel.app").rstrip("/")
