import sqlite3
import json
import os
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import List, Dict, Any, Optional
from config import DB_PATH, DEFAULT_TARGET_EMAIL, DEFAULT_CHECK_INTERVAL_MINS, DEFAULT_NTFY_TOPIC, DEFAULT_DESKTOP_NOTIFY, DEFAULT_MOBILE_NOTIFY, DEFAULT_AUTH_MODE

# ---------------------------------------------------------------------------
# Dual-backend database layer:
#   - If DATABASE_URL is set (e.g. Neon Postgres on Vercel) -> Postgres
#   - Otherwise -> local SQLite (data/gmail_jobs.db), unchanged behavior
# All SQL in this file uses SQLite-style '?' placeholders; the Postgres
# cursor wrapper transparently converts them to '%s'.
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
IS_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

if IS_POSTGRES:
    # Normalize older postgres:// scheme for psycopg2
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


class _PgCursor:
    """Cursor wrapper over psycopg2 exposing the sqlite3-style interface used here."""

    def __init__(self, pg_cursor):
        self._cur = pg_cursor

    @staticmethod
    def _convert(sql: str, has_params: bool) -> str:
        # Work only OUTSIDE single-quoted string literals (SQL contains literal
        # '?' and '%' inside URLs / LIKE patterns).
        parts = sql.split("'")
        for i in range(0, len(parts), 2):  # even indexes are outside quotes
            if has_params:
                # psycopg2 applies %-formatting when params are passed:
                # escape literal % first, then convert ? placeholders to %s
                parts[i] = parts[i].replace("%", "%%").replace("?", "%s")
            else:
                # No params -> no %-interpolation; just convert ? (none expected)
                parts[i] = parts[i].replace("?", "%s")
        return "'".join(parts)

    def execute(self, sql: str, params: tuple = ()): 
        converted = self._convert(sql, bool(params))
        if params:
            self._cur.execute(converted, list(params))
        else:
            self._cur.execute(converted)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def lastrowid(self):
        return getattr(self._cur, "lastrowid", None)

    @property
    def rowcount(self):
        return self._cur.rowcount


class _PgConnection:
    """Connection wrapper over psycopg2 exposing the sqlite3-style interface."""

    def __init__(self):
        import psycopg2
        import psycopg2.extras
        self._conn = psycopg2.connect(DATABASE_URL)
        self._cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def cursor(self):
        return _PgCursor(self._cur)

    def commit(self):
        self._conn.commit()

    def close(self):
        try:
            self._cur.close()
            self._conn.close()
        except Exception:
            pass


def get_db():
    if IS_POSTGRES:
        return _PgConnection()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# Dialect-specific DDL -------------------------------------------------------

_JOBS_COLUMNS = """
    message_id TEXT,
    email_subject TEXT,
    email_sender TEXT,
    source_platform TEXT DEFAULT 'Direct',
    date_received TEXT,
    job_title TEXT NOT NULL,
    company_name TEXT NOT NULL,
    location TEXT,
    job_type TEXT,
    apply_url TEXT,
    salary TEXT,
    skills TEXT,
    experience_level TEXT,
    summary TEXT,
    raw_email_snippet TEXT,
    status TEXT DEFAULT 'NEW',
    applied_at TEXT,
    notes TEXT,
    applied_earlier INTEGER DEFAULT 0,
    previous_application_id INTEGER,
    previous_applied_date TEXT,
    previous_job_title TEXT,
    match_score INTEGER DEFAULT 80,
    created_at TEXT,
    updated_at TEXT
"""

def _create_schema(cursor):
    if IS_POSTGRES:
        cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS jobs (
            id SERIAL PRIMARY KEY,
            {_JOBS_COLUMNS}
        );
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS email_logs (
            id SERIAL PRIMARY KEY,
            message_id TEXT,
            sender TEXT,
            subject TEXT,
            date_received TEXT,
            is_job INTEGER DEFAULT 0,
            jobs_extracted_count INTEGER DEFAULT 0,
            ai_classification_summary TEXT,
            check_cycle_timestamp TEXT,
            created_at TEXT
        );
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS check_history (
            id SERIAL PRIMARY KEY,
            checked_at TEXT,
            emails_scanned INTEGER DEFAULT 0,
            new_jobs_found INTEGER DEFAULT 0,
            status_message TEXT,
            triggered_by TEXT DEFAULT 'scheduler',
            created_at TEXT
        );
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        """)
    else:
        cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            {_JOBS_COLUMNS}
        );
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS email_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT,
            sender TEXT,
            subject TEXT,
            date_received TEXT,
            is_job INTEGER DEFAULT 0,
            jobs_extracted_count INTEGER DEFAULT 0,
            ai_classification_summary TEXT,
            check_cycle_timestamp TEXT,
            created_at TEXT
        );
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS check_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT,
            emails_scanned INTEGER DEFAULT 0,
            new_jobs_found INTEGER DEFAULT 0,
            status_message TEXT,
            triggered_by TEXT DEFAULT 'scheduler',
            created_at TEXT
        );
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        """)

    # Alter table if source_platform is missing from earlier schema
    if IS_POSTGRES:
        # Never let a failed ALTER abort the transaction: check first.
        cursor.execute("SELECT COUNT(*) AS cnt FROM information_schema.columns WHERE table_name = 'jobs' AND column_name = 'source_platform'")
        row = cursor.fetchone()
        col_count = row["cnt"] if hasattr(row, "get") or isinstance(row, dict) else row[0]
        if not col_count:
            cursor.execute("ALTER TABLE jobs ADD COLUMN source_platform TEXT DEFAULT 'Direct'")
    else:
        try:
            cursor.execute("ALTER TABLE jobs ADD COLUMN source_platform TEXT DEFAULT 'Direct'")
        except sqlite3.OperationalError:
            pass


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    if IS_POSTGRES:
        # Postgres: any failed statement aborts the transaction, so schema DDL
        # runs on its own committed connection first.
        _create_schema(cursor)
        conn.commit()
    else:
        _create_schema(cursor)

    # Migration: clean up any broken simulator URLs from earlier runs
    cursor.execute("""
    UPDATE jobs 
    SET apply_url = 'https://www.indeed.com/jobs?q=' || replace(job_title, ' ', '+') || '+' || replace(company_name, ' ', '+')
    WHERE apply_url LIKE '%jk=stripe-cloud-backend%' OR apply_url LIKE '%jk=anthropic-ai%'
    """)

    # Migration: infer source_platform from sender if NULL or Direct
    cursor.execute("UPDATE jobs SET source_platform = 'LinkedIn' WHERE (source_platform IS NULL OR source_platform = 'Direct') AND LOWER(email_sender) LIKE '%linkedin%'")
    cursor.execute("UPDATE jobs SET source_platform = 'Naukri' WHERE (source_platform IS NULL OR source_platform = 'Direct') AND LOWER(email_sender) LIKE '%naukri%'")
    cursor.execute("UPDATE jobs SET source_platform = 'Indeed' WHERE (source_platform IS NULL OR source_platform = 'Direct') AND LOWER(email_sender) LIKE '%indeed%'")
    cursor.execute("UPDATE jobs SET source_platform = 'Glassdoor' WHERE (source_platform IS NULL OR source_platform = 'Direct') AND LOWER(email_sender) LIKE '%glassdoor%'")

    # Populate default settings if missing
    default_settings = {
        "target_email": DEFAULT_TARGET_EMAIL,
        "check_interval_mins": str(DEFAULT_CHECK_INTERVAL_MINS),
        "desktop_notify": "true" if DEFAULT_DESKTOP_NOTIFY else "false",
        "mobile_notify": "true" if DEFAULT_MOBILE_NOTIFY else "false",
        "ntfy_topic": DEFAULT_NTFY_TOPIC,
        "auth_mode": DEFAULT_AUTH_MODE,
        "gemini_api_key": "",
        "imap_password": "",
        "public_url": "",
        "last_checked_at": "",
        "next_check_at": "",
    }

    now_str = datetime.now().isoformat()
    for k, v in default_settings.items():
        if IS_POSTGRES:
            cursor.execute("""
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT (key) DO NOTHING
            """, (k, v, now_str))
        else:
            cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()

# --- Job Operations ---

# Dashboard freshness: job alerts older than this are ignored when the
# dashboard lists jobs (stale postings are not worth applying to).
MAX_JOB_AGE_DAYS = 30  # ~1 month


def _parse_date_received(value: str) -> Optional[datetime]:
    """Parses the mixed date formats stored in jobs.date_received.
    Handles ISO timestamps (2026-08-26T00:20:41+00:00) and RFC 2822 email
    Date headers (Thu, 05 Sep 2026 12:34:56 +0000). Returns None when the
    value is missing or unparseable."""
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None


def _is_within_max_age(date_received: str) -> bool:
    """True when the job's email arrived within MAX_JOB_AGE_DAYS of now.
    Unparseable/missing dates count as fresh so nothing silently disappears."""
    dt = _parse_date_received(date_received)
    if dt is None:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_JOB_AGE_DAYS)
    return dt >= cutoff


def _job_sort_key(job: Dict[str, Any]):
    """Sorts non-APPLIED jobs first, then newest email date, then highest id.
    Done in Python because date_received mixes ISO timestamps and RFC 2822
    email Date headers, which no single SQL expression sorts portably."""
    dt = _parse_date_received(job.get("date_received") or "")
    received_ts = dt.timestamp() if dt else 0.0
    is_applied = 1 if (job.get("status") or "").upper() == "APPLIED" else 0
    return (is_applied, -received_ts, -(job.get("id") or 0))


def get_all_jobs(status_filter: Optional[str] = None, platform_filter: Optional[str] = None, search_query: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    query = "SELECT * FROM jobs WHERE 1=1"
    params = []

    if status_filter and status_filter.upper() != "ALL":
        query += " AND status = ?"
        params.append(status_filter.upper())

    if platform_filter and platform_filter.upper() != "ALL":
        p_clean = platform_filter.strip().lower()
        if p_clean == "direct":
            query += " AND (LOWER(COALESCE(source_platform, 'direct')) = 'direct' OR source_platform IS NULL OR source_platform = '' OR (LOWER(source_platform) NOT LIKE '%linkedin%' AND LOWER(source_platform) NOT LIKE '%naukri%' AND LOWER(source_platform) NOT LIKE '%indeed%' AND LOWER(source_platform) NOT LIKE '%glassdoor%' AND LOWER(source_platform) NOT LIKE '%monster%'))"
        else:
            query += " AND (LOWER(COALESCE(source_platform, '')) LIKE ? OR LOWER(COALESCE(email_sender, '')) LIKE ? OR LOWER(COALESCE(email_subject, '')) LIKE ?)"
            params.extend([f"%{p_clean}%", f"%{p_clean}%", f"%{p_clean}%"])

    if search_query and search_query.strip():
        search = f"%{search_query.strip()}%"
        query += " AND (job_title LIKE ? OR company_name LIKE ? OR location LIKE ? OR skills LIKE ? OR summary LIKE ? OR source_platform LIKE ?)"
        params.extend([search, search, search, search, search, search])

    # Base order: insertion id (stable). The date-aware ordering (newest email
    # first, APPLIED last) happens in Python below via _job_sort_key, because
    # date_received mixes ISO and RFC 2822 formats SQL can't sort portably.
    query += " ORDER BY id DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    # Ignore alerts from emails older than one month whenever the dashboard
    # (or anything else) lists jobs.
    jobs = [dict(row) for row in rows if _is_within_max_age(row["date_received"])]
    jobs.sort(key=_job_sort_key)
    return jobs

def get_job_by_id(job_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def insert_job(job_data: Dict[str, Any]) -> int:
    conn = get_db()
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()

    insert_sql = """
    INSERT INTO jobs (
        message_id, email_subject, email_sender, source_platform, date_received,
        job_title, company_name, location, job_type, apply_url,
        salary, skills, experience_level, summary, raw_email_snippet,
        status, applied_at, notes, applied_earlier, previous_application_id,
        previous_applied_date, previous_job_title, match_score,
        created_at, updated_at
    ) VALUES (
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?
    )
    """
    params = (
        job_data.get("message_id"),
        job_data.get("email_subject", ""),
        job_data.get("email_sender", ""),
        job_data.get("source_platform", "Direct"),
        job_data.get("date_received", now_str),
        job_data.get("job_title", "Unknown Role"),
        job_data.get("company_name", "Unknown Company"),
        job_data.get("location", "Remote / Unspecified"),
        job_data.get("job_type", "Full-time"),
        job_data.get("apply_url", ""),
        job_data.get("salary", "Not specified"),
        json.dumps(job_data.get("skills", [])) if isinstance(job_data.get("skills"), list) else job_data.get("skills", ""),
        job_data.get("experience_level", "Not specified"),
        job_data.get("summary", ""),
        job_data.get("raw_email_snippet", ""),
        job_data.get("status", "NEW"),
        job_data.get("applied_at"),
        job_data.get("notes", ""),
        1 if job_data.get("applied_earlier") else 0,
        job_data.get("previous_application_id"),
        job_data.get("previous_applied_date"),
        job_data.get("previous_job_title"),
        job_data.get("match_score", 85),
        now_str,
        now_str
    )

    if IS_POSTGRES:
        cursor.execute(insert_sql + " RETURNING id", params)
        row = cursor.fetchone()
        job_id = row["id"]
    else:
        cursor.execute(insert_sql, params)
        job_id = cursor.lastrowid

    conn.commit()
    conn.close()
    return job_id

def update_job_status(job_id: int, status: str, notes: Optional[str] = None) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()
    applied_at = now_str if status == "APPLIED" else None

    if notes is not None:
        cursor.execute("""
        UPDATE jobs 
        SET status = ?, applied_at = COALESCE(?, applied_at), notes = ?, updated_at = ?
        WHERE id = ?
        """, (status, applied_at, notes, now_str, job_id))
    else:
        cursor.execute("""
        UPDATE jobs 
        SET status = ?, applied_at = COALESCE(?, applied_at), updated_at = ?
        WHERE id = ?
        """, (status, applied_at, now_str, job_id))

    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return success

def delete_job(job_id: int) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    success = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return success

def mark_all_jobs_applied() -> int:
    """Marks every job as APPLIED (keeps existing applied_at if already set)."""
    conn = get_db()
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()
    cursor.execute("""
    UPDATE jobs
    SET status = 'APPLIED', applied_at = COALESCE(applied_at, ?), updated_at = ?
    WHERE status != 'APPLIED'
    """, (now_str, now_str))
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count

def delete_all_jobs() -> int:
    """Deletes every job from the dashboard/database. Returns count deleted."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) AS cnt FROM jobs")
    row = cursor.fetchone()
    count = row["cnt"] if row else 0
    cursor.execute("DELETE FROM jobs")
    conn.commit()
    conn.close()
    return count

def log_email_inspection(message_id: str, sender: str, subject: str, date_received: str, is_job: bool, summary: str, count: int = 0):
    conn = get_db()
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()
    cursor.execute("""
    INSERT INTO email_logs (message_id, sender, subject, date_received, is_job, jobs_extracted_count, ai_classification_summary, check_cycle_timestamp, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (message_id, sender, subject, date_received, 1 if is_job else 0, count, summary, now_str, now_str))
    conn.commit()
    conn.close()

def log_check_run(emails_scanned: int, new_jobs_found: int, status_message: str, triggered_by: str = "scheduler"):
    conn = get_db()
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()
    cursor.execute("""
    INSERT INTO check_history (checked_at, emails_scanned, new_jobs_found, status_message, triggered_by, created_at)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (now_str, emails_scanned, new_jobs_found, status_message, triggered_by, now_str))
    
    cursor.execute("UPDATE settings SET value = ?, updated_at = ? WHERE key = 'last_checked_at'", (now_str, now_str))
    conn.commit()
    conn.close()

def get_recent_email_logs(limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM email_logs ORDER BY id DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_check_history(limit: int = 30) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM check_history ORDER BY id DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def is_message_already_processed(message_id: str) -> bool:
    if not message_id:
        return False
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM email_logs WHERE message_id = ?", (message_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def find_previous_applications_for_company(company_name: str, exclude_id: Optional[int] = None) -> List[Dict[str, Any]]:
    if not company_name:
        return []
    conn = get_db()
    cursor = conn.cursor()
    clean_company = company_name.strip().lower()

    query = """
    SELECT * FROM jobs 
    WHERE LOWER(company_name) LIKE ?
    """
    params = [f"%{clean_company}%"]
    if exclude_id:
        query += " AND id != ?"
        params.append(exclude_id)
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_all_settings() -> Dict[str, str]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM settings")
    rows = cursor.fetchall()
    conn.close()
    return {row["key"]: row["value"] for row in rows}

def get_setting(key: str, default_val: str = "") -> str:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    value = row["value"] if row else None
    return value if value else default_val

def update_settings(settings_dict: Dict[str, str]):
    conn = get_db()
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()
    for k, v in settings_dict.items():
        cursor.execute("""
        INSERT INTO settings (key, value, updated_at) 
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """, (k, str(v), now_str))
    conn.commit()
    conn.close()

def get_dashboard_stats() -> Dict[str, Any]:
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) AS cnt FROM jobs")
    total_jobs = cursor.fetchone()["cnt"]

    cursor.execute("SELECT COUNT(*) AS cnt FROM jobs WHERE status = 'APPLIED'")
    applied_jobs = cursor.fetchone()["cnt"]

    cursor.execute("SELECT COUNT(*) AS cnt FROM jobs WHERE status = 'NEW'")
    new_jobs = cursor.fetchone()["cnt"]

    cursor.execute("SELECT COUNT(*) AS cnt FROM jobs WHERE applied_earlier = 1")
    repeat_companies = cursor.fetchone()["cnt"]

    cursor.execute("SELECT COUNT(*) AS cnt FROM email_logs")
    total_emails_scanned = cursor.fetchone()["cnt"]

    cursor.execute("SELECT source_platform, COUNT(*) AS cnt FROM jobs GROUP BY source_platform")
    platform_rows = cursor.fetchall()
    platform_counts = {row["source_platform"]: row["cnt"] for row in platform_rows}

    cursor.execute("SELECT value FROM settings WHERE key = 'last_checked_at'")
    last_check_row = cursor.fetchone()
    last_checked_at = last_check_row["value"] if last_check_row else None

    cursor.execute("SELECT value FROM settings WHERE key = 'public_url'")
    pub_url_row = cursor.fetchone()
    public_url = pub_url_row["value"] if pub_url_row else ""

    conn.close()
    return {
        "total_jobs": total_jobs,
        "applied_jobs": applied_jobs,
        "new_jobs": new_jobs,
        "repeat_companies": repeat_companies,
        "total_emails_scanned": total_emails_scanned,
        "platform_counts": platform_counts,
        "last_checked_at": last_checked_at,
        "public_url": public_url
    }

init_db()
