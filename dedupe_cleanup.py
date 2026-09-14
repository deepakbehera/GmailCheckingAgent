"""
One-time duplicate cleanup for the jobs table.

Run WITHOUT arguments to preview what would be deleted (safe, read-only):
    python dedupe_cleanup.py

Run with --apply to actually delete the duplicate rows:
    python dedupe_cleanup.py --apply

Uses the DATABASE_URL from .env (Neon Postgres) when set, otherwise the local
SQLite database. Also rewrites a stale trycloudflare.com/loca.lt public_url
setting to the production dashboard URL so old notifications stop pointing at
dead tunnel links.
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env")

from config import PUBLIC_DASHBOARD_URL  # noqa: E402
import database  # noqa: E402
from database import (  # noqa: E402
    get_db,
    normalize_apply_url,
    _norm_company,
    _titles_similar,
    update_settings,
    get_setting,
    IS_POSTGRES,
)


def preview_duplicates():
    """Returns (keep_rows, duplicate_rows) using the same matching rules as dedupe_existing_jobs()."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM jobs ORDER BY id ASC")
        rows = [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

    seen_urls = {}
    seen_companies = []
    keep, dups = [], []
    for job in rows:
        norm_url = normalize_apply_url(job.get("apply_url"))
        norm_company = _norm_company(job.get("company_name"))
        title = job.get("job_title")

        dup_of = None
        if norm_url and norm_url in seen_urls:
            dup_of = seen_urls[norm_url]
        elif norm_company:
            for prior in seen_companies:
                if prior["norm_company"] == norm_company and _titles_similar(prior["title"], title):
                    dup_of = prior["id"]
                    break

        if dup_of is not None:
            job["_duplicate_of"] = dup_of
            dups.append(job)
            continue

        if norm_url:
            seen_urls[norm_url] = job["id"]
        if norm_company:
            seen_companies.append({"id": job["id"], "norm_company": norm_company, "title": title})
        keep.append(job)
    return keep, dups


def main():
    apply = "--apply" in sys.argv

    keep, dups = preview_duplicates()
    print(f"Database backend : {'Postgres (Neon)' if IS_POSTGRES else 'Local SQLite'}")
    print(f"Total jobs       : {len(keep) + len(dups)}")
    print(f"Unique jobs kept : {len(keep)}")
    print(f"Duplicates found : {len(dups)}")
    print("-" * 80)
    for d in dups[:40]:
        print(f"  DELETE id={d['id']:>5}  [{d.get('source_platform')}] {d.get('job_title')} @ {d.get('company_name')}"
              f"  (dup of #{d['_duplicate_of']})")
    if len(dups) > 40:
        print(f"  ... and {len(dups) - 40} more")

    stale_url = (get_setting("public_url", "") or "").strip()
    if stale_url and ("trycloudflare.com" in stale_url or "loca.lt" in stale_url):
        print("-" * 80)
        print(f"Stale tunnel URL in settings: {stale_url}")
        print(f"Will rewrite to: {PUBLIC_DASHBOARD_URL}")

    if not dups and not stale_url:
        print("\nNothing to clean up.")
        return

    if not apply:
        print("\nDRY RUN — no changes made. Re-run with --apply to delete these rows.")
        return

    deleted = database.dedupe_existing_jobs()
    updates = {}
    if stale_url and ("trycloudflare.com" in stale_url or "loca.lt" in stale_url):
        updates["public_url"] = PUBLIC_DASHBOARD_URL
    if updates:
        update_settings(updates)

    print(f"\n✅ Done. Deleted {deleted} duplicate row(s)."
          + (f" public_url rewritten to {PUBLIC_DASHBOARD_URL}." if updates else ""))


if __name__ == "__main__":
    main()
