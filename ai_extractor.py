import re
import json
import time
import logging
import urllib.parse
from typing import Dict, Any, List, Optional
from config import GEMINI_API_KEY, DEFAULT_GEMINI_MODEL
from database import get_setting

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Gemini 429 Quota Throttle -------------------------------------------
# The Gemini free tier allows very few requests per minute/day. When a call
# hits RESOURCE_EXHAUSTED (429), stop calling for a cooldown window that
# doubles with each breach (exponential backoff) and skip further calls while
# it lasts, so a single scan cannot burn the whole day's quota on retries.
# Degradation is graceful: link-based cards keep their anchor details and the
# heuristic extractor handles emails without AI enrichment.
_GEMINI_COOLDOWN_START_SECS = 30.0
_GEMINI_COOLDOWN_MAX_SECS = 3600.0  # never back off more than an hour
_QUOTA_STATE = {
    "cooldown_until": 0.0,  # epoch seconds when Gemini calls may resume
    "cooldown_secs": _GEMINI_COOLDOWN_START_SECS,  # next cooldown length
}


def _quota_error(e: Exception) -> bool:
    s = str(e).lower()
    return "429" in s or "resource_exhausted" in s or "quota" in s or "rate limit" in s


def _gemini_quota_in_cooldown() -> bool:
    return time.time() < _QUOTA_STATE["cooldown_until"]


def _gemini_report_quota_error(e: Exception) -> float:
    """Puts Gemini calls into cooldown with exponential backoff. Honors the
    API's 'Please retry in Ns' hint when present. Returns the wait length."""
    now = time.time()
    wait = _QUOTA_STATE["cooldown_secs"]
    m = re.search(r"retry\s*(?:in|after)?\s*([0-9]+(?:\.[0-9]+)?)\s*s", str(e), re.I)
    if m:  # hint from the API ('Please retry in 43.62s'); small buffer on top
        wait = max(wait, min(float(m.group(1)) + 2.0, _GEMINI_COOLDOWN_MAX_SECS))
    _QUOTA_STATE["cooldown_until"] = now + wait
    _QUOTA_STATE["cooldown_secs"] = min(wait * 2, _GEMINI_COOLDOWN_MAX_SECS)
    logger.warning(
        f"Gemini quota exhausted (429). Pausing AI calls for {wait:.0f}s "
        f"(backoff doubles up to {_GEMINI_COOLDOWN_MAX_SECS:.0f}s). "
        "Extraction continues without AI enrichment."
    )
    return wait


def _gemini_report_success() -> None:
    """A successful call resets the backoff ladder."""
    _QUOTA_STATE["cooldown_secs"] = _GEMINI_COOLDOWN_START_SECS
    _QUOTA_STATE["cooldown_until"] = 0.0
# --- End Gemini 429 Quota Throttle ----------------------------------------

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

# --- Click-Tracking Redirect Unwrapping ---
# Some platforms (e.g. Glassdoor "Just in at <Company>:" weekly digests) wrap
# EVERY link in a click-tracking redirect like
#   https://mail8.content.glassdoor.com/ls/click?upn=<opaque-encrypted>
# The real posting URL is only revealed by following the redirect, so we
# resolve a capped number of them per email (in parallel) and cache results
# across check cycles. Unresolved URLs keep their original form.

TRACKING_HOST_SUFFIXES = ("content.glassdoor.com",)
MAX_TRACKING_UNWRAPS_PER_EMAIL = 8
_TRACKING_RESOLVE_TIMEOUT = (5, 12)  # (connect, read) seconds

_tracking_url_cache: Dict[str, str] = {}


def _host_matches_tracking(url: str) -> bool:
    try:
        host = urllib.parse.urlsplit(url).netloc.lower()
    except Exception:
        return False
    return any(host == s or host.endswith("." + s) for s in TRACKING_HOST_SUFFIXES)


def _resolve_tracking_url(url: str) -> str:
    """Follows a click-tracking redirect and returns the real destination URL
    ('' when it cannot be resolved)."""
    try:
        import requests
        with requests.Session() as s:
            s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            # stream=True: stop once the final URL is known (no body download)
            resp = s.get(url, allow_redirects=True, timeout=_TRACKING_RESOLVE_TIMEOUT, stream=True)
            final = resp.url
            resp.close()
            if final.startswith("http") and not _host_matches_tracking(final):
                return final
    except Exception as e:
        logger.warning(f"Tracking redirect resolve failed ({url[:80]}...): {e}")
    return ""


def _unwrap_tracking_redirects(candidates: List[tuple]) -> List[tuple]:
    """Replaces tracking-wrapped URLs with their real destinations (best effort).
    Resolution runs in parallel and is capped so a check cycle stays fast."""
    todo = []
    for url, anchor in candidates:
        if _host_matches_tracking(url) and url not in _tracking_url_cache:
            todo.append(url)
    todo = list(dict.fromkeys(todo))[:MAX_TRACKING_UNWRAPS_PER_EMAIL]

    if todo:
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=4) as pool:
                resolved = list(pool.map(_resolve_tracking_url, todo))
            for u, real in zip(todo, resolved):
                if real:
                    _tracking_url_cache[u] = real
            # Keep the cache bounded (drop oldest entries beyond 512)
            if len(_tracking_url_cache) > 512:
                for k in list(_tracking_url_cache.keys())[:-256]:
                    _tracking_url_cache.pop(k, None)
        except Exception as e:
            logger.warning(f"Tracking unwrap pass failed: {e}")

    out = []
    for url, anchor in candidates:
        real = _tracking_url_cache.get(url)
        out.append((real, anchor) if real else (url, anchor))
    return out


def _company_from_subject(subject: str) -> str:
    """Extracts the hiring company from Glassdoor digest subjects.
    Handles the two weekly-digest formats:
      \"Just in at TestVagrant: This week's employee reviews and more\"
      \"Test Automation Engineer at Testunity and 6 more jobs in India for you. Apply Now\"
    Glassdoor digests mention the company ONLY in the subject (the anchors
    carry title/salary/location), so this fills the company field."""
    if not subject:
        return ""
    m = re.search(r'\bjust in at\s+(.+?)\s*:', subject, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r'\bnew jobs? at\s+(.+?)\s*[:!]', subject, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # "TITLE at COMPANY and N more jobs ..." -> COMPANY (shortest match before
    # the 'and N more jobs' marker so multi-word companies stay intact)
    m = re.search(r'\bat\s+(.+?)\s+and\s+\d+\s*\+?\s*more\s+jobs?', subject, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


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

def _decode_cts_indeed(url: str) -> str:
    """Decodes an Indeed 'cts.indeed.com/v3/<gzip+base64>' tracking redirect into
    the real destination URL it points at. Returns '' when undecodable."""
    try:
        import gzip as _gzip
        import base64 as _base64
        tail = url.split("/v3/", 1)[1]
        tail = urllib.parse.unquote(tail)
        pad = tail + "=" * (-len(tail) % 4)
        decoded = _gzip.decompress(_base64.urlsafe_b64decode(pad)).decode("utf-8", errors="replace")
        return decoded.strip()
    except Exception:
        return ""

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

    # Unwrap Indeed tracking redirects so the real posting URL is what gets
    # stored (and what the dashboard opens).
    unwrapped = []
    for url, anchor in candidates:
        if url.startswith("//cts.indeed.com/v3/"):
            url = "https:" + url
        if url.startswith(("https://cts.indeed.com/v3/", "http://cts.indeed.com/v3/")):
            real = _decode_cts_indeed(url)
            unwrapped.append((real, anchor) if real.startswith("http") else (url, anchor))
        else:
            unwrapped.append((url, anchor))
    candidates = unwrapped

    # Unwrap marketing click-tracking redirects (e.g. Glassdoor digests) so the
    # real posting URLs can be recognized by the platform patterns below.
    candidates = _unwrap_tracking_redirects(candidates)

    # Platform job-URL patterns: these are genuine posting links, not search pages
    platform_url_patterns = [
        r'linkedin\.com/(?:jobs/view|jobs/collections|learning/jobs|comm/jobs/view)/[^\s"\'<>]+',
        r'naukri\.com/joblisting[^\s"\'<>]*',
        r'naukri\.com/[a-z0-9-]+-jobs-[^\s"\'<>]+',
        r'naukri\.com/jd/job-listings?[^\s"\'<>]+',
        r'(?:my\.)?naukri\.com/(?:AL|msg)/[^\s"\'<>]+',
        r'indeed\.com/(?:viewjob|job|cmp/[^/]+/jobs/|rc/clk)[^\s"\'<>]*',
        r'glassdoor\.[a-z.]+/job-listing[^\s"\'<>]+',
        r'monsterindia\.com/job-search/[a-z0-9_-]+[^\s"\'<>]*',
        r'monster\.com/job-openings/[^\s"\'<>]+',
        r'roku\.com/(?:careers?|jobs?)/[^\s"\'<>]+',
        r'talent500\.co/(?:jobs?|open-roles?|careers?)/[^\s"\'<>]+',
        r'jobalert\.indeed\.com/(?:email|alerts?)/[^\s"\'<>]+',
        r'glassdoor\.[a-z.]+/partner/jobListing\.htm[^\s"\'<>]+',
    ]
    pattern_re = re.compile('|'.join(f'(?:{p})' for p in platform_url_patterns), re.IGNORECASE)

    # Generic posting keywords that can qualify a URL on any (company ATS) domain
    generic_keyword_re = re.compile(
        r'(?:/jobs?/|/careers?/|/job-detail|/jobdetails?/|jobdetail|/openings?/|/requisitions?/|/position/|/posting/)',
        re.IGNORECASE
    )

    def is_junk(u: str) -> bool:
        u_low = u.lower()
        # Exclude generated platform SEARCH pages - the user wants real job links only
        if any(p in u_low for p in _SEARCH_URL_PATTERNS):
            return True
        # Junk/marketing filter: only reject if the URL path (before '?') hits a
        # junk pattern. Real job links usually carry utm_/campaign TRACKING PARAMS
        # in the query string, which must NOT disqualify them.
        path_part = u_low.split('?', 1)[0]
        if any(p in path_part for p in _JUNK_URL_PATTERNS):
            return True
        # Glassdoor generated search/listing pages (any query string) are not
        # single jobs: /job/jobs.htm, /jobs.htm, ...-jobs-SRCH_KO... results
        if "glassdoor." in path_part and (re.search(r'(/job)?/jobs\.htm$', path_part) or "srch_" in path_part):
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

def parse_company_from_anchor(anchor: str) -> str:
    """Extracts the company name from a job link anchor text.

    Handles common email formats:
      'KPMG India Python Ai Testing KPMG India · Bengaluru, ...'  -> KPMG India
      'GE HealthCare Staff Automation & Verification Lead GE HealthCare · ...' -> GE HealthCare
      'Engineer Software - Java Full Stack at Empower' -> Empower
    """
    if not anchor:
        return ""
    t = re.sub(r'\s+', ' ', anchor).strip()

    # LinkedIn style: 'TITLE COMPANY · LOCATION ...' -> company = first group
    # after the title (they repeat the company name before the dot separator).
    if '·' in t:
        before_dot = t.split('·')[0].strip()
        words = before_dot.split()
        # The company name usually appears TWICE (start & end): e.g.
        # 'KPMG India Python Ai Testing KPMG India'. If the first N words
        # match the last N words (N=1..4), that repeated group is the company.
        for n in (4, 3, 2, 1):
            if len(words) < 2 * n:
                continue
            head = [w.lower().strip('&,') for w in words[:n]]
            tail = [w.lower().strip('&,') for w in words[-n:]]
            if head == tail:
                return ' '.join(words[:n])
        # No repetition: try known company suffixes at the end
        m = re.search(r'([A-Z][A-Za-z0-9&. ]*\s(?:India|Inc|Corp|LLC|Labs|Technologies|Solutions|Group|HealthCare|Digital|Software|Systems))\s*$', before_dot)
        if m:
            return m.group(1).strip()
        # Single trailing capitalized word that is not a job word
        job_words = {'engineer', 'developer', 'manager', 'lead', 'testing',
                     'staff', 'senior', 'junior', 'principal', 'architect',
                     'analyst', 'specialist', 'ai', 'python', 'automation',
                     'verification', 'software', 'intern', 'associate', 'ml'}
        if words and words[-1][0:1].isupper() and words[-1].lower().strip('&,') not in job_words:
            return words[-1]
        return ""

    # 'X at Y' / 'X - Y' patterns - but the tail must look like a company
    # (title case, short, not a location/salary/experience fragment)
    for sep in [' at ', ' – ', ' - ', ' | ']:
        idx = t.lower().rfind(sep)
        if idx > 0:
            tail = t[idx + len(sep):].strip().rstrip('·,')
            if 2 <= len(tail) <= 40 and not re.match(r'^(remote|hybrid|bengaluru|india|hyderabad|pune|mumbai|delhi|chennai|kolkata|gurgaon|noida|\d)', tail, re.IGNORECASE) and not re.search(r'\d', tail):
                return tail

    # Naukri style: 'TITLE LOCATION EXP SALARY SKILLS...' with no company
    # marker: fall back to empty (company stays generic).
    return ""

def build_jobs_from_real_links(real_links: List[Dict[str, Any]], platform: str, is_confirmation: bool, company_hint: str = "") -> List[Dict[str, Any]]:
    """Creates one job card per real posting link found in the email body.

    The apply_url IS the exact link copied from the email ('Copy Link Address'),
    and the title/company come from the link's anchor text - so the dashboard's
    Apply Online button opens the exact job page from the email."""
    jobs = []
    for link in real_links:
        url = link["url"]
        anchor = titleize_anchor(link.get("anchor_text", ""))
        company = parse_company_from_anchor(anchor)
        if not company and company_hint:
            company = company_hint
        title = anchor

        # Clean the title: strip trailing location/company fragments
        if title and '·' in title:
            title = title.split('·')[0].strip()
        if title and company:
            # Remove company name occurrences from the START and END (emails
            # often repeat the company: 'KPMG India Python Ai Testing KPMG India')
            comp_low = company.lower()
            while title.lower().startswith(comp_low):
                title = title[len(company):].strip(' -–|·,.')
            while title.lower().endswith(comp_low):
                title = title[:-len(company)].strip(' -–|·,.')
        # Trailing location / noise cleanup
        title = re.sub(r'\s+(?:·|\||-)?\s*(?:Hyderabad|Bengaluru|Bangalore|Pune|Mumbai|Delhi|Chennai|Hybrid|Remote|India)[\s,.0-9()\-–]*$', '', title, flags=re.IGNORECASE)
        title = re.sub(r'\s+\d+\s*-\s*\d+\s+years?\s*.*$', '', title, flags=re.IGNORECASE)
        title = re.sub(r'\s+\d+\.\d+\s*★.*$', '', title)
        title = re.sub(r'\s*\(Walk-In\)\s*$', '', title, flags=re.IGNORECASE)
        title = re.sub(r'\s+\d+L\s*-\s*\d+L.*$', '', title)
        # Glassdoor anchors look like:
        #   'TITLE CITY ₹5L - ₹10L ( Glassdoor Est. ) SKILL-CHIPS Full-time'
        # Cut the title at the first city token when a salary (₹) follows it,
        # then drop any '( Glassdoor ... )' block wherever it appears.
        city_cut = re.search(
            r'\s+(?:Bengaluru|Bangalore|Hyderabad|Pune|Mumbai|Delhi|Chennai|Kolkata|Gurgaon|Noida|NCR|Remote|Hybrid|India)\b(?=.*₹)',
            title, re.IGNORECASE)
        if city_cut and city_cut.start() > 3:
            title = title[:city_cut.start()]
        title = re.sub(r'\s*\(\s*Glassdoor[^)]*\)\s*', ' ', title, flags=re.IGNORECASE)
        title = re.sub(r'\s{2,}', ' ', title)
        title = title.replace('&amp;', '&').strip(' -–|·,')

        jobs.append({
            "job_title": title or "Job from Email",
            "company_name": company or "Company from Email",
            "location": "As per posting",
            "job_type": "Full-time",
            "apply_url": url,
            "salary": "Not specified",
            "skills": [],
            "experience_level": "Not specified",
            "summary": f"Direct job link extracted from {platform} email" + (f": {anchor}" if anchor else "."),
            "source_platform": platform,
            "match_score": 92,
            "is_application_confirmation": is_confirmation,
            "apply_url_real": True,
            "_anchor_text": anchor,
        })
    return jobs

def _clean_company_name(name: str) -> str:
    """Strips trailing noise the plain-text regexes tend to capture into company
    names (e.g. 'Stripe\nView and Apply', 'Stripe View job')."""
    if not name:
        return ""
    t = re.sub(r'\s+', ' ', name).strip()
    t = re.split(r'\s+(?:View|Apply|Location|Salary|Exp|Rating|Estimated)\b', t, flags=re.IGNORECASE)[0]
    # Digest subjects bleed into heuristic company names:
    # 'Testunity and 6 more jobs' / 'AGS Aluminum Alloy and 6 more jobs in India'
    t = re.split(r'\s+and\s+\d+\s*\+?\s*more\s+jobs?', t, flags=re.IGNORECASE)[0]
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

        # PRIMARY PATH: one job card per real posting link found in the email
        # body. The apply_url is the exact link address from the email, so the
        # dashboard's Apply Online button opens the actual job page. When a
        # Gemini key is available, titles/companies/summaries are enriched
        # without ever touching the exact apply URLs.
        real_links = extract_real_job_links(html_body, body, platform)
        if real_links:
            full_text = f"{subject} {sender} {body}".lower()
            is_confirmation = any(phrase in full_text for phrase in APPLICATION_CONFIRM_KEYWORDS)
            link_jobs = build_jobs_from_real_links(
                real_links, platform, is_confirmation,
                company_hint=_company_from_subject(subject)
            )

            if api_key:
                self._enrich_link_jobs_with_gemini(link_jobs, subject, sender, body, html_body, platform, api_key)

            return link_jobs

        # FALLBACK: no real links in the email - use Gemini full extraction if
        # available (its jobs without real links are filtered by the scheduler's
        # quality gate), otherwise heuristic analysis.
        if api_key:
            try:
                extracted_jobs = self._analyze_multi_with_gemini(subject, sender, body, html_body, platform, api_key)
                if extracted_jobs:
                    self._enrich_jobs_with_real_links(
                        extracted_jobs, html_body, body, platform,
                        company_hint=_company_from_subject(subject)
                    )
                    return extracted_jobs
            except Exception as e:
                logger.warning(f"Gemini multi-job analysis failed: {e}")

        jobs = self._heuristic_multi_analysis(subject, sender, body, html_body, platform)
        jobs = dedupe_jobs(jobs)
        self._enrich_jobs_with_real_links(
            jobs, html_body, body, platform,
            company_hint=_company_from_subject(subject)
        )
        return jobs

    def _enrich_link_jobs_with_gemini(self, jobs: List[Dict[str, Any]], subject: str, sender: str, body: str, html_body: str, platform: str, api_key: str):
        """One Gemini call per email to fill in proper job titles, companies,
        locations, salaries and summaries for the link-based cards. The exact
        apply URLs extracted from the email are NEVER modified."""
        if not jobs:
            return
        if _gemini_quota_in_cooldown():
            logger.info("Skipping Gemini enrichment: quota cooldown active.")
            return
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            links_payload = [
                {
                    "index": i,
                    "anchor_text": j.get("_anchor_text", "")[:200],
                    "url": j["apply_url"][:250],
                }
                for i, j in enumerate(jobs)
            ]
            prompt = f"""
You are a job-posting parser. The links below were extracted from a {platform} job-alert email.
For EACH link, use the anchor text, the URL structure and the email context to determine the real job details.

Email Subject: {subject[:200]}
Email Snippet: {(body or html_body)[:800]}

Links:
{json.dumps(links_payload, indent=1)}

Return ONLY raw JSON (no backticks) with this schema:
{{
  "jobs": [
    {{
      "index": <same index as input>,
      "job_title": string (concise real job title; do not include company or location in it),
      "company_name": string (company hiring; "Unknown" only if truly undeterminable),
      "location": string,
      "job_type": string,
      "salary": string ("Not specified" if absent),
      "skills": array of strings (empty if absent),
      "experience_level": string,
      "summary": string (one sentence, under 25 words)
    }}
  ]
}}
One entry per input link, same order, same count ({len(jobs)}).
"""
            response = client.models.generate_content(
                model=DEFAULT_GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            if response and response.text:
                parsed = json.loads(response.text)
                enriched = parsed.get("jobs", [])
                for item in enriched:
                    idx = item.get("index")
                    if not isinstance(idx, int) or not (0 <= idx < len(jobs)):
                        continue
                    j = jobs[idx]
                    # Enrich fields but NEVER the exact apply URL
                    for field in ("job_title", "company_name", "location", "job_type", "salary", "experience_level", "summary"):
                        val = item.get(field)
                        if isinstance(val, str) and val.strip() and val != "Unknown":
                            j[field] = val.strip()
                    skills = item.get("skills")
                    if isinstance(skills, list) and skills:
                        j["skills"] = [str(s) for s in skills][:8]
                    j["ai_enriched"] = True
                logger.info(f"Gemini enriched {len(enriched)} link-based job card(s)")
                _gemini_report_success()
        except Exception as e:
            if _quota_error(e):
                _gemini_report_quota_error(e)
            else:
                logger.warning(f"Gemini link enrichment failed (cards keep anchor-based details): {e}")

    def _enrich_jobs_with_real_links(self, jobs: List[Dict[str, Any]], html_body: str, body: str, platform: str, company_hint: str = ""):
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
            # Fill the company from the digest subject when the AI/heuristics
            # could not determine it (Glassdoor weekly digests mention the
            # company only in the subject line).
            if company_hint and j.get("company_name", "") in ("", "Unknown Company", "Company from Email"):
                j["company_name"] = company_hint
            else:
                # No real link matched: fall back to platform search link
                j["apply_url"] = make_working_url(
                    j.get("job_title", "Engineer"),
                    j.get("company_name", "Company"),
                    j.get("source_platform", platform)
                )
                j["link_source"] = "search_fallback"

    def _analyze_multi_with_gemini(self, subject: str, sender: str, body: str, html_body: str, platform: str, api_key: str) -> Optional[List[Dict[str, Any]]]:
        if _gemini_quota_in_cooldown():
            logger.info("Skipping Gemini extraction: quota cooldown active.")
            return None
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
- Glassdoor emails often wrap job links in click-tracking redirects (e.g. https://mail8.content.glassdoor.com/ls/click?upn=...). Those DO point at single job postings - treat each distinct one as a real job link and copy it exactly.
- NEVER invent or generate a search URL. If the email has no direct posting link, leave apply_url as an empty string.

Subject: {subject}
Sender: {sender}
Body & Links:
{(html_body or body)[:20000]}

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
                    _gemini_report_success()
                    return parsed["jobs"]

        except Exception as e:
            if _quota_error(e):
                _gemini_report_quota_error(e)
            else:
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
