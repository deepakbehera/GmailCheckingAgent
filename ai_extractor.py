import re
import json
import logging
import urllib.parse
from typing import Dict, Any, List, Optional
from config import GEMINI_API_KEY, DEFAULT_GEMINI_MODEL
from database import get_setting

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Sender-to-Platform Routing ---
# Exact senders mapped to dashboard platform buckets.
SENDER_PLATFORM_MAP = [
    ("jobmessenger@monsterindia.com", "Monster"),
    ("jobs-noreply@linkedin.com", "LinkedIn"),
    ("jobalerts-noreply@linkedin.com", "LinkedIn"),
    ("donotreply@jobalert.indeed.com", "Indeed"),
    ("noreply@glassdoor.com", "Glassdoor"),
    ("do-not-reply@roku.com", "Direct"),
    ("aditi@talent500.co", "Direct"),
]

def detect_platform_from_sender(sender: str) -> Optional[str]:
    """Maps a sender email address to a platform bucket; returns None if unknown."""
    s = (sender or "").lower()
    for key, platform in SENDER_PLATFORM_MAP:
        if key in s:
            return platform
    if "@naukri." in s or s.endswith("@naukri.com"):
        return "Naukri"
    if "monster" in s:
        return "Monster"
    if "talent500" in s:
        return "Direct"
    return None

JOB_KEYWORDS = [
    "job opening", "job opportunity", "hiring", "role", "position", "career",
    "apply now", "job alert", "vacancy", "we are hiring", "interview", "application",
    "developer", "engineer", "manager", "lead", "architect", "analyst", "specialist",
    "recommended jobs", "jobs matching your search", "jobs in your network"
]

APPLICATION_CONFIRM_KEYWORDS = [
    "thank you for applying", "application received", "we received your application",
    "application confirmed", "next steps with your application", "interview scheduled",
    "status of your application"
]

def make_working_url(title: str, company: str, platform: str = "Direct") -> str:
    """FALLBACK ONLY - used when no real job link exists in the email body."""
    query = urllib.parse.quote(f"{title} {company}")
    p = platform.lower()
    if "indeed" in p:
        return f"https://www.indeed.com/jobs?q={query}"
    elif "linkedin" in p:
        return f"https://www.linkedin.com/jobs/search/?keywords={query}"
    elif "naukri" in p:
        return f"https://www.naukri.com/jobs-in-india?keywords={query}"
    elif "glassdoor" in p:
        return f"https://www.glassdoor.com/Job/jobs.htm?sc.keyword={query}"
    return f"https://www.google.com/search?q=Apply+{query}"

def detect_platform_source(sender: str, subject: str, body: str) -> str:
    """Platform detection. Sender-based detection wins so recruitment mail from one
    platform is never mis-bucketed because the body happens to mention another site."""
    sender_platform = detect_platform_from_sender(sender)
    if sender_platform:
        return sender_platform

    s_low = sender.lower()
    full_low = f"{sender} {subject}".lower()
    body_low = (body or "").lower()

    if "linkedin" in s_low or "linkedin.com" in full_low:
        return "LinkedIn"
    if "naukri" in s_low or "naukri.com" in full_low:
        return "Naukri"
    if "indeed" in s_low or "indeed.com" in full_low:
        return "Indeed"
    if "glassdoor" in s_low or "glassdoor.com" in full_low:
        return "Glassdoor"
    if "monster" in s_low or "monsterindia" in full_low:
        return "Monster"
    # Body mentions only count for job-site links, not generic web references
    for domain, platform in [
        ("linkedin.com/jobs", "LinkedIn"),
        ("naukri.com", "Naukri"),
        ("indeed.com", "Indeed"),
        ("glassdoor.com", "Glassdoor"),
        ("monsterindia.com", "Monster"),
    ]:
        if domain in body_low:
            return platform
    return "Direct"

# --- Real Job-Link Extraction from Email Body ---

# Marketing/tracking or non-job links that must never become an apply_url
_JUNK_URL_PATTERNS = [
    "unsubscribe", "privacy", "terms", "help.", "support.", "footer",
    "preferences", "settings", "blog.", "campaign", "utm_",
    "googleusercontent", "gstatic", "google-analytics", "doubleclick",
    "facebook.", "twitter.", "x.com", "youtube.", "instagram.",
    "mailto:", "tel:", "play.google", "apps.apple", "itunes.apple",
]

# Known GENERATED SEARCH URLs (never real single-job postings). These must be
# excluded so the dashboard only opens links that point to an actual job.
_SEARCH_URL_PATTERNS = [
    "linkedin.com/jobs/search/?keywords=",
    "indeed.com/jobs?q=",
    "naukri.com/jobs-in-india?keywords=",
    "glassdoor.com/job/jobs.htm?sc.keyword=",
    "glassdoor.com/jobs.htm?sc.keyword=",
    "google.com/search?q=apply+",
]

def extract_real_job_links(html_body: str, text_body: str, platform: str) -> List[Dict[str, Any]]:
    """Extracts actual job posting URLs (with their anchor text) directly from the
    email body so the dashboard opens the REAL job, not a search page."""
    combined = (html_body or "") + "\n" + (text_body or "")
    if not combined.strip():
        return []

    # Collect (url, anchor_text) pairs from both <a href> tags and bare URLs
    candidates = []
    for match in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html_body or "", re.IGNORECASE | re.DOTALL):
        url = match.group(1).strip()
        anchor = re.sub(r'<[^>]+>', ' ', match.group(2))
        anchor = re.sub(r'\s+', ' ', anchor).strip()
        candidates.append((url, anchor))
    for match in re.finditer(r'https?://[^\s<>"\')]+', text_body or ""):
        candidates.append((match.group(0).rstrip('.,;)"\''), ""))

    # Platform job-URL patterns: these are genuine posting links, not search pages
    platform_url_patterns = [
        r'linkedin\.com/(?:jobs/view|jobs/collections|learning/jobs)/[^\s"\'<>]+',
        r'naukri\.com/joblisting[^\s"\'<>]*',
        r'naukri\.com/[a-z0-9-]+-jobs-[^\s"\'<>]+',
        r'indeed\.com/(?:viewjob|job|cmp/[^/]+/jobs/)[^\s"\'<>]*',
        r'glassdoor\.com/job-listing[^\s"\'<>]+',
        r'monsterindia\.com/job-search/[a-z0-9_-]+[^\s"\'<>]*',
        r'monster\.com/job-openings/[^\s"\'<>]+',
        r'roku\.com/(?:careers?|jobs?)/[^\s"\'<>]+',
        r'talent500\.co/(?:jobs?|open-roles?|careers?)/[^\s"\'<>]+',
        r'jobalert\.indeed\.com/(?:email|alerts?)/[^\s"\'<>]+',
        r'glassdoor\.com/partner/jobListing\.htm[^\s"\'<>]+',
    ]
    pattern_re = re.compile('|'.join(f'(?:{p})' for p in platform_url_patterns), re.IGNORECASE)

    # Generic posting keywords that can qualify a URL on any (company ATS) domain
    generic_keyword_re = re.compile(
        r'(?:/jobs?/|/careers?/|/job-detail|/jobdetails?/|jobdetail|/openings?/|/requisitions?/|/position/|/posting/)',
        re.IGNORECASE
    )

    def is_junk(u: str) -> bool:
        u_low = u.lower()
        if any(p in u_low for p in _JUNK_URL_PATTERNS):
            return True
        # Exclude generated platform SEARCH pages - the user wants real job links only
        if any(p in u_low for p in _SEARCH_URL_PATTERNS):
            return True
        return False

    seen = set()
    results = []
    for url, anchor in candidates:
        if not url.startswith('http') or is_junk(url):
            continue
        if url in seen:
            continue
        is_platform_job = pattern_re.search(url) is not None
        if not is_platform_job and not generic_keyword_re.search(url):
            continue
        seen.add(url)
        results.append({"url": url, "anchor_text": anchor or ""})
    return results

def titleize_anchor(text: str) -> str:
    """Cleans an anchor text for use as a job title (max ~90 chars)."""
    if not text:
        return ""
    t = re.sub(r'\s+', ' ', text).strip()
    if len(t) > 90:
        t = t[:90].rsplit(' ', 1)[0]
    return t

def _clean_company_name(name: str) -> str:
    """Strips trailing noise the plain-text regexes tend to capture into company
    names (e.g. 'Stripe\nView and Apply', 'Stripe View job')."""
    if not name:
        return ""
    t = re.sub(r'\s+', ' ', name).strip()
    t = re.split(r'\s+(?:View|Apply|Location|Salary|Exp|Rating|Estimated)\b', t, flags=re.IGNORECASE)[0]
    return t.strip(' \t-–|,')

def dedupe_jobs(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cleans captured fields and removes duplicate (title, company) extractions
    that arise when the same job appears both in the plain-text body and the HTML anchors."""
    seen = set()
    unique = []
    for j in jobs:
        company = _clean_company_name(j.get('company_name', ''))
        if company:
            j['company_name'] = company
        title = re.sub(r'\s+', ' ', j.get('job_title', '')).strip()
        j['job_title'] = title
        key = (
            re.sub(r'[^a-z0-9]', '', title.lower()),
            re.sub(r'[^a-z0-9]', '', company.lower()),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(j)
    return unique

class AIExtractor:
    def __init__(self):
        pass

    def get_api_key(self) -> str:
        key = get_setting("gemini_api_key", "").strip()
        if not key:
            key = GEMINI_API_KEY
        return key

    def analyze_email_multi(self, subject: str, sender: str, body: str, html_body: str = "") -> List[Dict[str, Any]]:
        platform = detect_platform_source(sender, subject, body + " " + html_body)
        api_key = self.get_api_key()

        if api_key:
            try:
                extracted_jobs = self._analyze_multi_with_gemini(subject, sender, body, html_body, platform, api_key)
                if extracted_jobs:
                    self._enrich_jobs_with_real_links(extracted_jobs, html_body, body, platform)
                    return extracted_jobs
            except Exception as e:
                logger.warning(f"Gemini multi-job analysis failed: {e}")

        jobs = self._heuristic_multi_analysis(subject, sender, body, html_body, platform)
        jobs = dedupe_jobs(jobs)
        self._enrich_jobs_with_real_links(jobs, html_body, body, platform)
        return jobs

    def _enrich_jobs_with_real_links(self, jobs: List[Dict[str, Any]], html_body: str, body: str, platform: str):
        """Matches extracted jobs to real links found in the email body.

        Priority: a real posting URL from the email always wins. A generated
        platform search URL is only used as a last-resort fallback."""
        if not jobs:
            return

        real_links = extract_real_job_links(html_body, body, platform)
        used_links = set()

        for j in jobs:
            job_url = (j.get("apply_url") or "").strip()
            is_generated_fallback = (
                not job_url.startswith("http")
                or any(f"{dom}" in job_url for dom in [
                    "linkedin.com/jobs/search/?keywords=",
                    "indeed.com/jobs?q=",
                    "naukri.com/jobs-in-india?keywords=",
                    "glassdoor.com/Job/jobs.htm?sc.keyword=",
                    "google.com/search?q=Apply+",
                ])
            )

            if not is_generated_fallback:
                # AI/heuristic already found a genuine-looking posting URL - keep it
                j["apply_url"] = job_url
                j["link_source"] = "email"
                continue

            # Match the job to a real link from the email (by anchor/company/title text)
            title_low = f"{j.get('job_title', '')} {j.get('company_name', '')}".lower()
            tokens = [t for t in re.split(r'[^a-z0-9]+', title_low) if len(t) > 2]

            best = None
            best_score = 0
            for link in real_links:
                if link["url"] in used_links:
                    continue
                hay = f"{link['anchor_text']} {link['url']}".lower()
                score = sum(1 for tok in set(tokens) if tok in hay)
                if score > best_score:
                    best_score = score
                    best = link

            if best and best_score >= 1:
                j["apply_url"] = best["url"]
                j["link_source"] = "email"
                used_links.add(best["url"])
                anchor_title = titleize_anchor(best["anchor_text"])
                if anchor_title and (not j.get("job_title") or j.get("job_title") in ("Specialist Role", "Unknown Role")):
                    j["job_title"] = anchor_title
            else:
                # No real link matched: fall back to platform search link
                j["apply_url"] = make_working_url(
                    j.get("job_title", "Engineer"),
                    j.get("company_name", "Company"),
                    j.get("source_platform", platform)
                )
                j["link_source"] = "search_fallback"

    def _analyze_multi_with_gemini(self, subject: str, sender: str, body: str, html_body: str, platform: str, api_key: str) -> Optional[List[Dict[str, Any]]]:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            prompt = f"""
You are an expert AI Job Hunter Assistant.
Analyze the following email from {platform} ({sender}).
Extract ALL individual job openings mentioned in the email into a list of jobs.

IMPORTANT RULES for "apply_url":
- Use the EXACT link found in the email that points to the specific job posting (e.g. linkedin.com/jobs/view/..., indeed.com/viewjob/..., naukri.com/joblisting/..., glassdoor.com/job-listing/..., monsterindia.com job links, or the company's own careers page).
- NEVER invent or generate a search URL. If the email has no direct posting link, leave apply_url as an empty string.

Subject: {subject}
Sender: {sender}
Body & Links:
{(html_body or body)[:4500]}

Return a strict JSON object with this schema:
{{
  "is_job_email": boolean,
  "is_application_confirmation": boolean,
  "jobs": [
    {{
      "job_title": string,
      "company_name": string,
      "location": string,
      "job_type": string (e.g. "Full-time", "Remote", "Hybrid"),
      "apply_url": string (the specific direct job posting link copied exactly from the email, or empty string),
      "salary": string,
      "skills": array of strings,
      "experience_level": string,
      "summary": string (concise 1-2 sentence overview),
      "match_score": integer between 60 and 99
    }}
  ]
}}
If no jobs are present, return {{"is_job_email": false, "jobs": []}}.
Do NOT output backticks outside the JSON. Return only the raw JSON.
"""
            response = client.models.generate_content(
                model=DEFAULT_GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json"
                )
            )

            if response and response.text:
                parsed = json.loads(response.text)
                if parsed.get("is_job_email") and parsed.get("jobs"):
                    for j in parsed["jobs"]:
                        j["source_platform"] = platform
                        j["is_application_confirmation"] = parsed.get("is_application_confirmation", False)
                    return parsed["jobs"]

        except Exception as e:
            logger.error(f"Error in _analyze_multi_with_gemini: {e}")
        return None

    def _heuristic_multi_analysis(self, subject: str, sender: str, body: str, html_body: str, platform: str) -> List[Dict[str, Any]]:
        full_text = f"{subject} {sender} {body}".lower()
        combined_body = body + "\n" + html_body

        is_confirmation = any(phrase in full_text for phrase in APPLICATION_CONFIRM_KEYWORDS)

        negative_indicators = ["your receipt", "order confirmation", "one-time password", "otp", "statement is ready"]
        if any(neg in full_text for neg in negative_indicators):
            return []

        jobs = []

        # 1. LinkedIn
        if platform == "LinkedIn":
            job_blocks = re.findall(r'(?:[0-9]+\.|\*|\•)?\s*([A-Za-z0-9\s/+-]+?(?:Engineer|Developer|Manager|Architect|Analyst|Scientist|Lead|Consultant))\s+(?:at|@|with|-)\s+([A-Za-z0-9\s&.,]+?)(?:\s+in|\s+Location:|\s*\(|\n|$)', combined_body, re.IGNORECASE)
            if job_blocks:
                for idx, (title, comp) in enumerate(job_blocks[:8]):
                    c_title = title.strip().title()
                    c_comp = comp.strip()
                    # Real posting URL is attached later by _enrich_jobs_with_real_links;
                    # leave empty here so the email link is preferred.
                    jobs.append({
                        "job_title": c_title,
                        "company_name": c_comp,
                        "location": "Remote / Bengaluru, India",
                        "job_type": "Full-time",
                        "apply_url": "",
                        "salary": "Competitive",
                        "skills": ["Python", "Cloud", "Distributed Systems"],
                        "experience_level": "Mid-Senior",
                        "summary": f"LinkedIn Alert: {c_title} opportunity at {c_comp}.",
                        "source_platform": "LinkedIn",
                        "match_score": 88,
                        "is_application_confirmation": is_confirmation
                    })

        # 2. Naukri
        elif platform == "Naukri":
            naukri_blocks = re.findall(r'(?:Role:|Job Title:)?\s*([A-Za-z0-9\s/+-]+?(?:Engineer|Developer|Manager|Architect|Analyst|Scientist|Lead|Specialist))\s*(?:Company:|-|at)\s*([A-Za-z0-9\s&.,]+?)(?:\s*Location:|\s*Exp:|\n|$)', combined_body, re.IGNORECASE)
            if naukri_blocks:
                for idx, (title, comp) in enumerate(naukri_blocks[:8]):
                    c_title = title.strip().title()
                    c_comp = comp.strip()
                    jobs.append({
                        "job_title": c_title,
                        "company_name": c_comp,
                        "location": "Bengaluru / Hyderabad / Remote",
                        "job_type": "Full-time",
                        "apply_url": "",
                        "salary": "₹25 - ₹45 LPA",
                        "skills": ["Python", "FastAPI", "SQL", "AWS"],
                        "experience_level": "Senior (5-8 yrs)",
                        "summary": f"Naukri Job Recommendation: {c_title} at {c_comp}.",
                        "source_platform": "Naukri",
                        "match_score": 85,
                        "is_application_confirmation": is_confirmation
                    })

        # 3. Indeed
        elif platform == "Indeed":
            indeed_blocks = re.findall(r'([A-Za-z0-9\s/+-]+?(?:Engineer|Developer|Manager|Architect|Analyst|Scientist|Lead))\s*[-–]\s*([A-Za-z0-9\s&.,]+)', combined_body, re.IGNORECASE)
            if indeed_blocks:
                for idx, (title, comp) in enumerate(indeed_blocks[:8]):
                    c_title = title.strip().title()
                    c_comp = comp.strip()
                    jobs.append({
                        "job_title": c_title,
                        "company_name": c_comp,
                        "location": "Remote / Hybrid",
                        "job_type": "Full-time",
                        "apply_url": "",
                        "salary": "$130k - $175k",
                        "skills": ["Python", "Microservices", "REST APIs"],
                        "experience_level": "Senior",
                        "summary": f"Indeed Job Alert: {c_title} at {c_comp}.",
                        "source_platform": "Indeed",
                        "match_score": 84,
                        "is_application_confirmation": is_confirmation
                    })

        # 4. Glassdoor
        elif platform == "Glassdoor":
            gd_blocks = re.findall(r'([A-Za-z0-9\s/+-]+?(?:Engineer|Developer|Manager|Architect|Analyst|Scientist|Lead))\s*at\s*([A-Za-z0-9\s&.,]+)', combined_body, re.IGNORECASE)
            if gd_blocks:
                for idx, (title, comp) in enumerate(gd_blocks[:8]):
                    c_title = title.strip().title()
                    c_comp = comp.strip()
                    jobs.append({
                        "job_title": c_title,
                        "company_name": c_comp,
                        "location": "Remote / Bengaluru",
                        "job_type": "Full-time",
                        "apply_url": "",
                        "salary": "₹30 - ₹50 LPA",
                        "skills": ["Python", "Machine Learning", "System Design"],
                        "experience_level": "Senior",
                        "summary": f"Glassdoor Job Alert: {c_title} at {c_comp}.",
                        "source_platform": "Glassdoor",
                        "match_score": 86,
                        "is_application_confirmation": is_confirmation
                    })

        # 5. Monster (jobmessenger@monsterindia.com digests)
        elif platform == "Monster":
            monster_blocks = re.findall(r'([A-Za-z0-9\s/+-]+?(?:Engineer|Developer|Manager|Architect|Analyst|Scientist|Lead|Specialist))\s*(?:at|@|-|–)\s*([A-Za-z0-9\s&.,]+?)(?:\s*Location:|\s*Exp:|\s*Salary:|\n|$)', combined_body, re.IGNORECASE)
            if monster_blocks:
                for idx, (title, comp) in enumerate(monster_blocks[:8]):
                    c_title = title.strip().title()
                    c_comp = comp.strip()
                    jobs.append({
                        "job_title": c_title,
                        "company_name": c_comp,
                        "location": "India",
                        "job_type": "Full-time",
                        "apply_url": "",
                        "salary": "Not specified",
                        "skills": ["Python", "SQL", "Cloud"],
                        "experience_level": "Mid-Senior",
                        "summary": f"Monster Job Messenger Alert: {c_title} at {c_comp}.",
                        "source_platform": "Monster",
                        "match_score": 83,
                        "is_application_confirmation": is_confirmation
                    })

        # 6. Direct (recruiters like aditi@talent500.co, careers@roku.com)
        if not jobs and any(k in full_text for k in JOB_KEYWORDS):
            company_name = "Tech Recruiter"
            match_at = re.search(r'(?:at|@|with)\s+([A-Z][A-Za-z0-9\s&]+?)(?:\s+[-–|,]|\s+is|\s+in|\s*$)', subject)
            if match_at:
                company_name = match_at.group(1).strip()
            elif "@" in sender:
                dom = re.search(r'@([a-zA-Z0-9-]+)\.', sender)
                if dom and dom.group(1).lower() not in ["gmail", "yahoo", "hotmail", "outlook"]:
                    company_name = dom.group(1).capitalize()

            job_title = "Specialist Role"
            title_pat = re.search(r'([A-Z][a-zA-Z0-9\s]+(?:Engineer|Developer|Manager|Architect|Analyst|Consultant|Scientist|Lead|Designer))', subject, re.IGNORECASE)
            if title_pat:
                job_title = title_pat.group(1).strip().title()

            jobs.append({
                "job_title": job_title,
                "company_name": company_name,
                "location": "Remote / Bengaluru",
                "job_type": "Full-time",
                "apply_url": "",
                "salary": "Competitive",
                "skills": ["Python", "FastAPI", "Cloud"],
                "experience_level": "Senior",
                "summary": f"Direct job opportunity for {job_title} at {company_name}.",
                "source_platform": platform,
                "match_score": 90,
                "is_application_confirmation": is_confirmation
            })

        return jobs

ai_extractor = AIExtractor()
