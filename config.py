import os
from pathlib import Path
from dotenv import load_dotenv

# Base directory
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

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
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Gemini API Configuration
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DEFAULT_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

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
