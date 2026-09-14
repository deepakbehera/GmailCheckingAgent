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

from database import get_db, get_setting  # noqa: E402
from ai_extractor import (  # noqa: E402
    _company_from_subject,
    _looks_like_company,
    _clean_company_name,
    _subject_hint_is_first_link_only,
    DEFAULT_GEMINI_MODEL,
    _gemini_quota_in_cooldown,
    _gemini_report_success,
    _gemini_report_quota_error,
)

import json as _json  # noqa: E402


# Placeholder texts that must never be mistaken for a real company
BAD_PLACEHOLDERS = {"company from email", "unknown company", "unknown", "job from email", ""}


def _gemini_resolve_companies(rows, subject: str) -> dict:
    """One batched Gemini call: for each row (title/url/snippet) return the
    hiring company. Returns {row_id: company_or_empty}. Rows Gemini cannot
    resolve map to '' (caller keeps/sets the neutral placeholder)."""
    api_key = get_setting("gemini_api_key", "").strip()
    if not api_key or _gemini_quota_in_cooldown() or not rows:
        return {}
    try:
        from google import genai
        from google.genai import types

        payload = [
            {
                "index": i,
                "job_title": (r.get("job_title") or "")[:120],
                "apply_url": (r.get("apply_url") or "")[:200],
                "email_context": (r.get("raw_email_snippet") or "")[:300],
            }
            for i, r in enumerate(rows)
        ]
        prompt = f"""
You are a job-alert parser. For EACH job below, identify the HIRING COMPANY
(the organisation with the vacancy, e.g. 'GoDaddy', 'Kanerika Inc', 'ZEISS Group').
Use the job title, the apply URL structure and the email context.
NEVER answer with a technology, skill or job role such as '.Net', 'Manual QA',
'Test Architect', 'Java', 'Automation'. If the company is genuinely not
determinable, use "Unknown".

Email subject: {subject[:300]}

Jobs:
{_json.dumps(payload, indent=1)}

Return ONLY raw JSON (no backticks):
{{"companies": [{{"index": 0, "company_name": "..."}}]}}
one entry per input job, same order, same count ({len(rows)}).
"""
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=DEFAULT_GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        out = {}
        if response and response.text:
            parsed = _json.loads(response.text)
            for item in parsed.get("companies", []):
                idx = item.get("index")
                if isinstance(idx, int) and 0 <= idx < len(rows):
                    company = _clean_company_name(str(item.get("company_name", "")))
                    out[rows[idx]["id"]] = company if _looks_like_company(company) else ""
            _gemini_report_success()
        return out
    except Exception as e:
        try:
            from ai_extractor import _quota_error
            if _quota_error(e):
                _gemini_report_quota_error(e)
        except Exception:
            pass
        print(f"  (Gemini enrichment failed: {e})")
        return {}


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
    # lead/original job. Bind the hint to the LOWEST row id sharing that
    # subject - permanent across runs, so later runs never leak the lead
    # company onto the digest's other jobs (which have other companies).
    min_id_per_subject = {}
    for row in rows:
        key = (row.get("email_subject") or "").strip().lower()
        if key and (key not in min_id_per_subject or row["id"] < min_id_per_subject[key]):
            min_id_per_subject[key] = row["id"]

    fixes = []  # (row, new_company, source)
    for row in rows:
        if not is_bad_company(row.get("company_name")):
            continue
        subject = row.get("email_subject") or ""
        hint = _company_from_subject(subject)
        key = subject.strip().lower()
        if _subject_hint_is_first_link_only(subject) and key and min_id_per_subject.get(key) != row["id"]:
            hint = ""
        if hint and _looks_like_company(hint) and hint.strip().lower() != (row.get("company_name") or "").strip().lower():
            fixes.append((row, _clean_company_name(hint), "subject"))

    fixed_ids = {row["id"] for row, _, _ in fixes}

    # Remaining rows have no subject context: try one batched Gemini call to
    # resolve the hiring company from title + URL + snippet.
    remaining = [r for r in rows if r["id"] not in fixed_ids and is_bad_company(r.get("company_name"))]
    if remaining:
        grouped = {}
        for r in remaining:
            grouped.setdefault((r.get("email_subject") or "").strip(), []).append(r)
        gemini_fixes = {}
        for subj_text, grp in grouped.items():
            gemini_fixes.update(_gemini_resolve_companies(grp, subj_text))
        for rid, company in gemini_fixes.items():
            if company:
                row = next(r for r in remaining if r["id"] == rid)
                fixes.append((row, company, "gemini"))
        resolved_ids = {rid for rid, c in gemini_fixes.items() if c}
        remaining = [r for r in remaining if r["id"] not in resolved_ids]
        # Rows Gemini could not resolve: keep the neutral placeholder rather
        # than misleading tech fragments like '.Net' or 'Manual QA'.
        for row in remaining:
            if row.get("company_name") not in ("", "Company from Email"):
                fixes.append((row, "Company from Email", "reset-placeholder"))
        fixed_ids.update({row["id"] for row, _, _ in fixes})
        remaining = [r for r in remaining if r["id"] not in fixed_ids]

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
