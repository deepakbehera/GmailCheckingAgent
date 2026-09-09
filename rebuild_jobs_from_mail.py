"""
Rebuild the jobs table from recent monitored-sender emails.

Re-scans the last N days of email from the monitored senders, extracts the
real posting links (anchor-first), enriches titles/companies with Gemini,
and inserts one job card per link. Resumable: message-ids already handled
are tracked in rebuild_checkpoint.txt, so the script can be re-run until
the whole window is processed.

Usage:
    python rebuild_jobs_from_mail.py --days 14 --limit 50
"""
import os
import sys
import time
import imaplib
import email as email_mod
import argparse
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv(".env")
os.environ.setdefault("AUTH_MODE", "imap")

import database
from ai_extractor import ai_extractor, detect_platform_source, extract_real_job_links, build_jobs_from_real_links
from job_matcher import job_matcher

CHECKPOINT_FILE = "rebuild_checkpoint.txt"

MONITORED_SENDERS = [
    "donotreply@jobalert.indeed.com",
    "noreply@glassdoor.com",
    "jobmessenger@monsterindia.com",
    "jobs-noreply@linkedin.com",
    "do-not-reply@roku.com",
    "naukri.com",
    "aditi@talent500.co",
]


def load_checkpoint():
    if not os.path.exists(CHECKPOINT_FILE):
        return set()
    with open(CHECKPOINT_FILE, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def add_checkpoint(mid):
    with open(CHECKPOINT_FILE, "a", encoding="utf-8") as f:
        f.write(mid + "\n")


def dec(s):
    if not s:
        return ""
    return "".join(
        p.decode(c or "utf-8", errors="replace") if isinstance(p, bytes) else p
        for p, c in decode_header(s)
    )


def extract_bodies(msg):
    body, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in ("text/plain", "text/html") and not part.get_filename():
                payload = part.get_payload(decode=True)
                if payload:
                    txt = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    if ct == "text/html":
                        html += txt
                    else:
                        body += txt
    else:
        payload = msg.get_payload(decode=True) or b""
        txt = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        if msg.get_content_type() == "text/html":
            html = txt
        else:
            body = txt
    return body, html


def count_existing(mid):
    conn = database.get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM jobs WHERE message_id LIKE ?", (f"{mid}%",))
    row = cur.fetchone()
    n = row["n"] if isinstance(row, dict) else row[0]
    conn.close()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--limit", type=int, default=0, help="max emails to process this run (0 = all)")
    args = ap.parse_args()

    target_email = database.get_setting("target_email", "")
    app_password = database.get_setting("imap_password", "") or os.getenv("GMAIL_APP_PASSWORD", "")
    assert target_email and app_password, "missing Gmail credentials"

    done = load_checkpoint()
    processed = stored = 0
    started = time.time()

    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(target_email, app_password)
    mail.select("INBOX")

    # Collect candidate (internaldate, num, msgid, sender) across all senders.
    # Headers are fetched one id at a time: giant batched fetches upset Gmail
    # for large mailboxes and one bad response would silently drop a sender.
    candidates = {}

    def ensure_connected(m):
        try:
            m.noop()
            return m, False
        except Exception:
            m2 = imaplib.IMAP4_SSL("imap.gmail.com")
            m2.login(target_email, app_password)
            m2.select("INBOX")
            return m2, True

    for sender in MONITORED_SENDERS:
        try:
            status, data = mail.search(None, f'X-GM-RAW "from:{sender} newer_than:{args.days}d"')
        except Exception as e:
            print(f"  search error for {sender}: {e}", flush=True)
            mail, _ = ensure_connected(mail)
            status, data = mail.search(None, f'X-GM-RAW "from:{sender} newer_than:{args.days}d"')
        if status != "OK" or not data[0]:
            print(f"  no results for {sender} (status={status})", flush=True)
            continue
        ids = data[0].split()
        for num in ids:
            try:
                status, fdata = mail.fetch(num, "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                if status != "OK" or not fdata:
                    continue
                for item in fdata:
                    if not isinstance(item, tuple):
                        continue
                    raw = item[1].decode("utf-8", errors="replace")
                    mid = ""
                    for line in raw.splitlines():
                        if line.lower().startswith("message-id:"):
                            mid = line.split(":", 1)[1].strip().strip("<>")
                            break
                    if not mid:
                        continue
                    idate = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
                    try:
                        ts = datetime.strptime(idate, '"%d-%b-%Y %H:%M:%S %z"').timestamp()
                    except Exception:
                        ts = 0
                    candidates[mid] = (ts, num, sender)
            except Exception as e:
                print(f"  fetch error for {sender} id {num}: {e}", flush=True)
                mail, _ = ensure_connected(mail)
                continue

    print(f"candidate emails in window: {len(candidates)} (checkpointed: {len(done)})")
    ordered = sorted(candidates.items(), key=lambda kv: kv[1][0], reverse=True)

    for mid, (ts, num, sender) in ordered:
        if mid in done:
            continue
        if args.limit and processed >= args.limit:
            break

        if count_existing(mid) > 0:
            processed += 1
            add_checkpoint(mid)
            continue

        status, fdata = mail.fetch(num, "(BODY.PEEK[])")
        if status != "OK" or not fdata or not isinstance(fdata[0], tuple):
            add_checkpoint(mid)
            continue
        msg = email_mod.message_from_bytes(fdata[0][1])
        subject = dec(msg.get("Subject", ""))
        body, html = extract_bodies(msg)
        date_rcvd = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).isoformat() \
            if msg.get("Date") else datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        platform = detect_platform_source(sender, subject, body + " " + html)

        jobs = build_jobs_from_real_links(
            extract_real_job_links(html, body, platform), platform, False
        )
        if jobs:
            api_key = ai_extractor.get_api_key()
            for attempt in range(3):
                if api_key:
                    ai_extractor._enrich_link_jobs_with_gemini(
                        jobs, subject, sender, body, html, platform, api_key
                    )
                if any(j.get("ai_enriched") for j in jobs):
                    break
                time.sleep(20)

            new_in_email = 0
            for i, item in enumerate(jobs):
                company = item.get("company_name", "Unknown Company")
                title = item.get("job_title", "Job from Email")
                dup = job_matcher.check_duplicate_and_history(company, title)
                record = {
                    "message_id": f"{mid}_{i}",
                    "email_subject": subject,
                    "email_sender": sender,
                    "source_platform": platform,
                    "date_received": date_rcvd,
                    "job_title": title,
                    "company_name": company,
                    "location": item.get("location", "Remote"),
                    "job_type": item.get("job_type", "Full-time"),
                    "apply_url": item.get("apply_url", ""),
                    "salary": item.get("salary", "Not specified"),
                    "skills": item.get("skills", []),
                    "experience_level": item.get("experience_level", ""),
                    "summary": item.get("summary", ""),
                    "raw_email_snippet": body[:500],
                    "status": "NEW",
                    "applied_earlier": dup["applied_earlier"],
                    "previous_application_id": dup["previous_application_id"],
                    "previous_applied_date": dup["previous_applied_date"],
                    "previous_job_title": dup["previous_job_title"],
                    "match_score": item.get("match_score", 85),
                }
                if item.get("apply_url_real") or item.get("link_source") == "email":
                    database.insert_job(record)
                    new_in_email += 1
            stored += new_in_email
            print(f"[{processed + 1}] +{new_in_email} jobs | {platform:10s} | {subject[:55]!r}", flush=True)
        else:
            print(f"[{processed + 1}]  0 jobs | {platform:10s} | {subject[:55]!r}", flush=True)

        processed += 1
        add_checkpoint(mid)

    mail.logout()
    mins = (time.time() - started) / 60
    print("=" * 50)
    print(f"REBUILD RUN DONE in {mins:.1f} min")
    print(f"  emails processed : {processed}")
    print(f"  quality jobs     : {stored}")
    print(f"  remaining        : {max(0, len(ordered) - len(done) - processed)}")


if __name__ == "__main__":
    main()
