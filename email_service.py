import imaplib
import email
from email.header import decode_header
import logging
import random
import os
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from config import IMAP_SERVER, IMAP_PORT, GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE
from database import get_setting

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Realistic Job Alert Templates for LinkedIn, Naukri, Indeed, Glassdoor & Direct Recruiters
MULTI_JOB_SIMULATOR_TEMPLATES = [
    {
        "source": "LinkedIn",
        "sender": "jobalerts-noreply@linkedin.com",
        "subject": "LinkedIn Job Alerts: 3 new Lead Python and AI roles in Bengaluru / Remote",
        "body": """LinkedIn Job Alerts for Deepak

1. Senior Python / AI Architect at ScaleAI
Location: Remote (Global)
Link: https://www.linkedin.com/jobs/view/scaleai-senior-python-architect-991201
Skills: Python, FastAPI, PyTorch, LLMs, Docker

2. Lead Backend Engineer at Uber
Location: Bengaluru, Karnataka, India
Link: https://www.linkedin.com/jobs/view/uber-lead-backend-engineer-882319
Skills: Python, Go, High Scale Distributed Systems, Kafka

3. Machine Learning Platform Lead at Databricks
Location: Remote / Hybrid
Link: https://www.linkedin.com/jobs/view/databricks-ml-platform-lead-771234
Skills: Python, Spark, MLflow, Kubernetes, Cloud AI

Click on any job above to apply directly with 1-click apply on LinkedIn.""",
        "html_body": "<div><h2>LinkedIn Job Alerts</h2><a href='https://www.linkedin.com/jobs/view/scaleai-senior-python-architect-991201'>1. Senior Python / AI Architect at ScaleAI</a><br><a href='https://www.linkedin.com/jobs/view/uber-lead-backend-engineer-882319'>2. Lead Backend Engineer at Uber</a><br><a href='https://www.linkedin.com/jobs/view/databricks-ml-platform-lead-771234'>3. Machine Learning Platform Lead at Databricks</a></div>"
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
Apply: https://www.naukri.com/job-listings-swiggy-principal-ai-engineer-10101

2. Staff Python Developer at Flipkart
Company: Flipkart
Location: Bengaluru / Remote
Exp: 6-10 yrs | Salary: ₹45 - ₹60 LPA
Apply: https://www.naukri.com/job-listings-flipkart-staff-python-dev-20202

3. Technical Lead - Data & AI at Infosys
Company: Infosys
Location: Hyderabad / Pune
Exp: 7-11 yrs | Salary: ₹32 - ₹45 LPA
Apply: https://www.naukri.com/job-listings-infosys-tech-lead-ai-30303

Regards,
Naukri Jobseeker Services""",
        "html_body": "<div><h2>Naukri FastForward Alerts</h2><a href='https://www.naukri.com/job-listings-swiggy-principal-ai-engineer-10101'>Swiggy - Principal AI Engineer</a><a href='https://www.naukri.com/job-listings-flipkart-staff-python-dev-20202'>Flipkart - Staff Python Dev</a></div>"
    },
    {
        "source": "Indeed",
        "sender": "donotreply@jobalert.indeed.com",
        "subject": "Indeed Job Alert: Senior Python Engineer & AI Solutions",
        "body": """Indeed Job Alert for Deepak Behera

1. Senior Cloud Backend Engineer - Stripe
Location: Remote
Salary: $170,000 - $215,000 a year
View and Apply: https://www.indeed.com/viewjob?jk=stripe-cloud-backend-991

2. Full Stack AI Engineer - Anthropic
Location: San Francisco, CA / Remote
Salary: $200,000 - $260,000 a year
View and Apply: https://www.indeed.com/viewjob?jk=anthropic-ai-engineer-882

Easily apply with your Indeed resume.""",
        "html_body": "<div><h2>Indeed Alerts</h2><a href='https://www.indeed.com/viewjob?jk=stripe-cloud-backend-991'>Senior Cloud Backend Engineer - Stripe</a></div>"
    },
    {
        "source": "Glassdoor",
        "sender": "noreply@glassdoor.com",
        "subject": "Glassdoor Jobs: 2 New Openings Matching Your Profile",
        "body": """Glassdoor Job Alert:

1. Lead Software Engineer at Razorpay
Rating: 4.4 ★ | Location: Bengaluru
Estimated Salary: ₹40L - ₹55L
Apply: https://www.glassdoor.com/job-listing/razorpay-lead-software-engineer-1122

2. Principal Architect at Atlassian
Rating: 4.6 ★ | Location: Remote (India)
Estimated Salary: ₹60L - ₹80L
Apply: https://www.glassdoor.com/job-listing/atlassian-principal-architect-3344

Visit Glassdoor for company reviews and salary insights.""",
        "html_body": "<div><h2>Glassdoor Jobs</h2><a href='https://www.glassdoor.com/job-listing/razorpay-lead-software-engineer-1122'>Lead Software Engineer at Razorpay</a></div>"
    }
]

class EmailService:
    def __init__(self):
        pass

    def fetch_recent_emails(self) -> List[Dict[str, Any]]:
        """
        Fetches new/recent emails including targeted search on:
        - LinkedIn (jobalerts-noreply@linkedin.com)
        - Indeed (donotreply@jobalert.indeed.com)
        - Naukri (*@naukri.com)
        - Glassdoor (noreply@glassdoor.com)
        - General INBOX unread emails
        """
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
            mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            mail.login(email_address, clean_pw)
            mail.select("INBOX")

            # Collect IDs from unread + specific job board senders
            search_queries = [
                'UNSEEN',
                '(FROM "jobalerts-noreply@linkedin.com")',
                '(FROM "donotreply@jobalert.indeed.com")',
                '(FROM "naukri.com")',
                '(FROM "glassdoor.com")'
            ]

            all_msg_ids = set()
            for sq in search_queries:
                status, messages = mail.search(None, sq)
                if status == 'OK' and messages[0]:
                    ids = messages[0].split()
                    # Take latest 10 from each category
                    for i in ids[-10:]:
                        all_msg_ids.add(i)

            if not all_msg_ids:
                status, messages = mail.search(None, 'ALL')
                if status == 'OK' and messages[0]:
                    all_ids = messages[0].split()
                    all_msg_ids = set(all_ids[-10:])

            for msg_id in all_msg_ids:
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

                        # Extract text and html
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
                            "body": body[:6000],
                            "html_body": html_body[:8000]
                        })

            mail.logout()
            logger.info(f"Successfully fetched {len(emails_list)} emails via IMAP.")
            return emails_list

        except Exception as e:
            logger.error(f"Error fetching emails via IMAP: {e}")
            return []

    def _fetch_via_oauth(self, target_email: str) -> List[Dict[str, Any]]:
        """Fetches emails using official Google Gmail API client."""
        try:
            if not os.path.exists(GMAIL_CREDENTIALS_FILE) and not os.path.exists(GMAIL_TOKEN_FILE):
                return []
            
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), ["https://www.googleapis.com/auth/gmail.readonly"])
            service = build('gmail', 'v1', credentials=creds)

            # Targeted query
            query = "is:unread OR from:linkedin.com OR from:naukri.com OR from:indeed.com OR from:glassdoor.com"
            results = service.users().messages().list(userId='me', maxResults=10, q=query).execute()
            messages = results.get('messages', [])

            emails_list = []
            for m in messages:
                msg = service.users().messages().get(userId='me', id=m['id']).execute()
                headers = msg['payload']['headers']
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "No Subject")
                sender = next((h['value'] for h in headers if h['name'] == 'From'), "Unknown")
                snippet = msg.get('snippet', '')
                
                emails_list.append({
                    "message_id": m['id'],
                    "subject": subject,
                    "sender": sender,
                    "date_received": datetime.now().isoformat(),
                    "body": snippet,
                    "html_body": ""
                })
            return emails_list
        except Exception as e:
            logger.error(f"Error in Gmail API OAuth fetch: {e}")
            return []

    def _fetch_simulated_emails(self) -> List[Dict[str, Any]]:
        """Generates realistic sample multi-job email digests from LinkedIn, Naukri, Indeed, Glassdoor."""
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
        """Creates a single specific simulated digest email for manual UI testing."""
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
