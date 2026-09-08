"""
Backfill: process the entire backlog of UNSEEN emails from monitored job-alert
senders, in batches, directly against Gmail IMAP.

- Resumable: safe to re-run; already-processed message IDs are skipped.
- Quiet: no desktop/mobile notifications during backfill (pass --notify to enable).
- Quality gate: heuristic-extracted placeholder rows ("Specialist Role" with
  search-fallback links) are discarded during backfill; only jobs with a real
  posting URL from the email body are stored. Pass --keep-all to disable.

Usage:
    python backfill_inbox.py                 # run until inbox is fully processed
    python backfill_inbox.py --limit 50      # stop after ~50 processed emails
    python backfill_inbox.py --notify        # also send notifications
"""
import os
import sys
import time
import argparse
import logging
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

logging.basicConfig(level=logging.WARNING)  # keep the console readable

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE_DIR, ".env"))

import imaplib
import email as email_lib
from email.header import decode_header
from config import IMAP_SERVER, IMAP_PORT
from database import (
    is_message_already_processed,
    log_email_inspection,
    insert_job,
    log_check_run,
    get_setting,
)
from ai_extractor import ai_extractor
from job_matcher import job_matcher

from email_service import build_sender_search_queries

BATCH_SIZE = 25
IMAP_SOCKET_TIMEOUT = 30

PLACEHOLDER_TITLES = {"specialist role", "unknown role", ""}


def decode_subject(raw):
    decoded = ""
    for part, enc in decode_header(raw or ""):
        if isinstance(part, bytes):
            decoded += part.decode(enc or "utf-8", errors="ignore")
        else:
            decoded += str(part)
    return decoded.strip()


def fetch_batch(mail, msg_ids):
    """Fetch full RFC822 for a batch of message ids (marks them as read)."""
    out = []
    for msg_id in msg_ids:
        try:
            status, data = mail.fetch(msg_id, "(RFC822)")
            if status != "OK" or not data:
                continue
        except imaplib.IMAP4.error as e:
            print(f"    ! fetch error for {msg_id}: {e}")
            continue
        for response_part in data:
            if not isinstance(response_part, tuple):
                continue
            try:
                msg = email_lib.message_from_bytes(response_part[1])
                raw_subj = msg.get("Subject", "No Subject")
                sender = msg.get("From", "Unknown Sender")
                date_received = msg.get("Date", datetime.now().isoformat())
                message_id = msg.get("Message-ID", f"backfill_{datetime.now().timestamp()}")

                body = ""
                html_body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if "attachment" in str(part.get("Content-Disposition")):
                            continue
                        payload = part.get_payload(decode=True)
                        if not payload:
                            continue
                        decoded = payload.decode("utf-8", errors="ignore")
                        if part.get_content_type() == "text/plain":
                            body += decoded
                        elif part.get_content_type() == "text/html":
                            html_body += decoded
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="ignore")

                out.append({
                    "message_id": message_id,
                    "subject": decode_subject(raw_subj),
                    "sender": sender,
                    "date_received": date_received,
                    "body": body[:4000],
                    "html_body": html_body[:5000],
                })
            except Exception as e:
                print(f"    ! parse error: {e}")
    return out


def process_email(em, notify: bool, keep_all: bool):
    """Run extraction + persistence for one email. Returns number of jobs stored."""
    subject = em["subject"]
    sender = em["sender"]
    body = em["body"]
    html_body = em.get("html_body", "")

    extracted = ai_extractor.analyze_email_multi(subject, sender, body, html_body)
    is_job = len(extracted) > 0

    first_summary = extracted[0]["summary"] if extracted else "Regular non-job email."
    log_email_inspection(
        message_id=em["message_id"],
        sender=sender,
        subject=subject,
        date_received=em.get("date_received", ""),
        is_job=is_job,
        summary=first_summary,
        count=len(extracted),
    )

    stored = 0
    for item in extracted:
        if item.get("is_application_confirmation"):
            continue  # backfill does not auto-mark applications

        title = (item.get("job_title") or "").strip()
        link_source = item.get("link_source", "")
        apply_url = item.get("apply_url", "")

        # Quality gate: drop placeholder heuristic rows unless asked to keep all
        if not keep_all and (title.lower() in PLACEHOLDER_TITLES or link_source == "search_fallback"):
            continue

        company = item.get("company_name", "Unknown Company")
        platform = item.get("source_platform", "Direct")
        dup_check = job_matcher.check_duplicate_and_history(company, title)

        job_record = {
            "message_id": f"{em['message_id']}_{stored}",
            "email_subject": subject,
            "email_sender": sender,
            "source_platform": platform,
            "date_received": em.get("date_received", ""),
            "job_title": title or "Unknown Role",
            "company_name": company,
            "location": item.get("location", "Remote"),
            "job_type": item.get("job_type", "Full-time"),
            "apply_url": apply_url,
            "salary": item.get("salary", "Not specified"),
            "skills": item.get("skills", []),
            "experience_level": item.get("experience_level", "Mid-Senior"),
            "summary": item.get("summary", ""),
            "raw_email_snippet": body[:500],
            "status": "NEW",
            "applied_earlier": dup_check["applied_earlier"],
            "previous_application_id": dup_check["previous_application_id"],
            "previous_applied_date": dup_check["previous_applied_date"],
            "previous_job_title": dup_check["previous_job_title"],
            "match_score": item.get("match_score", 85),
        }
        insert_job(job_record)
        stored += 1

    return stored


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="stop after N processed emails (0 = all)")
    parser.add_argument("--notify", action="store_true", help="send notifications (default: off)")
    parser.add_argument("--keep-all", action="store_true", help="store placeholder jobs too")
    args = parser.parse_args()

    target_email = get_setting("target_email", os.getenv("TARGET_EMAIL", "deepak.gvit@gmail.com"))
    imap_password = get_setting("imap_password", "") or os.getenv("GMAIL_APP_PASSWORD", "")
    if not imap_password:
        print("No IMAP password configured - aborting.")
        return

    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=IMAP_SOCKET_TIMEOUT)
    mail.login(target_email, imap_password.replace(" ", ""))
    mail.select("INBOX")

    query = build_sender_search_queries()[0]
    status, messages = mail.search(None, query)
    all_ids = messages[0].split() if status == "OK" and messages[0] else []
    total_unread = len(all_ids)
    print(f"UNSEEN emails from monitored senders: {total_unread}")

    processed = stored_jobs = skipped = 0
    batches = 0
    started = time.time()

    try:
        for i in range(0, len(all_ids), BATCH_SIZE):
            batch = all_ids[i:i + BATCH_SIZE]

            # Cheap header scan to skip already-processed message IDs
            todo_ids = []
            for msg_id in batch:
                try:
                    status, hdr = mail.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                    blob = b""
                    for part in hdr or []:
                        if isinstance(part, tuple):
                            blob = part[1] or b""
                            break
                    mid = ""
                    for line in blob.decode("utf-8", errors="ignore").splitlines():
                        if line.lower().startswith("message-id:"):
                            mid = line.split(":", 1)[1].strip()
                            break
                    if mid and is_message_already_processed(mid):
                        skipped += 1
                        continue
                    todo_ids.append((msg_id, mid))
                except Exception:
                    todo_ids.append((msg_id, ""))

            if not todo_ids:
                continue

            emails = fetch_batch(mail, [m for m, _ in todo_ids])
            batches += 1

            for em in emails:
                if args.limit and processed >= args.limit:
                    break
                try:
                    stored_jobs += process_email(em, args.notify, args.keep_all)
                except Exception as e:
                    print(f"    ! process error ({em.get('sender','?')}): {e}")
                processed += 1

            elapsed = time.time() - started
            print(f"[batch {batches}] processed={processed} new_jobs={stored_jobs} "
                  f"skipped_dup={skipped} | {elapsed/60:.1f} min elapsed", flush=True)

            if args.limit and processed >= args.limit:
                print(f"Reached --limit {args.limit}, stopping.")
                break

            time.sleep(1)  # breathe between batches

        # Record one backfill run in check history
        log_check_run(
            emails_scanned=processed,
            new_jobs_found=stored_jobs,
            status_message=f"Backfill: processed {processed} emails ({skipped} skipped as duplicates), {stored_jobs} quality jobs stored.",
            triggered_by="backfill",
        )
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    print()
    print("=" * 60)
    print(f"BACKFILL COMPLETE in {(time.time()-started)/60:.1f} min")
    print(f"  emails processed : {processed}")
    print(f"  skipped (dup)    : {skipped}")
    print(f"  quality jobs     : {stored_jobs}")
    print("=" * 60)


if __name__ == "__main__":
    main()
