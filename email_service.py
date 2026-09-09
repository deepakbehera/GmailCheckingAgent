import imaplib
import email
from email.header import decode_header
import logging
import random
import os
import json
import urllib.parse
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from config import IMAP_SERVER, IMAP_PORT, GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE
from database import get_setting, is_message_already_processed

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Max emails processed per check cycle. Kept small so a cycle fits inside the
# Vercel serverless timeout; the 15-min scheduler (or repeated manual checks)
# gradually works through the inbox backlog. Already-fetched mail is marked
# read by Gmail, so each cycle naturally checkpoints forward.
MAX_EMAILS_PER_CYCLE = 5
IMAP_SOCKET_TIMEOUT = 20  # seconds; guards against hung IMAP connections

# Truncation caps for fetched email content. Marketing emails (Glassdoor
# digests, LinkedIn alerts) are HTML-heavy: job cards can start past position
# 10k and tracking-wrapped hrefs alone span >1.5k chars, so a mid-tag cut
# destroys the <a href=...> match entirely. Keep the caps generous (60k) so
# the full job section always survives; Postgres TEXT handles this easily.
MAX_BODY_CHARS = 20000
MAX_HTML_CHARS = 60000

def generate_working_apply_url(job_title: str, company_name: str, platform: str = "Direct") -> str:
    """Generates a guaranteed working, live search/apply URL for any role and company."""
    query = urllib.parse.quote(f"{job_title} {company_name}")
    p_low = platform.lower()
    if "indeed" in p_low:
        return f"https://www.indeed.com/jobs?q={query}"
    elif "linkedin" in p_low:
        return f"https://www.linkedin.com/jobs/search/?keywords={query}"
    elif "naukri" in p_low:
        return f"https://www.naukri.com/jobs-in-india?keywords={query}"
    elif "glassdoor" in p_low:
        return f"https://www.glassdoor.com/Job/jobs.htm?sc.keyword={query}"
    return f"https://www.google.com/search?q=Apply+{query}+Careers"

# --- Monitored Senders ---
# Every email from these senders is fetched, opened, read and scanned for job links.
# A leading '@' means: match ANY sender on that domain (e.g. *@naukri.com).
MONITORED_SENDERS = [
    "*@indeed.com",                     # Indeed job alerts (covers jobalert.indeed.com)
    "*@glassdoor.com",                  # Glassdoor job alerts
    "jobmessenger@monsterindia.com",    # Monster India job messenger
    "jobs-noreply@linkedin.com",        # LinkedIn job alerts
    "jobalerts-noreply@linkedin.com",   # LinkedIn job alerts (alternate sender)
    "do-not-reply@roku.com",            # Roku careers / job notifications
    "*@naukri.com",                     # ANY email from Naukri.com
    "*@foundit.in",                     # Foundit (formerly Monster) job alerts
    "*@hirist.tech",                    # Hirist job alerts
    "*@jobfeed.hirist.com",             # Hirist job feed digests
    "*@hackerearth.com",                # HackerEarth job/opportunity mails
    "aditi@talent500.co",               # Direct recruiter (Talent500)
]

def build_sender_search_queries() -> List[str]:
    """Builds IMAP search query for all monitored senders (domains match any
    address @domain), restricted to UNSEEN (new, not-yet-read) emails only.
    Emails that are already read are deliberately ignored."""
    or_parts = []
    for s in MONITORED_SENDERS:
        if s.startswith("*@"):
            or_parts.append(f'(FROM "@{s[2:]}")')
        else:
            or_parts.append(f'(FROM "{s}")')
    # IMAP OR is binary: fold list into nested OR chain
    query = or_parts[0]
    for part in or_parts[1:]:
        query = f"OR {query} {part}"
    # Only NEW / UNREAD emails: UNSEEN matches messages without the \\Seen flag
    return [f"UNSEEN {query}"]

# Realistic Multi-Job Alert Templates with 100% Working Live Links
MULTI_JOB_SIMULATOR_TEMPLATES = [
    {
        "source": "LinkedIn",
        "sender": "jobalerts-noreply@linkedin.com",
        "subject": "LinkedIn Job Alerts: 3 new Lead Python and AI roles in Bengaluru / Remote",
        "body": """LinkedIn Job Alerts for Deepak

1. Senior Python / AI Architect at ScaleAI
Location: Remote (Global)
Apply: https://www.linkedin.com/jobs/search/?keywords=Senior+Python+AI+Architect+ScaleAI
Skills: Python, FastAPI, PyTorch, LLMs, Docker

2. Lead Backend Engineer at Uber
Location: Bengaluru, Karnataka, India
Apply: https://www.linkedin.com/jobs/search/?keywords=Lead+Backend+Engineer+Uber+Bengaluru
Skills: Python, Go, High Scale Distributed Systems, Kafka

3. Machine Learning Platform Lead at Databricks
Location: Remote / Hybrid
Apply: https://www.linkedin.com/jobs/search/?keywords=Machine+Learning+Platform+Lead+Databricks
Skills: Python, Spark, MLflow, Kubernetes, Cloud AI

Click on any job above to apply directly on LinkedIn.""",
        "html_body": "<div><h2>LinkedIn Job Alerts</h2><a href='https://www.linkedin.com/jobs/search/?keywords=Senior+Python+AI+Architect+ScaleAI'>1. Senior Python / AI Architect at ScaleAI</a><br><a href='https://www.linkedin.com/jobs/search/?keywords=Lead+Backend+Engineer+Uber+Bengaluru'>2. Lead Backend Engineer at Uber</a><br><a href='https://www.linkedin.com/jobs/search/?keywords=Machine+Learning+Platform+Lead+Databricks'>3. Machine Learning Platform Lead at Databricks</a></div>"
    },
    {
        "source": "Naukri",
        "sender": "alerts@naukri.com",
        "subject": "Naukri Job Recommendation: 3 Openings for Python AI Architect & Tech Lead",
        "body": """Dear Deepak,

Matching jobs for your profile on Naukri.com:

1. Principal AI Engineer at Swiggy
Company: Swiggy
Location: Bengaluru
Exp: 8-12 yrs | Salary: ₹55 - ₹75 LPA
Apply: https://www.naukri.com/jobs-in-india?keywords=Principal+AI+Engineer+Swiggy

2. Staff Python Developer at Flipkart
Company: Flipkart
Location: Bengaluru / Remote
Exp: 6-10 yrs | Salary: ₹45 - ₹60 LPA
Apply: https://www.naukri.com/jobs-in-india?keywords=Staff+Python+Developer+Flipkart

3. Technical Lead - Data & AI at Infosys
Company: Infosys
Location: Hyderabad / Pune
Exp: 7-11 yrs | Salary: ₹32 - ₹45 LPA
Apply: https://www.naukri.com/jobs-in-india?keywords=Technical+Lead+AI+Infosys

Regards,
Naukri Jobseeker Services""",
        "html_body": "<div><h2>Naukri FastForward Alerts</h2><a href='https://www.naukri.com/jobs-in-india?keywords=Principal+AI+Engineer+Swiggy'>Swiggy - Principal AI Engineer</a><a href='https://www.naukri.com/jobs-in-india?keywords=Staff+Python+Developer+Flipkart'>Flipkart - Staff Python Dev</a></div>"
    },
    {
        "source": "Indeed",
        "sender": "donotreply@jobalert.indeed.com",
        "subject": "Indeed Job Alert: Senior Python Engineer & AI Solutions",
        "body": """Indeed Job Alert for Deepak Behera

1. Senior Cloud Backend Engineer - Stripe
Location: Remote
Salary: $170,000 - $215,000 a year
View and Apply: https://www.indeed.com/jobs?q=Senior+Cloud+Backend+Engineer+Stripe

2. Full Stack AI Engineer - Anthropic
Location: San Francisco, CA / Remote
Salary: $200,000 - $260,000 a year
View and Apply: https://www.indeed.com/jobs?q=Full+Stack+AI+Engineer+Anthropic

Easily apply with your Indeed resume.""",
        "html_body": "<div><h2>Indeed Alerts</h2><a href='https://www.indeed.com/jobs?q=Senior+Cloud+Backend+Engineer+Stripe'>Senior Cloud Backend Engineer - Stripe</a></div>"
    },
    {
        "source": "Glassdoor",
        "sender": "noreply@glassdoor.com",
        "subject": "Glassdoor Jobs: 2 New Openings Matching Your Profile",
        "body": """Glassdoor Job Alert:

1. Lead Software Engineer at Razorpay
Rating: 4.4 ★ | Location: Bengaluru
Estimated Salary: ₹40L - ₹55L
Apply: https://www.glassdoor.com/Job/jobs.htm?sc.keyword=Lead+Software+Engineer+Razorpay

2. Principal Architect at Atlassian
Rating: 4.6 ★ | Location: Remote (India)
Estimated Salary: ₹60L - ₹80L
Apply: https://www.glassdoor.com/Job/jobs.htm?sc.keyword=Principal+Architect+Atlassian

Visit Glassdoor for company reviews and salary insights.""",
        "html_body": "<div><h2>Glassdoor Jobs</h2><a href='https://www.glassdoor.com/Job/jobs.htm?sc.keyword=Lead+Software+Engineer+Razorpay'>Lead Software Engineer at Razorpay</a></div>"
    }
]

class EmailService:
    def __init__(self):
        pass

    def fetch_recent_emails(self) -> List[Dict[str, Any]]:
        auth_mode = get_setting("auth_mode", "simulator").lower()
        target_email = get_setting("target_email", "deepak.gvit@gmail.com")

        if auth_mode == "imap":
            password = get_setting("imap_password", "")
            if password:
                return self._fetch_via_imap(target_email, password)
            else:
                logger.warning("IMAP mode selected but App Password is empty. Falling back to simulator.")
                return self._fetch_simulated_emails()

        elif auth_mode == "oauth":
            return self._fetch_via_oauth(target_email)

        else:
            return self._fetch_simulated_emails()

    def _fetch_via_imap(self, email_address: str, app_password: str) -> List[Dict[str, Any]]:
        """Connects to Gmail via IMAP SSL and searches for job alert senders and unread messages."""
        emails_list = []
        try:
            clean_pw = app_password.replace(" ", "")
            mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT, timeout=IMAP_SOCKET_TIMEOUT)
            mail.login(email_address, clean_pw)
            mail.select("INBOX")

            # Search ONLY the monitored job-alert senders (no 'ALL' fallback:
            # the agent must not ingest personal or unrelated mail).
            search_queries = build_sender_search_queries()

            all_msg_ids = set()
            for sq in search_queries:
                status, messages = mail.search(None, sq)
                if status == 'OK' and messages[0]:
                    ids = messages[0].split()
                    # Process only the newest batch each cycle to stay within
                    # the serverless function time budget.
                    for i in ids[-MAX_EMAILS_PER_CYCLE:]:
                        all_msg_ids.add(i)

            if not all_msg_ids:
                logger.info("No new mail from monitored job senders in this cycle.")

            # Phase 1: cheap header-only scan (PEEK does not mark as read) to
            # skip already-processed messages without fetching full bodies.
            new_msg_ids = []
            for msg_id in sorted(all_msg_ids):
                try:
                    status, hdr_data = mail.fetch(msg_id, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
                    header_blob = b""
                    for part in hdr_data or []:
                        if isinstance(part, tuple):
                            header_blob = part[1] or b""
                            break
                    mid = ""
                    for line in header_blob.decode('utf-8', errors='ignore').splitlines():
                        if line.lower().startswith('message-id:'):
                            mid = line.split(':', 1)[1].strip()
                            break
                    if mid and is_message_already_processed(mid):
                        continue  # already scanned in a previous cycle
                    new_msg_ids.append(msg_id)
                except Exception as e:
                    logger.warning(f"Header scan failed for {msg_id}: {e}")
                    new_msg_ids.append(msg_id)

            if not new_msg_ids:
                logger.info(f"{len(all_msg_ids)} candidate email(s) already processed - nothing new.")

            for msg_id in new_msg_ids:
                status, msg_data = mail.fetch(msg_id, '(RFC822)')
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        
                        raw_subj = msg.get("Subject", "No Subject")
                        decoded_subj = ""
                        for part, encoding in decode_header(raw_subj):
                            if isinstance(part, bytes):
                                decoded_subj += part.decode(encoding or "utf-8", errors="ignore")
                            else:
                                decoded_subj += str(part)

                        sender = msg.get("From", "Unknown Sender")
                        date_received = msg.get("Date", datetime.now().isoformat())
                        message_id = msg.get("Message-ID", f"imap_{msg_id.decode()}_{datetime.now().timestamp()}")

                        body = ""
                        html_body = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                c_type = part.get_content_type()
                                c_disp = str(part.get("Content-Disposition"))
                                if "attachment" not in c_disp:
                                    payload = part.get_payload(decode=True)
                                    if payload:
                                        decoded = payload.decode("utf-8", errors="ignore")
                                        if c_type == "text/plain":
                                            body += decoded
                                        elif c_type == "text/html":
                                            html_body += decoded
                        else:
                            payload = msg.get_payload(decode=True)
                            if payload:
                                body = payload.decode("utf-8", errors="ignore")

                        emails_list.append({
                            "message_id": message_id,
                            "subject": decoded_subj.strip(),
                            "sender": sender,
                            "date_received": date_received,
                            "body": body[:MAX_BODY_CHARS],
                            "html_body": html_body[:MAX_HTML_CHARS]
                        })

            mail.logout()
            logger.info(f"Successfully fetched {len(emails_list)} emails via IMAP.")
            return emails_list

            mail.logout()
        except Exception as e:
            logger.error(f"Error fetching emails via IMAP: {e}")
        return emails_list

    def _fetch_via_oauth(self, target_email: str) -> List[Dict[str, Any]]:
        try:
            if not os.path.exists(GMAIL_CREDENTIALS_FILE) and not os.path.exists(GMAIL_TOKEN_FILE):
                return []
            
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), ["https://www.googleapis.com/auth/gmail.readonly"])
            service = build('gmail', 'v1', credentials=creds)

            # Build Gmail API query strictly from monitored job-alert senders,
            # restricted to unread (is:unread) emails only - read mail is ignored.
            from_terms = []
            for s in MONITORED_SENDERS:
                from_terms.append(f"from:{s[2:] if s.startswith('*@') else s}")
            query = "is:unread (" + " OR ".join(from_terms) + ")"
            results = service.users().messages().list(userId='me', maxResults=25, q=query).execute()
            messages = results.get('messages', [])

            emails_list = []
            for m in messages:
                msg = service.users().messages().get(userId='me', id=m['id']).execute()
                headers = msg['payload']['headers']
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "No Subject")
                sender = next((h['value'] for h in headers if h['name'] == 'From'), "Unknown")
                snippet = msg.get('snippet', '')
                
                # Pull full body (text + HTML) so real apply links can be extracted
                payload = msg.get('payload', {})
                body_text = ""
                html_text = ""

                def _walk_payload(part):
                    nonlocal body_text, html_text
                    mime = part.get('mimeType', '')
                    if part.get('body', {}).get('data'):
                        import base64
                        decoded = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')
                        if mime == 'text/plain':
                            body_text += decoded
                        elif mime == 'text/html':
                            html_text += decoded
                    for child in part.get('parts', []) or []:
                        _walk_payload(child)

                _walk_payload(payload)
                if not body_text and not html_text:
                    try:
                        full = service.users().messages().get(userId='me', id=m['id'], format='full').execute()
                        _walk_payload(full.get('payload', {}))
                    except Exception:
                        pass

                emails_list.append({
                    "message_id": m['id'],
                    "subject": subject,
                    "sender": sender,
                    "date_received": datetime.now().isoformat(),
                    "body": body_text[:MAX_BODY_CHARS] or snippet,
                    "html_body": html_text[:MAX_HTML_CHARS]
                })
            return emails_list
        except Exception as e:
            logger.error(f"Error in Gmail API OAuth fetch: {e}")
            return []

    def _fetch_simulated_emails(self) -> List[Dict[str, Any]]:
        chosen = random.sample(MULTI_JOB_SIMULATOR_TEMPLATES, random.randint(1, 2))
        results = []
        for item in chosen:
            ts = int(datetime.now().timestamp() * 1000)
            rand_id = f"sim_{item['source'].lower()}_{ts}_{random.randint(100, 999)}"
            results.append({
                "message_id": rand_id,
                "subject": item["subject"],
                "sender": item["sender"],
                "date_received": datetime.now().strftime("%a, %d %b %Y %H:%M:%S"),
                "body": item["body"],
                "html_body": item.get("html_body", "")
            })
        return results

    def create_single_simulated_job_email(self, platform_source: Optional[str] = None) -> Dict[str, Any]:
        if platform_source:
            matches = [t for t in MULTI_JOB_SIMULATOR_TEMPLATES if t["source"].lower() == platform_source.lower()]
            item = matches[0] if matches else random.choice(MULTI_JOB_SIMULATOR_TEMPLATES)
        else:
            item = random.choice(MULTI_JOB_SIMULATOR_TEMPLATES)

        ts = int(datetime.now().timestamp() * 1000)
        return {
            "message_id": f"sim_manual_{item['source'].lower()}_{ts}_{random.randint(100, 999)}",
            "subject": item["subject"],
            "sender": item["sender"],
            "date_received": datetime.now().strftime("%a, %d %b %Y %H:%M:%S"),
            "body": item["body"],
            "html_body": item.get("html_body", "")
        }

email_service = EmailService()
