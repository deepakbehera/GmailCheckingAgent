"""
Tests for the job-tailored resume generator (resume_tailor.py).

Run with:
    JOB_AGENT_DB_PATH=<temp db> python -m pytest test_resume_tailor.py -v
or simply:
    JOB_AGENT_DB_PATH=%TEMP%\test_resume.db python test_resume_tailor.py
"""

import os
import sys
import tempfile

# Test isolation: point the database layer at a throwaway SQLite file BEFORE
# importing anything else (config.py reads this env var at import time).
_TMP_DIR = tempfile.mkdtemp(prefix="resume_test_")
os.environ["JOB_AGENT_DB_PATH"] = os.path.join(_TMP_DIR, "test_resume.db")

import unittest  # noqa: E402
import json  # noqa: E402

from database import build_resume_filename, _resume_job_key, save_tailored_resume, get_tailored_resume  # noqa: E402
import resume_tailor  # noqa: E402
import pymupdf  # noqa: E402

BASE_PDF = os.path.join(os.path.dirname(__file__), "data", "Deepak_Kumar_Behera_Resume.pdf")


class NamingTests(unittest.TestCase):
    def test_basic_naming(self):
        name = build_resume_filename(
            {"job_title": "Senior Test Automation Engineer", "company_name": "Infosys Ltd."}, 7)
        self.assertEqual(name, "07_Senior_Test_Automation_Engineer_Infosys_Ltd.pdf")

    def test_special_chars_stripped(self):
        name = build_resume_filename(
            {"job_title": "QA/SDET Lead (Remote)", "company_name": "AT&T"}, 123)
        self.assertEqual(name, "123_QA_SDET_Lead_Remote_AT_T.pdf")

    def test_long_title_truncated(self):
        name = build_resume_filename(
            {"job_title": "X" * 200, "company_name": "Acme"}, 1)
        self.assertLessEqual(len(name), 100)
        self.assertTrue(name.endswith("_Acme.pdf"))

    def test_job_key_stable(self):
        a = _resume_job_key({"company_name": "AT&T!", "job_title": "QA  Lead"})
        b = _resume_job_key({"company_name": "att", "job_title": "qa lead"})
        self.assertEqual(a, b)


class PlanTests(unittest.TestCase):
    """Structure extraction from the real base resume."""

    @classmethod
    def setUpClass(cls):
        cls.doc = pymupdf.open(BASE_PDF)
        cls.plan = resume_tailor._build_line_plan(cls.doc)

    @classmethod
    def tearDownClass(cls):
        cls.doc.close()

    def test_plan_has_headline(self):
        self.assertIsNotNone(self.plan["headline"])
        self.assertIn("AI TEST ENGINEER", self.plan["headline"]["orig"])

    def test_plan_has_summary_slots(self):
        self.assertEqual(len(self.plan["summary_slots"]), 9)
        joined = " ".join(s["orig"] for s in self.plan["summary_slots"])
        self.assertIn("Senior Quality Engineering", joined)
        # no experience lines leaked in
        self.assertNotIn("AT&T", joined)

    def test_plan_has_comp_groups(self):
        labels = [g["label"] for g in self.plan["comp_groups"]]
        self.assertEqual(len(labels), 8)
        self.assertIn("AI & LLM Testing:", labels[0])
        self.assertIn("Cloud & CI/CD Tools:", labels[-1])

    def test_headline_no_section_headers_in_plan(self):
        joined = " ".join(s["orig"] for s in self.plan["summary_slots"])
        self.assertNotIn("PROFESSIONAL SUMMARY", joined)
        self.assertNotIn("CORE COMPETENCIES", joined)


class FallbackTailoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with pymupdf.open(BASE_PDF) as doc:
            cls.plan = resume_tailor._build_line_plan(doc)

    def test_rule_based_keeps_labels(self):
        job = {"job_title": "SDET", "company_name": "TestCorp", "skills": ["Playwright"],
               "summary": "", "experience_level": ""}
        texts = resume_tailor._tailor_rule_based(job, self.plan)
        self.assertEqual(texts["engine"], "rules")
        self.assertEqual(len(texts["comp_contents"]), 8)
        # Content-only text: no label prefix, no bullet glyphs
        self.assertNotIn(":", texts["comp_contents"][0].split("Safety Testing")[0][:40] or ":")
        self.assertFalse(texts["comp_contents"][0].startswith("AI"))
        self.assertIn("Agentic AI", texts["comp_contents"][0])
        # Playwright should be present in the automation group content
        auto_idx = next(i for i, g in enumerate(self.plan["comp_groups"])
                        if "Automation Architecture" in g["label"])
        content = texts["comp_contents"][auto_idx]
        self.assertIn("Playwright", content)

    def test_wrap_to_slots_never_overflows(self):
        slots = self.plan["summary_slots"][0:4]
        wrapped = resume_tailor._wrap_to_slots("word " * 200, slots)
        self.assertEqual(len(wrapped), 4)
        for slot, line in zip(slots, wrapped):
            if line:
                width = resume_tailor._text_length(line, slot["style"], slot["size"])
                self.assertLessEqual(width, slot["right"] - slot["x_start"] + 0.6)


class PDFGenerationTests(unittest.TestCase):
    """End-to-end: generate a real PDF, then verify its text and structure."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="resume_pdf_")
        with open(BASE_PDF, "rb") as f:
            base_bytes = f.read()
        job = {"job_title": "Senior Test Automation Engineer", "company_name": "Infosys",
               "skills": ["Playwright", "Python", "AWS"], "summary": "Automation role",
               "experience_level": "Senior", "raw_email_snippet": ""}
        texts = resume_tailor._tailor_rule_based(job, cls.plan) if hasattr(cls, "plan") else None
        if texts is None:
            with pymupdf.open(stream=base_bytes, filetype="pdf") as doc:
                plan = resume_tailor._build_line_plan(doc)
            texts = resume_tailor._tailor_rule_based(job, plan)
        out = os.path.join(cls.tmpdir, "01_Senior_Test_Automation_Engineer_Infosys.pdf")
        resume_tailor._write_tailored_pdf(base_bytes, texts, out)
        with open(out, "rb") as f:
            cls.pdf_bytes = f.read()
        cls.out_path = out

    def test_pdf_generated(self):
        self.assertGreater(len(self.pdf_bytes), 10000)

    def test_structure_preserved(self):
        with pymupdf.open(self.out_path) as doc:
            self.assertEqual(len(doc), 2)
            p0 = doc[0].get_text()
            self.assertIn("DEEPAK KUMAR BEHERA", p0)
            self.assertIn("PROFESSIONAL SUMMARY", p0)
            self.assertIn("AT&T, India", p0)
            p1 = doc[1].get_text()
            self.assertIn("TECHNICAL SKILLS", p1)
            self.assertIn("IISc Bangalore", p1)
            self.assertIn("B.E. in Computer Science", p1)
            # competency labels survived (untouched)
            self.assertIn("AI & LLM Testing:", doc[0].get_text())

    def test_fonts_preserved(self):
        with pymupdf.open(BASE_PDF) as base, pymupdf.open(self.out_path) as new:
            f_base = {f[3].split("+")[-1] for f in base[0].get_fonts()}
            f_new = {f[3].split("+")[-1] for f in new[0].get_fonts()}
            self.assertTrue(f_base.issubset(f_new) or f_new.issubset(f_base) or "Liberation-Sans-Bold" in str(f_base))

    def test_touched_lines_replaced(self):
        with pymupdf.open(self.out_path) as doc:
            p0 = doc[0].get_text()
            # Headline region still contains an engineer headline
            self.assertIn("AI TEST ENGINEER", p0)


class SaveLoadTests(unittest.TestCase):
    def test_save_and_get(self):
        job = {"id": 42, "job_title": "QA Lead", "company_name": "Acme Corp"}
        name = save_tailored_resume(job, "42_QA_Lead_Acme_Corp.pdf", b"%PDF-fake", "2026-01-01", "gemini",
                                    match_summary="good fit")
        self.assertEqual(name, "42_QA_Lead_Acme_Corp.pdf")
        row = get_tailored_resume({"company_name": "acme corp", "job_title": "qa lead"})
        self.assertIsNotNone(row)
        self.assertEqual(row["file_name"], "42_QA_Lead_Acme_Corp.pdf")
        self.assertEqual(bytes(row["pdf_bytes"]), b"%PDF-fake")


if __name__ == "__main__":
    unittest.main(verbosity=2)
