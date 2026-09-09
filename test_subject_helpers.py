"""Unit checks for the new subject/company helpers and junk filtering."""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv
load_dotenv(".env")
import ai_extractor as ax

cases = [
    ("Just in at TestVagrant: This week's employee reviews and more", "TestVagrant"),
    ("Test Automation Engineer at Testunity and 6 more jobs in India for you. Apply Now.", "Testunity"),
    ("Quality Manager at AGS Aluminum Alloy and 6 more jobs in India for you. Apply Now.", "AGS Aluminum Alloy"),
    ("SDET - QA Automation(Remote) at Jitterbit and 9 more jobs in India for you. Apply Now.", "Jitterbit"),
    ("Capgemini, Clinisys and others are hiring in Bengaluru. Apply Now.", ""),
    ("New jobs in India. Apply Now.", ""),
]
ok = True
for subj, expected in cases:
    got = ax._company_from_subject(subj)
    status = "OK " if got == expected else "FAIL"
    if got != expected:
        ok = False
    print(f"[{status}] {subj[:60]:62} -> '{got}' (expected '{expected}')")

# Junk filter: Glassdoor search pages must be rejected, postings accepted
links = ax.extract_real_job_links(
    '<a href="https://www.glassdoor.co.in/Job/automation-engineer-jobs-SRCH_KO0,19.htm?x=1">See more</a>'
    '<a href="https://www.glassdoor.co.in/job-listing/sdet-testvagrant-JV_IC2940587_KO0,4_KE5,16.htm?jl=1">SDET</a>',
    "", "Glassdoor")
urls = [l["url"] for l in links]
assert not any("SRCH_" in u for u in urls), "SRCH_ search page leaked through!"
assert any("job-listing/sdet-testvagrant" in u for u in urls), "Real posting rejected!"
print("[OK ] SRCH_ search page rejected, real posting accepted")

# Title cleanup sanity via build_jobs_from_real_links
jobs = ax.build_jobs_from_real_links(
    [{"url": "https://www.glassdoor.co.in/job-listing/sdet-testvagrant-JV_IC1.htm",
      "anchor_text": "SDET Bengaluru ₹5L - ₹10L ( Glassdoor Est. ) BCS Computer Science Test automation Full-tim"}],
    "Glassdoor", False, company_hint="TestVagrant")
t = jobs[0]["job_title"]
assert t == "SDET", f"title cleanup produced: {t!r}"
assert jobs[0]["company_name"] == "TestVagrant"
print(f"[OK ] title cleanup -> {t!r}, company -> {jobs[0]['company_name']!r}")

print("\nALL SUBJECT/HELPER CHECKS PASSED" if ok else "\nSOME CHECKS FAILED")
sys.exit(0 if ok else 1)
