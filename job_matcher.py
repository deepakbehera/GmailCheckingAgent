import re
import logging
from typing import Dict, Any, List, Optional
from database import find_previous_applications_for_company

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def normalize_company(name: str) -> str:
    """Cleans up company name for comparison (e.g. removes Inc, LLC, Corp, Ltd)."""
    if not name:
        return ""
    cleaned = name.lower().strip()
    cleaned = re.sub(r'\b(inc\.?|llc\.?|corp\.?|corporation|ltd\.?|limited|technologies|solutions|labs)\b', '', cleaned)
    cleaned = re.sub(r'[^a-z0-9]', '', cleaned)
    return cleaned

class JobMatcher:
    def __init__(self):
        pass

    def check_duplicate_and_history(self, company_name: str, job_title: str) -> Dict[str, Any]:
        """
        Cross-references the database to check if the user has applied to this company or job earlier.
        """
        if not company_name or company_name.lower() in ["unknown company", "linkedin network"]:
            return {
                "applied_earlier": False,
                "previous_application_id": None,
                "previous_applied_date": None,
                "previous_job_title": None,
                "history_message": ""
            }

        norm_target = normalize_company(company_name)
        past_jobs = find_previous_applications_for_company(company_name)

        # Also search with normalized comparison
        applied_matches = []
        recorded_matches = []

        for job in past_jobs:
            norm_past = normalize_company(job["company_name"])
            if norm_target == norm_past or norm_target in norm_past or norm_past in norm_target:
                if job["status"] == "APPLIED":
                    applied_matches.append(job)
                else:
                    recorded_matches.append(job)

        if applied_matches:
            # Sort by applied_at desc
            applied_matches.sort(key=lambda x: x["applied_at"] or x["created_at"], reverse=True)
            most_recent = applied_matches[0]
            date_str = most_recent["applied_at"] or most_recent["created_at"]
            if date_str and "T" in date_str:
                date_str = date_str.split("T")[0]

            return {
                "applied_earlier": True,
                "previous_application_id": most_recent["id"],
                "previous_applied_date": date_str,
                "previous_job_title": most_recent["job_title"],
                "history_message": f"⚠️ Already applied to {most_recent['company_name']} for '{most_recent['job_title']}' on {date_str}."
            }

        elif recorded_matches:
            most_recent = recorded_matches[0]
            date_str = most_recent["created_at"].split("T")[0] if "T" in most_recent["created_at"] else most_recent["created_at"]
            return {
                "applied_earlier": False,
                "previous_application_id": most_recent["id"],
                "previous_applied_date": None,
                "previous_job_title": most_recent["job_title"],
                "history_message": f"ℹ️ Previous opening from {most_recent['company_name']} was posted on {date_str}."
            }

        return {
            "applied_earlier": False,
            "previous_application_id": None,
            "previous_applied_date": None,
            "previous_job_title": None,
            "history_message": ""
        }

job_matcher = JobMatcher()
