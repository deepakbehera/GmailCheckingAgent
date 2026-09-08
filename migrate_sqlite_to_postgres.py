"""
One-time migration: copy all data from the local SQLite DB (data/gmail_jobs.db)
into the Postgres database specified by DATABASE_URL (Neon).

Usage:
    python migrate_sqlite_to_postgres.py

Safe to re-run: existing rows (matched by id) are skipped.
"""
import os
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL.startswith(("postgres://", "postgresql://")):
    print("DATABASE_URL not set to a Postgres URL in .env - nothing to migrate.")
    sys.exit(1)

DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

import psycopg2
import psycopg2.extras

SQLITE_PATH = BASE_DIR / "data" / "gmail_jobs.db"
if not SQLITE_PATH.exists():
    print(f"Local SQLite DB not found at {SQLITE_PATH} - nothing to migrate.")
    sys.exit(0)

sqlite_conn = sqlite3.connect(str(SQLITE_PATH))
sqlite_conn.row_factory = sqlite3.Row
pg_conn = psycopg2.connect(DATABASE_URL)
pg_cur = pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def table_columns(cursor, table):
    cursor.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,),
    )
    return {r["column_name"] for r in cursor.fetchall()}


def migrate_table(table, id_col="id"):
    rows = sqlite_conn.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        print(f"{table}: 0 rows (skipped)")
        return

    pg_cols = table_columns(pg_cur, table)
    inserted = skipped = 0

    for row in rows:
        data = {k: v for k, v in dict(row).items() if k in pg_cols}
        cols = list(data.keys())
        placeholders = ", ".join(["%s"] * len(cols))
        col_list = ", ".join(cols)

        # Skip if the id already exists in Postgres
        pg_cur.execute(f"SELECT 1 FROM {table} WHERE {id_col} = %s", (data[id_col],))
        if pg_cur.fetchone():
            skipped += 1
            continue

        pg_cur.execute(
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
            [data[c] for c in cols],
        )
        inserted += 1

    # Keep SERIAL sequence ahead of the imported ids
    pg_cur.execute(
        f"SELECT setval(pg_get_serial_sequence('{table}', '{id_col}'), "
        f"GREATEST((SELECT COALESCE(MAX({id_col}), 1) FROM {table}), 1))"
    )
    pg_conn.commit()
    print(f"{table}: {inserted} inserted, {skipped} already present")


try:
    migrate_table("jobs")
    migrate_table("email_logs")
    migrate_table("check_history")

    # Settings: upsert by key (skip empty values from SQLite)
    rows = sqlite_conn.execute("SELECT key, value, updated_at FROM settings").fetchall()
    upserted = 0
    for row in rows:
        if row["value"] is None or row["value"] == "":
            continue
        pg_cur.execute("""
            INSERT INTO settings (key, value, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (row["key"], row["value"], row["updated_at"]))
        upserted += 1
    pg_conn.commit()
    print(f"settings: {upserted} upserted")

    # Final verification
    pg_cur.execute("SELECT COUNT(*) AS cnt FROM jobs")
    print(f"\\nVerification - jobs in Neon: {pg_cur.fetchone()['cnt']}")
    pg_cur.execute("SELECT source_platform, COUNT(*) AS cnt FROM jobs GROUP BY source_platform")
    print("Platform counts:", {r["source_platform"]: r["cnt"] for r in pg_cur.fetchall()})
    print("\\nMIGRATION COMPLETE")
finally:
    sqlite_conn.close()
    pg_conn.close()
