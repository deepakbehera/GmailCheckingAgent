import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import APP_TITLE, VERSION, BASE_DIR, PORT
from database import (
    get_all_jobs,
    get_job_by_id,
    update_job_status,
    delete_job,
    get_dashboard_stats,
    get_recent_email_logs,
    get_check_history,
    get_all_settings,
    update_settings,
    get_setting,
    insert_job
)
from scheduler import job_scheduler
from notification_service import notification_service
from email_service import email_service
from ai_extractor import ai_extractor
from job_matcher import job_matcher
from tunnel_service import tunnel_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # On Vercel serverless, skip the background scheduler and cloudflared
    # tunnel entirely (no long-lived process, and starting them wastes
    # precious seconds of the function time budget on every cold start).
    if os.getenv("VERCEL"):
        logger.info(f"{APP_TITLE} v{VERSION} initialized (serverless mode: scheduler/tunnel disabled).")
        yield
        return

    # Start scheduler & public tunnel (local mode only)
    job_scheduler.start()
    logger.info(f"{APP_TITLE} v{VERSION} initialized.")
    yield
    if job_scheduler.scheduler.running:
        job_scheduler.scheduler.shutdown()
    tunnel_service.stop_tunnel()

app = FastAPI(title=APP_TITLE, version=VERSION, lifespan=lifespan)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# --- Pydantic Models ---

class StatusUpdateRequest(BaseModel):
    status: str
    notes: Optional[str] = None

class SettingsUpdateRequest(BaseModel):
    target_email: Optional[str] = None
    check_interval_mins: Optional[str] = None
    desktop_notify: Optional[str] = None
    mobile_notify: Optional[str] = None
    ntfy_topic: Optional[str] = None
    auth_mode: Optional[str] = None
    gemini_api_key: Optional[str] = None
    imap_password: Optional[str] = None
    public_url: Optional[str] = None

# --- REST Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>AI Gmail Job Checking Dashboard</h1><p>Frontend loading...</p>")

@app.get("/api/jobs")
async def api_get_jobs(status: Optional[str] = None, platform: Optional[str] = None, q: Optional[str] = None):
    jobs = get_all_jobs(status_filter=status, platform_filter=platform, search_query=q)
    return {"status": "success", "count": len(jobs), "jobs": jobs}

@app.get("/api/jobs/{job_id}")
async def api_get_job(job_id: int):
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    history = []
    if job.get("company_name"):
        from database import find_previous_applications_for_company
        history = find_previous_applications_for_company(job["company_name"], exclude_id=job["id"])

    return {"status": "success", "job": job, "company_history": history}

@app.post("/api/jobs/{job_id}/status")
async def api_update_status(job_id: int, req: StatusUpdateRequest):
    success = update_job_status(job_id, req.status.upper(), req.notes)
    if not success:
        raise HTTPException(status_code=404, detail="Job not found or could not be updated")
    
    await job_scheduler.broadcast_event("JOB_STATUS_UPDATED", {
        "job_id": job_id,
        "new_status": req.status.upper()
    })
    return {"status": "success", "message": f"Job #{job_id} status updated to {req.status.upper()}"}

@app.delete("/api/jobs/{job_id}")
async def api_delete_job(job_id: int):
    success = delete_job(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Job not found")
    
    await job_scheduler.broadcast_event("JOB_DELETED", {"job_id": job_id})
    return {"status": "success", "message": f"Job #{job_id} deleted"}

@app.post("/api/check-now")
async def api_trigger_check():
    result = await job_scheduler.run_email_check_cycle(triggered_by="manual")
    return result

# Vercel serverless: give the Gmail IMAP check cycle up to 60s (Hobby plan
# max). Locally this decorator argument is ignored by uvicorn.
try:
    api_trigger_check.__dict__["_vercel_max_duration"] = 60
except Exception:
    pass

@app.post("/api/simulate-job")
async def api_simulate_job(source: Optional[str] = None):
    """Injects a sample multi-job alert digest (LinkedIn, Naukri, Indeed, Glassdoor) for instant testing."""
    sim_email = email_service.create_single_simulated_job_email(source)
    
    subject = sim_email["subject"]
    sender = sim_email["sender"]
    body = sim_email["body"]
    html_body = sim_email.get("html_body", "")
    msg_id = sim_email["message_id"]
    date_rcvd = sim_email["date_received"]

    extracted_jobs = ai_extractor.analyze_email_multi(subject, sender, body, html_body)
    new_jobs_list = []

    for item in extracted_jobs:
        company = item.get("company_name", "Unknown Company")
        title = item.get("job_title", "Specialist Role")
        platform = item.get("source_platform", "Direct")
        
        dup_check = job_matcher.check_duplicate_and_history(company, title)

        job_record = {
            "message_id": f"{msg_id}_{len(new_jobs_list)}",
            "email_subject": subject,
            "email_sender": sender,
            "source_platform": platform,
            "date_received": date_rcvd,
            "job_title": title,
            "company_name": company,
            "location": item.get("location", "Remote"),
            "job_type": item.get("job_type", "Full-time"),
            "apply_url": item.get("apply_url", ""),
            "salary": item.get("salary", "Not specified"),
            "skills": item.get("skills", []),
            "experience_level": item.get("experience_level", "Senior"),
            "summary": item.get("summary", ""),
            "raw_email_snippet": body[:500],
            "status": "NEW",
            "applied_earlier": dup_check["applied_earlier"],
            "previous_application_id": dup_check["previous_application_id"],
            "previous_applied_date": dup_check["previous_applied_date"],
            "previous_job_title": dup_check["previous_job_title"],
            "match_score": item.get("match_score", 90)
        }

        job_id = insert_job(job_record)
        job_record["id"] = job_id
        new_jobs_list.append(job_record)

    # Notifications
    notification_service.notify_check_result(1, new_jobs_list)

    # Broadcast
    for j in new_jobs_list:
        await job_scheduler.broadcast_event("NEW_JOB_RECEIVED", {"job": j})

    return {
        "status": "success",
        "message": f"Processed {len(new_jobs_list)} job openings from {sender}!",
        "jobs": new_jobs_list
    }

@app.get("/api/stats")
async def api_get_stats():
    stats = get_dashboard_stats()
    stats["next_check_at"] = get_setting("next_check_at", "")
    stats["check_interval_mins"] = get_setting("check_interval_mins", "15")
    stats["target_email"] = get_setting("target_email", "deepak.gvit@gmail.com")
    stats["auth_mode"] = get_setting("auth_mode", "simulator")
    stats["public_url"] = get_setting("public_url", "")
    return {"status": "success", "stats": stats}

@app.get("/api/logs")
async def api_get_logs(limit: int = 50):
    logs = get_recent_email_logs(limit)
    return {"status": "success", "logs": logs}

@app.get("/api/history")
async def api_get_history(limit: int = 30):
    history = get_check_history(limit)
    return {"status": "success", "history": history}

@app.get("/api/settings")
async def api_get_settings():
    settings = get_all_settings()
    if settings.get("imap_password"):
        settings["imap_password_masked"] = "••••••••••••••••"
    return {"status": "success", "settings": settings}

@app.post("/api/settings")
async def api_save_settings(req: SettingsUpdateRequest):
    updates = {}
    if req.target_email is not None: updates["target_email"] = req.target_email
    if req.check_interval_mins is not None:
        updates["check_interval_mins"] = req.check_interval_mins
        job_scheduler.update_interval(int(req.check_interval_mins))
    if req.desktop_notify is not None: updates["desktop_notify"] = req.desktop_notify
    if req.mobile_notify is not None: updates["mobile_notify"] = req.mobile_notify
    if req.ntfy_topic is not None: updates["ntfy_topic"] = req.ntfy_topic
    if req.auth_mode is not None: updates["auth_mode"] = req.auth_mode
    if req.public_url is not None: updates["public_url"] = req.public_url
    if req.gemini_api_key is not None and req.gemini_api_key.strip():
        updates["gemini_api_key"] = req.gemini_api_key.strip()
    if req.imap_password is not None and req.imap_password.strip():
        updates["imap_password"] = req.imap_password.strip()

    update_settings(updates)
    return {"status": "success", "message": "Settings updated successfully."}

@app.post("/api/test-notification")
async def api_test_notification():
    pub_url = notification_service.get_effective_dashboard_url()
    notification_service.send_desktop_notification(
        "🚀 Test Notification Active",
        f"Your AI Gmail Agent is connected! Mobile URL: {pub_url}"
    )
    sent_mobile = notification_service.send_mobile_push(
        "🚀 AI Agent Test Notification",
        f"Mobile sync active! Tapping 'Open Dashboard' will open your live dashboard:\n{pub_url}",
        apply_url="https://www.linkedin.com/jobs",
        tags="sparkles,bell",
        priority="default"
    )
    return {
        "status": "success",
        "desktop": "triggered",
        "mobile_sent": sent_mobile,
        "public_url": pub_url,
        "ntfy_topic": get_setting("ntfy_topic", "deepak-job-hunter-alerts")
    }

@app.get("/api/events")
async def sse_events(request: Request):
    queue = asyncio.Queue()
    job_scheduler.event_subscribers.append(queue)

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'CONNECTED', 'message': 'Live SSE Feed Connected'})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield f": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if queue in job_scheduler.event_subscribers:
                job_scheduler.event_subscribers.remove(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
