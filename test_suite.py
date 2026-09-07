import unittest
import os
import uuid
import json
from database import (
    init_db,
    insert_job,
    get_all_jobs,
    get_job_by_id,
    update_job_status,
    get_dashboard_stats,
    find_previous_applications_for_company,
    get_setting,
    update_settings
)
from ai_extractor import ai_extractor
from job_matcher import job_matcher
from email_service import email_service
from fastapi.testclient import TestClient
from app import app

class TestGmailJobAgent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def test_01_database_operations_and_platform(self):
        """Test inserting and updating job records with source platform in SQLite."""
        job_data = {
            "message_id": f"test_msg_multi_{uuid.uuid4()}",
            "email_subject": "LinkedIn Job Alert: Lead AI Engineer",
            "email_sender": "jobalerts-noreply@linkedin.com",
            "source_platform": "LinkedIn",
            "job_title": "Lead AI Engineer",
            "company_name": "Anthropic",
            "location": "San Francisco, CA",
            "job_type": "Full-time",
            "apply_url": "https://www.linkedin.com/jobs/view/anthropic-lead-ai",
            "salary": "$250k - $320k",
            "skills": ["Python", "Transformers", "LLMs"],
            "summary": "Lead AI engineering initiatives.",
            "status": "NEW"
        }
        job_id = insert_job(job_data)
        self.assertIsNotNone(job_id)

        job = get_job_by_id(job_id)
        self.assertEqual(job["company_name"], "Anthropic")
        self.assertEqual(job["source_platform"], "LinkedIn")
        self.assertEqual(job["status"], "NEW")

        # Test marking as APPLIED (strikethrough marker)
        updated = update_job_status(job_id, "APPLIED")
        self.assertTrue(updated)
        job_after = get_job_by_id(job_id)
        self.assertEqual(job_after["status"], "APPLIED")

    def test_02_multi_job_linkedin_digest_parsing(self):
        """Test extracting multiple distinct job postings from a single LinkedIn email."""
        email_content = email_service.create_single_simulated_job_email("LinkedIn")
        jobs = ai_extractor.analyze_email_multi(
            email_content["subject"],
            email_content["sender"],
            email_content["body"],
            email_content.get("html_body", "")
        )
        self.assertGreaterEqual(len(jobs), 2)
        for j in jobs:
            self.assertEqual(j["source_platform"], "LinkedIn")
            self.assertTrue(j["apply_url"].startswith("http"))
            self.assertTrue(len(j["job_title"]) > 0)
            self.assertTrue(len(j["company_name"]) > 0)

    def test_03_multi_job_naukri_alert_parsing(self):
        """Test extracting multiple distinct job postings from a Naukri email."""
        email_content = email_service.create_single_simulated_job_email("Naukri")
        jobs = ai_extractor.analyze_email_multi(
            email_content["subject"],
            email_content["sender"],
            email_content["body"],
            email_content.get("html_body", "")
        )
        self.assertGreaterEqual(len(jobs), 2)
        for j in jobs:
            self.assertEqual(j["source_platform"], "Naukri")
            self.assertIn("naukri.com", j["apply_url"])

    def test_04_rest_api_platform_filtering(self):
        """Test REST API platform filters and multi-job simulation."""
        client = TestClient(app)
        
        # Test simulate with specific platform
        resp_sim = client.post("/api/simulate-job?source=Indeed")
        self.assertEqual(resp_sim.status_code, 200)
        data = resp_sim.json()
        self.assertEqual(data["status"], "success")
        self.assertGreaterEqual(len(data["jobs"]), 1)

        # Test filtering by platform
        resp_filter = client.get("/api/jobs?platform=LinkedIn")
        self.assertEqual(resp_filter.status_code, 200)
        jobs = resp_filter.json()["jobs"]
        for j in jobs:
            self.assertEqual(j["source_platform"], "LinkedIn")

if __name__ == "__main__":
    unittest.main()
