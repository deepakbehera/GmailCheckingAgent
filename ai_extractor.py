import re
import json
import logging
import urllib.parse
from typing import Dict, Any, List, Optional
from config import GEMINI_API_KEY, DEFAULT_GEMINI_MODEL
from database import get_setting

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
    s_low = sender.lower()
    full_low = f"{sender} {subject} {body}".lower()

    if "linkedin" in s_low or "linkedin.com" in full_low:
        return "LinkedIn"
    elif "naukri" in s_low or "naukri.com" in full_low:
        return "Naukri"
    elif "indeed" in s_low or "indeed.com" in full_low:
        return "Indeed"
    elif "glassdoor" in s_low or "glassdoor.com" in full_low:
        return "Glassdoor"
    return "Direct"

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
                    for j in extracted_jobs:
                        if not j.get("apply_url") or not j["apply_url"].startswith("http"):
                            j["apply_url"] = make_working_url(j.get("job_title", "Engineer"), j.get("company_name", "Company"), platform)
                    return extracted_jobs
            except Exception as e:
                logger.warning(f"Gemini multi-job analysis failed: {e}")

        jobs = self._heuristic_multi_analysis(subject, sender, body, html_body, platform)
        for j in jobs:
            if not j.get("apply_url") or not j["apply_url"].startswith("http"):
                j["apply_url"] = make_working_url(j.get("job_title", "Engineer"), j.get("company_name", "Company"), platform)
        return jobs

    def _analyze_multi_with_gemini(self, subject: str, sender: str, body: str, html_body: str, platform: str, api_key: str) -> Optional[List[Dict[str, Any]]]:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            prompt = f"""
You are an expert AI Job Hunter Assistant.
Analyze the following email from {platform} ({sender}).
Extract ALL individual job openings mentioned in the email into a list of jobs.

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
      "apply_url": string (extract the specific direct apply or search link),
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
                    apply_link = make_working_url(c_title, c_comp, "LinkedIn")
                    jobs.append({
                        "job_title": c_title,
                        "company_name": c_comp,
                        "location": "Remote / Bengaluru, India",
                        "job_type": "Full-time",
                        "apply_url": apply_link,
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
                    apply_link = make_working_url(c_title, c_comp, "Naukri")
                    jobs.append({
                        "job_title": c_title,
                        "company_name": c_comp,
                        "location": "Bengaluru / Hyderabad / Remote",
                        "job_type": "Full-time",
                        "apply_url": apply_link,
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
                    apply_link = make_working_url(c_title, c_comp, "Indeed")
                    jobs.append({
                        "job_title": c_title,
                        "company_name": c_comp,
                        "location": "Remote / Hybrid",
                        "job_type": "Full-time",
                        "apply_url": apply_link,
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
                    apply_link = make_working_url(c_title, c_comp, "Glassdoor")
                    jobs.append({
                        "job_title": c_title,
                        "company_name": c_comp,
                        "location": "Remote / Bengaluru",
                        "job_type": "Full-time",
                        "apply_url": apply_link,
                        "salary": "₹30 - ₹50 LPA",
                        "skills": ["Python", "Machine Learning", "System Design"],
                        "experience_level": "Senior",
                        "summary": f"Glassdoor Job Alert: {c_title} at {c_comp}.",
                        "source_platform": "Glassdoor",
                        "match_score": 86,
                        "is_application_confirmation": is_confirmation
                    })

        # 5. Direct
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

            apply_link = make_working_url(job_title, company_name, platform)

            jobs.append({
                "job_title": job_title,
                "company_name": company_name,
                "location": "Remote / Bengaluru",
                "job_type": "Full-time",
                "apply_url": apply_link,
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
