import unittest
import os
import sys
import uuid
import json
import tempfile

# Isolate the test database BEFORE any project import: a temp SQLite file and
# no DATABASE_URL, so running the suite never touches local dashboard data or
# the production Neon database.
os.environ["JOB_AGENT_DB_PATH"] = os.path.join(tempfile.gettempdir(), f"gmail_jobs_test_{uuid.uuid4().hex}.db")
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import (
    init_db,
    insert_job,
    get_all_jobs,
    get_job_by_id,
    update_job_status,
    get_dashboard_stats,
    find_previous_applications_for_company,
    find_existing_duplicate,
    dedupe_existing_jobs,
    normalize_apply_url,
    get_setting,
    update_settings
)
from ai_extractor import (
    ai_extractor,
    _company_from_subject,
    parse_company_from_anchor,
    _looks_like_company,
    _subject_hint_is_first_link_only,
)
from job_matcher import job_matcher
from email_service import email_service
from fastapi.testclient import TestClient
from app import app

class TestGmailJobAgent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        # Never spam the real phone/desktop during tests: the notification
        # service reads these settings per call from the (isolated) test DB.
        update_settings({"mobile_notify": "false", "desktop_notify": "false"})

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

    def test_05_duplicate_detection_url_tracking_params(self):
        """URLs differing only by tracking params / scheme / www compare equal."""
        base = "https://www.linkedin.com/jobs/view/123456?trackingId=abc123&trk=some-pulse"
        same = "http://linkedin.com/jobs/view/123456/"
        different = "https://www.linkedin.com/jobs/view/999999"
        self.assertEqual(normalize_apply_url(base), normalize_apply_url(same))
        self.assertNotEqual(normalize_apply_url(base), normalize_apply_url(different))

    def test_06_duplicate_detection_company_title(self):
        """Same company + similar title is flagged even when the URL differs."""
        marker = f"dupco{uuid.uuid4().hex[:8]}"
        job_id = insert_job({
            "message_id": f"test_dup_{uuid.uuid4()}",
            "email_subject": "Job Alert",
            "email_sender": "alerts@example.com",
            "source_platform": "LinkedIn",
            "job_title": "Senior Backend Engineer",
            "company_name": marker,
            "apply_url": f"https://example.com/jobs/{uuid.uuid4()}",
            "status": "NEW"
        })
        dup = find_existing_duplicate(
            apply_url="https://example.com/jobs/different-link",
            company_name=marker,
            job_title="Senior Backend  Engineer"  # extra space, still similar
        )
        self.assertIsNotNone(dup)
        self.assertEqual(dup["id"], job_id)

        # Different title at the same company is NOT a duplicate
        dup_other = find_existing_duplicate(
            apply_url="https://example.com/jobs/other",
            company_name=marker,
            job_title="Graphic Designer Intern"
        )
        self.assertIsNone(dup_other)

    def test_07_dedupe_existing_jobs(self):
        """dedupe_existing_jobs removes later copies, keeping the oldest row."""
        marker = f"dedupco{uuid.uuid4().hex[:8]}"
        url = f"https://example.com/jobs/view/98765?trackingId=xyz"
        id1 = insert_job({
            "message_id": f"test_dedupe1_{uuid.uuid4()}",
            "email_subject": "Job Alert",
            "email_sender": "alerts@example.com",
            "source_platform": "LinkedIn",
            "job_title": "Platform Engineer",
            "company_name": marker,
            "apply_url": url,
            "status": "NEW"
        })
        id2 = insert_job({
            "message_id": f"test_dedupe2_{uuid.uuid4()}",
            "email_subject": "Job Alert (re-send)",
            "email_sender": "alerts@example.com",
            "source_platform": "Indeed",
            "job_title": "Platform  Engineer",  # similar title, different URL
            "company_name": marker,
            "apply_url": "https://indeed.com/viewjob?jk=repost123",
            "status": "NEW"
        })
        self.assertNotEqual(id1, id2)

        deleted = dedupe_existing_jobs()
        self.assertGreaterEqual(deleted, 1)

        self.assertIsNotNone(get_job_by_id(id1), "oldest duplicate row must be kept")
        self.assertIsNone(get_job_by_id(id2), "newer duplicate row must be deleted")

    def test_08_custom_job_endpoint(self):
        """POST /api/simulate-custom-job creates one job with defaults applied."""
        client = TestClient(app)
        marker = f"Custom Co {uuid.uuid4().hex[:6]}"
        resp = client.post("/api/simulate-custom-job", json={
            "job_title": "QA Automation Lead",
            "company_name": marker,
            "skills": "Selenium, Pytest, API testing",
            "location": "Pune"
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "success")
        self.assertFalse(data.get("duplicate", False))
        self.assertEqual(len(data["jobs"]), 1)
        job = data["jobs"][0]
        self.assertTrue(job["apply_url"].startswith("http"), "blank apply URL must be auto-generated")
        self.assertEqual(job["source_platform"], "Direct")
        self.assertIn("Selenium", job["skills"] if isinstance(job["skills"], str) else ",".join(job["skills"]))

        # Posting the exact same custom job again is blocked as a duplicate
        resp2 = client.post("/api/simulate-custom-job", json={
            "job_title": "QA Automation Lead",
            "company_name": marker
        })
        data2 = resp2.json()
        self.assertEqual(data2["status"], "success")
        self.assertTrue(data2.get("duplicate"))
        self.assertEqual(len(data2["jobs"]), 0)

    def test_09_company_from_subject_formats(self):
        """Subject-derived company hints cover the real alert-email formats."""
        cases = {
            "Deepak, your application was sent to Kanerika Inc": "Kanerika Inc",
            "Just in at TestVagrant: This week's employee reviews and more": "TestVagrant",
            "New jobs at Testunity! Apply to 5 new positions": "Testunity",
            "Test Automation Engineer at Testunity and 6 more jobs in India for you. Apply Now": "Testunity",
            "Senior QA Engineer at Lister Digital. 12 more senior software quality assurance engineer jobs in Bengaluru, Karnataka": "Lister Digital",
            "New jobs similar to AI Test Architect at WSA \u2013 Wonderful Sound for All": "WSA \u2013 Wonderful Sound for All",
        }
        for subject, expected in cases.items():
            self.assertEqual(_company_from_subject(subject), expected, f"subject: {subject}")
        # No company signal -> empty, never a placeholder
        self.assertEqual(_company_from_subject("Your weekly job digest is here"), "")

    def test_10_anchor_company_junk_rejection(self):
        """Anchor tails that are tech/skill fragments must not become companies."""
        junk_anchors = [
            "Senior Software Engineer - .Net",
            "Senior Software Quality Assurance Engineer - Manual QA",
            "Senior Designer - DFMA, CAD Software",
            "Automation - Test Architect",
            "TechOps-DE-AI-Staff-Assistant-GDSN02 - Company from Email",
        ]
        for anchor in junk_anchors:
            self.assertEqual(parse_company_from_anchor(anchor), "", f"anchor: {anchor}")

        good_anchors = {
            "KPMG India Python Ai Testing KPMG India \u00b7 Bengaluru, Karnataka, India": "KPMG India",
            "GoDaddy Senior Network Security Analyst GoDaddy \u00b7 Hyderabad": "GoDaddy",
        }
        for anchor, expected in good_anchors.items():
            self.assertEqual(parse_company_from_anchor(anchor), expected, f"anchor: {anchor}")

        # Placeholder fragments can never validate as companies
        for frag in (".Net", "Manual QA", "Company from Email", "GoDaddy"):
            self.assertEqual(_looks_like_company(frag), frag == "GoDaddy", f"frag: {frag}")

    def test_11_similar_jobs_hint_first_link_only(self):
        """Multi-job digests ('similar to', 'N more jobs'): the subject company
        applies only to the first/lead link; later links are other companies."""
        self.assertTrue(_subject_hint_is_first_link_only("New jobs similar to AI Test Architect at WSA \u2013 Wonderful Sound for All"))
        self.assertTrue(_subject_hint_is_first_link_only("Senior QA Engineer at Lister Digital. 12 more jobs"))
        self.assertTrue(_subject_hint_is_first_link_only("Test Automation Engineer at Testunity and 6 more jobs in India"))
        # Single-job emails keep the hint for every extracted link
        self.assertFalse(_subject_hint_is_first_link_only("Deepak, your application was sent to Kanerika Inc"))

        from ai_extractor import build_jobs_from_real_links
        links = [
            {"url": "https://www.linkedin.com/comm/jobs/view/111/", "anchor_text": "AI Test Architect WSA \u2013 Wonderful Sound for All"},
            {"url": "https://www.linkedin.com/comm/jobs/view/222/", "anchor_text": "Engineering Quality Lead \u2013 AI & Product Quality"},
        ]
        jobs = build_jobs_from_real_links(
            links, "LinkedIn", False,
            company_hint="WSA \u2013 Wonderful Sound for All",
            hint_first_link_only=True,
        )
        self.assertEqual(jobs[0]["company_name"], "WSA \u2013 Wonderful Sound for All")
        self.assertEqual(jobs[1]["company_name"], "Company from Email", "later links must not inherit the original job's company")

if __name__ == "__main__":
    unittest.main()
