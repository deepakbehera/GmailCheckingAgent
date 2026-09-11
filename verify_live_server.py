import sys
import time
import requests

# Ensure utf-8 output on Windows console
sys.stdout.reconfigure(encoding='utf-8')

# Optional CLI arg: python verify_live_server.py [BASE_URL]
BASE_URL = (sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000")

S = requests.Session()
# Cold-start tolerant timeouts (serverless first-hit can be slow)
TIMEOUT = (20, 60)


def retry(fn, attempts=3, wait=8):
    """Retry transient failures so cold serverless starts cannot fail the suite."""
    last_err = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                print(f"    [RETRY] {type(e).__name__}: {e} (attempt {i + 1}/{attempts})")
                time.sleep(wait)
    raise last_err


def test_live_server():
    print(f"Testing live server at {BASE_URL}...")
    injected_ids = []

    # 1. Test HTML Dashboard
    r_html = S.get(f"{BASE_URL}/", timeout=TIMEOUT)
    assert r_html.status_code == 200, f"Expected 200, got {r_html.status_code}"
    assert "AI Gmail Job Agent" in r_html.text, "Dashboard HTML missing title"
    print("[OK] Dashboard HTML loaded successfully.")

    # 2. Test Stats
    r_stats = retry(lambda: S.get(f"{BASE_URL}/api/stats", timeout=TIMEOUT))
    assert r_stats.status_code == 200
    stats = r_stats.json()["stats"]
    print(f"[OK] Stats API: Total Jobs = {stats['total_jobs']}, Target Email = {stats['target_email']}")

    # 3. Inject simulated multi-job digest, pick first job
    r_sim = retry(lambda: S.post(f"{BASE_URL}/api/simulate-job", timeout=TIMEOUT))
    assert r_sim.status_code == 200, f"simulate-job expected 200, got {r_sim.status_code}"
    sim_data = r_sim.json()
    jobs = sim_data.get("jobs", [])
    assert jobs, "simulate-job returned no jobs"
    for j in jobs:
        if j.get("id"):
            injected_ids.append(j["id"])
    job = jobs[0]
    job_id = job["id"]
    print(f"[OK] Injected simulated alert: {len(jobs)} job(s); using job #{job_id}: "
          f"'{job['job_title']}' at '{job['company_name']}'")

    # 4. Fetch single job by ID
    r_get = retry(lambda: S.get(f"{BASE_URL}/api/jobs/{job_id}", timeout=TIMEOUT))
    assert r_get.status_code == 200, f"GET job expected 200, got {r_get.status_code}"
    fetched = r_get.json()["job"]
    assert fetched["id"] == job_id
    print(f"[OK] GET /api/jobs/{job_id} verified: '{fetched['job_title']}'")

    # 5. Mark Job as Applied (Strikethrough / Red Marker)
    r_apply = S.post(f"{BASE_URL}/api/jobs/{job_id}/status", json={"status": "APPLIED"}, timeout=TIMEOUT)
    assert r_apply.status_code == 200, f"status update expected 200, got {r_apply.status_code}"
    print(f"[OK] Marked Job #{job_id} as APPLIED (Strikethrough & Red Marker applied).")

    # 6. Verify Filter for APPLIED jobs
    r_filtered = retry(lambda: S.get(f"{BASE_URL}/api/jobs?status=APPLIED", timeout=TIMEOUT))
    assert r_filtered.status_code == 200
    applied_list = r_filtered.json()["jobs"]
    found = any(j["id"] == job_id for j in applied_list)
    assert found, f"Job #{job_id} not found in APPLIED filter"
    print(f"[OK] Applied filter verified: {len(applied_list)} applied job(s) visible.")

    # 7. Test Manual Check Now (real Gmail IMAP scan)
    r_check = retry(lambda: S.post(f"{BASE_URL}/api/check-now", timeout=(20, 120)))
    assert r_check.status_code == 200, f"check-now expected 200, got {r_check.status_code}"
    print(f"[OK] Manual 'Check Now' triggered: {r_check.json().get('message', 'done')}")

    # 8. Check History
    r_hist = retry(lambda: S.get(f"{BASE_URL}/api/history", timeout=TIMEOUT))
    assert r_hist.status_code == 200
    history = r_hist.json()["history"]
    assert len(history) > 0, "History is empty after check"
    print(f"[OK] History API verified: {len(history)} check cycles recorded.")

    # 9. Verify single-job DELETE (cleanup of the injected job)
    for jid in injected_ids:
        r_del = S.delete(f"{BASE_URL}/api/jobs/{jid}", timeout=TIMEOUT)
        assert r_del.status_code == 200, f"DELETE job #{jid} expected 200, got {r_del.status_code}"
    if injected_ids:
        print(f"[OK] Deleted injected test job(s): {injected_ids}")

    print("\nALL LIVE SERVER ENDPOINTS AND WORKFLOWS VERIFIED SUCCESSFULLY!")


if __name__ == "__main__":
    test_live_server()
