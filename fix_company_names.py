"""
Repairs bad company names already stored in the jobs table.

A row is repaired when company_name is a placeholder ('Company from Email',
'Unknown Company'), missing, or a tech/skill fragment ('.Net', 'Manual QA').
The replacement comes from (in priority order):
  1. The email subject via ai_extractor._company_from_subject (e.g.
     'Deepak, your application was sent to Kanerika Inc' -> Kanerika Inc).
     For 'New jobs similar to X at Y' digests the hint is applied to the
     FIRST email row only (later rows are similar jobs from other companies).
  2. The Gemini link-enrichment call on the stored anchor text + URL
     (only when rows remain that we could not resolve deterministically).

Run WITHOUT arguments for a read-only preview:
    python fix_company_names.py

Apply the fixes:
    python fix_company_names.py --apply
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env")

from database import get_db  # noqa: E402
from ai_extractor import (  # noqa: E402
    _company_from_subject,
    _looks_like_company,
    _clean_company_name,
    _subject_hint_is_first_link_only,
)

BAD_PLACEHOLDERS = {"company from email", "unknown company", "unknown", "job from email", ""}


def is_bad_company(name: str) -> bool:
    if not name or name.strip().lower() in BAD_PLACEHOLDERS:
        return True
    return not _looks_like_company(name)


def main():
    apply = "--apply" in sys.argv

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, job_title, company_name, email_subject, apply_url, "
        "raw_email_snippet, email_sender, source_platform FROM jobs ORDER BY id ASC"
    )
    rows = [dict(r) for r in cursor.fetchall()]

    # The subject hint of a multi-job digest ('X at Y and 6 more jobs',
    # 'X at Y. 12 more ...', 'New jobs similar to X at Y') names only the
    # lead/original job - apply it to the FIRST bad row per subject only.
    subjects_seen = set()
    fixes = []  # (row, new_company, source)
    for row in rows:
        if not is_bad_company(row.get("company_name")):
            continue
        subject = row.get("email_subject") or ""
        hint = _company_from_subject(subject)
        first_only = _subject_hint_is_first_link_only(subject)
        key = subject.strip().lower()
        if first_only and key in subjects_seen:
            hint = ""
        subjects_seen.add(key)
        if hint and _looks_like_company(hint) and hint.strip().lower() != (row.get("company_name") or "").strip().lower():
            fixes.append((row, _clean_company_name(hint), "subject"))

    fixed_ids = {row["id"] for row, _, _ in fixes}

    # Remaining rows have no subject context to lean on (the email subject did
    # not name a company). Future emails get Gemini enrichment instead.
    remaining = [r for r in rows if r["id"] not in fixed_ids and is_bad_company(r.get("company_name"))]

    conn.close()

    print(f"Jobs scanned    : {len(rows)}")
    print(f"Bad company rows: {sum(1 for r in rows if is_bad_company(r.get('company_name')))}")
    print(f"Fixable now     : {len(fixes)} (via subject context)")
    if remaining:
        print(f"Unresolvable    : {len(remaining)} (would need Gemini or manual edit)")
    print("-" * 90)
    for row, new_company, source in fixes:
        print(f"  id={row['id']:>5}  {row.get('company_name')!r:>28} -> {new_company!r}  [{source}]  ({(row.get('job_title') or '')[:40]})")
    for row in remaining:
        print(f"  id={row['id']:>5}  {row.get('company_name')!r:>28} -> ?            [no context]  ({(row.get('job_title') or '')[:40]})")

    if not fixes and not remaining:
        print("\nAll company names look sane. Nothing to do.")
        return
    if not apply:
        print("\nDRY RUN - no changes made. Re-run with --apply to write the fixes.")
        return

    now = None
    conn = get_db()
    cursor = conn.cursor()
    updated = 0
    for row, new_company, _src in fixes:
        cursor.execute(
            "UPDATE jobs SET company_name = ?, updated_at = ? WHERE id = ?",
            (new_company, __import__("datetime").datetime.now().isoformat(), row["id"]),
        )
        updated += cursor.rowcount
    conn.commit()
    conn.close()
    print(f"\n✅ Updated {updated} row(s).")


if __name__ == "__main__":
    main()
