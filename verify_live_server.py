import sys
import requests
import json

# Ensure utf-8 output on Windows console
sys.stdout.reconfigure(encoding='utf-8')

BASE_URL = "http://127.0.0.1:8000"

def test_live_server():
    print(f"Testing live server at {BASE_URL}...")

    # 1. Test HTML Dashboard
    r_html = requests.get(f"{BASE_URL}/")
    assert r_html.status_code == 200, f"Expected 200, got {r_html.status_code}"
    assert "AI Gmail Job Agent" in r_html.text
    print("[OK] Dashboard HTML loaded successfully.")

    # 2. Test Stats
    r_stats = requests.get(f"{BASE_URL}/api/stats")
    assert r_stats.status_code == 200
    stats = r_stats.json()["stats"]
    print(f"[OK] Stats API: Total Jobs = {stats['total_jobs']}, Target Email = {stats['target_email']}")

    # 3. Simulate Job Ingestion
    r_sim = requests.post(f"{BASE_URL}/api/simulate-job")
    assert r_sim.status_code == 200
    job = r_sim.json()["job"]
    job_id = job["id"]
    print(f"[OK] Injected simulated job #{job_id}: '{job['job_title']}' at '{job['company_name']}'")

    # 4. Mark Job as Applied (Strikethrough / Red Marker)
    r_apply = requests.post(f"{BASE_URL}/api/jobs/{job_id}/status", json={"status": "APPLIED"})
    assert r_apply.status_code == 200
    print(f"[OK] Marked Job #{job_id} as APPLIED (Strikethrough & Red Marker applied).")

    # 5. Verify Filter for APPLIED jobs
    r_filtered = requests.get(f"{BASE_URL}/api/jobs?status=APPLIED")
    assert r_filtered.status_code == 200
    applied_list = r_filtered.json()["jobs"]
    found = any(j["id"] == job_id for j in applied_list)
    assert found, "Applied job not found in filtered list"
    print(f"[OK] Applied filter verified: Found {len(applied_list)} applied job(s).")

    # 6. Test Manual Check Now
    r_check = requests.post(f"{BASE_URL}/api/check-now")
    assert r_check.status_code == 200
    print(f"[OK] Manual 'Check Now' triggered: {r_check.json()['message']}")

    # 7. Check History
    r_hist = requests.get(f"{BASE_URL}/api/history")
    assert r_hist.status_code == 200
    history = r_hist.json()["history"]
    print(f"[OK] History API verified: {len(history)} check cycles recorded.")

    print("\nALL LIVE SERVER ENDPOINTS AND WORKFLOWS VERIFIED SUCCESSFULLY!")

if __name__ == "__main__":
    test_live_server()
