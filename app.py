import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import APP_TITLE, VERSION, BASE_DIR, PORT, RESUME_CACHE_DIR
import resume_tailor
from database import (
    get_all_jobs,
    get_tailored_resume,
    get_job_by_id,
    update_job_status,
    mark_job_checked,
    clear_job_checked,
    delete_job,
    get_dashboard_stats,
    get_recent_email_logs,
    get_check_history,
    get_all_settings,
    update_settings,
    get_setting,
    insert_job,
    mark_all_jobs_applied,
    delete_all_jobs,
    dedupe_existing_jobs,
    find_existing_duplicate
)
from scheduler import job_scheduler
from notification_service import notification_service
from email_service import email_service, generate_working_apply_url
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

@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico():
    """Serves the favicon at the exact path browsers request, preventing a
    harmless but noisy 404 on every dashboard load."""
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")

@app.get("/favicon.svg", include_in_schema=False)
async def favicon_svg():
    """Vector favicon referenced from the HTML <head>; scales crisply."""
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

# --- Pydantic Models ---

class StatusUpdateRequest(BaseModel):
    status: str
    notes: Optional[str] = None

# Allowed workflow statuses a user can set from the dashboard dropdown.
# 'APPLIED' can also be set programmatically by the confirmation-email matcher.
VALID_JOB_STATUSES = {"NEW", "APPLIED", "IN_REVIEW", "SEE_LATER"}

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


class CustomJobRequest(BaseModel):
    """Payload for the 'Custom Job' simulate form on the dashboard.
    Only job_title and company_name are required; everything else is optional
    and gets a sensible default."""
    job_title: str
    company_name: str
    location: Optional[str] = None
    job_type: Optional[str] = None
    apply_url: Optional[str] = None
    salary: Optional[str] = None
    skills: Optional[Any] = None  # list OR comma-separated string
    experience_level: Optional[str] = None
    summary: Optional[str] = None
    source_platform: Optional[str] = None

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

# NOTE: bulk routes MUST be declared BEFORE the /api/jobs/{job_id} routes,
# otherwise FastAPI matches "all" as job_id and fails int parsing (422).
@app.post("/api/jobs/mark-all-applied")
async def api_mark_all_applied():
    """Marks every stored job as APPLIED (strikethrough + red badge on the dashboard)."""
    count = mark_all_jobs_applied()
    await job_scheduler.broadcast_event("JOBS_BULK_UPDATED", {"action": "mark_all_applied", "count": count})
    return {"status": "success", "message": f"Marked {count} job(s) as Applied.", "updated": count}

@app.delete("/api/jobs/all")
async def api_delete_all_jobs():
    """Clears and deletes ALL jobs from the dashboard/database."""
    count = delete_all_jobs()
    await job_scheduler.broadcast_event("JOBS_BULK_UPDATED", {"action": "delete_all", "count": count})
    return {"status": "success", "message": f"Deleted {count} job(s). Dashboard cleared.", "deleted": count}

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
    new_status = req.status.upper()
    if new_status not in VALID_JOB_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status '{req.status}'. Allowed: {sorted(VALID_JOB_STATUSES)}")

    success = update_job_status(job_id, new_status, req.notes)
    if not success:
        raise HTTPException(status_code=404, detail="Job not found or could not be updated")
    
    await job_scheduler.broadcast_event("JOB_STATUS_UPDATED", {
        "job_id": job_id,
        "new_status": new_status
    })
    return {"status": "success", "message": f"Job #{job_id} status updated to {new_status}"}

@app.post("/api/jobs/{job_id}/checked")
async def api_mark_job_checked(job_id: int):
    """Flags a job as visually reviewed ('Checked') so the dashboard grays it
    out — the user knows they already looked at it. Status is untouched."""
    success = mark_job_checked(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Job not found")
    await job_scheduler.broadcast_event("JOB_STATUS_UPDATED", {"job_id": job_id, "checked": True})
    return {"status": "success", "message": f"Job #{job_id} marked as checked"}

@app.delete("/api/jobs/{job_id}/checked")
async def api_clear_job_checked(job_id: int):
    """Removes the grayed-out 'Checked' flag, restoring the card's normal look."""
    success = clear_job_checked(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Job not found")
    await job_scheduler.broadcast_event("JOB_STATUS_UPDATED", {"job_id": job_id, "checked": False})
    return {"status": "success", "message": f"Job #{job_id} unchecked"}

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

@app.get("/api/cron-check")
async def api_cron_check(request: Request):
    """Vercel Cron entrypoint: runs the same Gmail check cycle on a schedule.

    Vercel automatically sends 'Authorization: Bearer <CRON_SECRET>' when the
    CRON_SECRET env var is set on the project, so we validate that header when
    a secret is configured. GET is used because Vercel Cron only issues GETs.
    """
    import os as _os
    secret = _os.getenv("CRON_SECRET", "")
    if secret:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {secret}":
            raise HTTPException(status_code=401, detail="Invalid cron secret")
    result = await job_scheduler.run_email_check_cycle(triggered_by="cron")

    # Daily digest: when the 9:03 AM IST cron runs, push a summary of what is
    # waiting on the dashboard to the phone via ntfy (new + applied counts).
    try:
        stats = get_dashboard_stats()
        total = stats.get("total_jobs", 0)
        new_count = stats.get("new_jobs", 0)
        applied = stats.get("applied_jobs", 0)
        title = f"📬 Daily Job Digest: {new_count} new of {total}"
        message = (
            f"Dashboard status at 9 AM IST:\n"
            f"• New jobs waiting: {new_count}\n"
            f"• Applied: {applied}\n"
            f"• Total tracked: {total}\n\n"
            "Open the dashboard to review and apply."
        )
        notification_service.send_mobile_push(
            title=title,
            message=message,
            priority="default",
            tags="chart_with_upwards_trend,briefcase",
        )
    except Exception as e:
        logger.warning(f"Cron digest push failed (cycle result unaffected): {e}")
    return result

try:
    api_cron_check.__dict__["_vercel_max_duration"] = 60
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

        # Same duplicate rule as the live scheduler: never re-post a job that
        # is already on the dashboard (same normalized apply URL, or same
        # company + similar title).
        existing_dup = find_existing_duplicate(item.get("apply_url", ""), company, title)
        if existing_dup:
            logger.info(
                f"Simulator skipped duplicate job posting: '{title}' @ {company} "
                f"(already posted as job #{existing_dup['id']})"
            )
            continue

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

    skipped = len(extracted_jobs) - len(new_jobs_list)
    message = f"Processed {len(new_jobs_list)} job openings from {sender}."
    if skipped > 0:
        message += f" ({skipped} duplicate(s) already on the dashboard were skipped.)"

    return {
        "status": "success",
        "message": message,
        "jobs": new_jobs_list
    }


@app.post("/api/simulate-custom-job")
async def api_simulate_custom_job(req: CustomJobRequest):
    """Creates ONE job card from user-supplied fields (Custom Job form in the
    Simulate modal). Goes through the same duplicate filter as real alerts."""
    title = (req.job_title or "").strip() or "Unknown Role"
    company = (req.company_name or "").strip() or "Unknown Company"
    platform = (req.source_platform or "").strip() or "Direct"

    # A working search URL is generated when the user leaves Apply URL blank.
    apply_url = (req.apply_url or "").strip()
    if not apply_url.startswith("http"):
        apply_url = generate_working_apply_url(title, company, platform)

    dup_check = job_matcher.check_duplicate_and_history(company, title)
    existing_dup = find_existing_duplicate(apply_url, company, title)
    if existing_dup:
        return {
            "status": "success",
            "duplicate": True,
            "message": f"⚠️ Not added: this job is already on the dashboard (job #{existing_dup['id']} '{existing_dup['job_title']}' @ {existing_dup['company_name']}).",
            "jobs": []
        }

    skills = req.skills
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(",") if s.strip()]
    elif not isinstance(skills, list):
        skills = []

    msg_id = f"custom_{datetime.now(timezone.utc).timestamp()}"
    job_record = {
        "message_id": msg_id,
        "email_subject": f"Manual job entry: {title} @ {company}",
        "email_sender": "custom@dashboard.local",
        "source_platform": platform,
        "date_received": datetime.now(timezone.utc).isoformat(),
        "job_title": title,
        "company_name": company,
        "location": (req.location or "").strip() or "Remote / Unspecified",
        "job_type": (req.job_type or "").strip() or "Full-time",
        "apply_url": apply_url,
        "salary": (req.salary or "").strip() or "Not specified",
        "skills": skills,
        "experience_level": (req.experience_level or "").strip() or "Not specified",
        "summary": (req.summary or "").strip(),
        "raw_email_snippet": "Manually added via the dashboard Custom Job form.",
        "status": "NEW",
        "applied_earlier": dup_check["applied_earlier"],
        "previous_application_id": dup_check["previous_application_id"],
        "previous_applied_date": dup_check["previous_applied_date"],
        "previous_job_title": dup_check["previous_job_title"],
        "match_score": 100
    }

    job_id = insert_job(job_record)
    job_record["id"] = job_id

    notification_service.notify_check_result(1, [job_record])
    await job_scheduler.broadcast_event("NEW_JOB_RECEIVED", {"job": job_record})

    return {
        "status": "success",
        "duplicate": False,
        "message": f"✨ Custom job added: '{title}' @ {company} (#{job_id}).",
        "jobs": [job_record]
    }


@app.post("/api/jobs/dedupe")
async def api_dedupe_jobs():
    """One-click cleanup: removes already-posted duplicates, keeping the oldest
    copy of each job. Returns how many rows were deleted."""
    deleted = dedupe_existing_jobs()
    if deleted:
        await job_scheduler.broadcast_event("JOBS_BULK_UPDATED", {"action": "dedupe", "count": deleted})
    return {
        "status": "success",
        "message": f"Removed {deleted} duplicate job(s)." if deleted else "No duplicates found — dashboard is clean.",
        "deleted": deleted
    }

# --- Tailored Resume Endpoints ---------------------------------------------

@app.post("/api/jobs/{job_id}/tailor-resume")
async def api_tailor_resume(job_id: int, force: Optional[str] = None):
    """Generates the job-tailored resume PDF for one job posting.

    Saves '<nn>_<Job_Name>_<Company_Name>.pdf' to the configured output
    folder (D:\\TaileredResume by default) plus the server-side cache, and
    caches the PDF bytes in the database for the View/Download buttons.
    Pass ?force=true to regenerate even if a tailored resume already exists."""
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    api_key = ai_extractor.get_api_key()
    result = resume_tailor.generate_tailored_resume(
        job, force=(str(force).lower() == "true"), gemini_api_key=api_key
    )
    if result.get("status") != "success":
        raise HTTPException(status_code=500, detail=result.get("error", "Resume generation failed"))

    # pdf_bytes is binary: it is served via the view/download endpoints, not JSON.
    result.pop("pdf_bytes", None)

    try:
        await job_scheduler.broadcast_event("JOB_RESUME_READY", {
            "job_id": job_id, "file_name": result["file_name"]
        })
    except Exception:
        pass
    return result


def _resume_file_response(job_id: int, download: bool):
    """Streams the tailored PDF for a job: DB blob first, file cache second."""
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    file_name = job.get("resume_file") or ""
    pdf_bytes: Optional[bytes] = None

    cached = get_tailored_resume(job)
    if cached:
        file_name = cached.get("file_name") or file_name
        data = cached.get("pdf_bytes")
        if isinstance(data, memoryview):
            data = data.tobytes()
        pdf_bytes = bytes(data) if data else None

    if not pdf_bytes and file_name:
        for folder in (RESUME_CACHE_DIR,):
            candidate = folder / file_name
            if candidate.exists():
                pdf_bytes = candidate.read_bytes()
                break

    if not pdf_bytes:
        raise HTTPException(status_code=404, detail=
            "No tailored resume for this job yet. Click 'Tailor Resume' first.")

    media = "application/pdf"
    if download:
        return Response(
            content=pdf_bytes, media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{file_name}"'}
        )
    return Response(content=pdf_bytes, media_type=media,
                    headers={"Content-Disposition": f'inline; filename="{file_name}"'})


@app.get("/api/jobs/{job_id}/resume/view")
async def api_view_resume(job_id: int):
    """Opens the job's tailored resume PDF inline in the browser."""
    return _resume_file_response(job_id, download=False)


@app.get("/api/jobs/{job_id}/resume/download")
async def api_download_resume(job_id: int):
    """Downloads the job's tailored resume PDF with its unique file name."""
    return _resume_file_response(job_id, download=True)


@app.get("/api/stats")
async def api_get_stats():
    stats = get_dashboard_stats()
    stats["next_check_at"] = get_setting("next_check_at", "")
    stats["check_interval_mins"] = get_setting("check_interval_mins", "15")
    stats["target_email"] = get_setting("target_email", "deepak.gvit@gmail.com")
    stats["auth_mode"] = get_setting("auth_mode", "simulator")
    # Never surface a stale tunnel URL (e.g. an old trycloudflare.com link in
    # the settings table). The banner, QR code and test-notification button
    # all show the effective URL: production URL on Vercel, fresh tunnel or
    # localhost when running locally.
    stats["public_url"] = notification_service.get_effective_dashboard_url()
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
    has_password = bool(str(settings.get("imap_password", "")).strip())
    # Security: never send the real App Password to the browser — only a flag
    # saying whether one is stored, plus a masked placeholder for display.
    settings.pop("imap_password", None)
    settings["imap_password_set"] = has_password
    if has_password:
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
