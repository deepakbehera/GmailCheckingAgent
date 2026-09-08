import sys
from pathlib import Path

# Add project root to sys.path so all imports work seamlessly on Vercel
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

# Vercel serverless entrypoint.
# NOTE: the Gmail check endpoint (/api/check-now) can take 30-50s per cycle.
# On the Vercel Hobby plan the maximum function duration is 60s, configured
# via Project Settings -> Functions -> Function Max Duration (set to 60s).
# If you are on Hobby and hitting timeouts, run check cycles from the local
# agent (python run.py) instead - it shares the same Neon database.

from app import app
