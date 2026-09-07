import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import (
    is_message_already_processed,
    log_email_inspection,
    insert_job,
    log_check_run,
    get_setting,
    update_settings,
    find_previous_applications_for_company,
    update_job_status
)
from email_service import email_service
from ai_extractor import ai_extractor
from job_matcher import job_matcher
from notification_service import notification_service
from tunnel_service import tunnel_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class JobHunterScheduler:
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.event_subscribers: List[asyncio.Queue] = []
        self.is_running = False

    async def broadcast_event(self, event_type: str, data: Dict[str, Any]):
        """Broadcasts real-time events to connected frontend clients (SSE)."""
        payload = {"type": event_type, "data": data, "timestamp": datetime.now().isoformat()}
        dead_queues = []
        for q in self.event_subscribers:
            try:
                await q.put(payload)
            except Exception:
                dead_queues.append(q)
        
        for dq in dead_queues:
            if dq in self.event_subscribers:
                self.event_subscribers.remove(dq)

    async def run_email_check_cycle(self, triggered_by: str = "scheduler") -> Dict[str, Any]:
        """
        Executes one full 15-minute check cycle:
        1. Ingests emails from deepak.gvit@gmail.com (LinkedIn, Naukri, Indeed, Glassdoor, Direct)
        2. Extracts multiple job postings per email digest
        3. Cross-references duplicate / previous applications
        4. Saves all to SQLite
        5. Triggers Desktop & Mobile notifications with public URL
        6. Broadcasts to Web Dashboard
        """
        logger.info(f"--- Starting Email Check Cycle (Triggered by: {triggered_by}) ---")
        
        raw_emails = email_service.fetch_recent_emails()
        new_jobs_found = []
        emails_scanned = 0

        for em in raw_emails:
            msg_id = em["message_id"]
            if is_message_already_processed(msg_id):
                continue

            emails_scanned += 1
            subject = em["subject"]
            sender = em["sender"]
            body = em["body"]
            html_body = em.get("html_body", "")
            date_received = em.get("date_received", datetime.now().isoformat())

            # Multi-job extraction
            extracted_jobs = ai_extractor.analyze_email_multi(subject, sender, body, html_body)
            is_job = len(extracted_jobs) > 0

            # Log email check audit
            first_summary = extracted_jobs[0]["summary"] if extracted_jobs else "Regular non-job email."
            log_email_inspection(
                message_id=msg_id,
                sender=sender,
                subject=subject,
                date_received=date_received,
                is_job=is_job,
                summary=first_summary,
                count=len(extracted_jobs)
            )

            # Process each individual job extracted from the email digest
            for item in extracted_jobs:
                # If application confirmation received, auto-mark existing company job as APPLIED
                if item.get("is_application_confirmation"):
                    company = item.get("company_name", "")
                    if company and company != "Unknown Company":
                        past = find_previous_applications_for_company(company)
                        for pj in past:
                            if pj["status"] != "APPLIED":
                                update_job_status(pj["id"], "APPLIED", notes="Auto-marked via application confirmation.")

                company = item.get("company_name", "Unknown Company")
                title = item.get("job_title", "Specialist Role")
                platform = item.get("source_platform", "Direct")
                
                # Check previous applications for this company/role
                dup_check = job_matcher.check_duplicate_and_history(company, title)

                job_record = {
                    "message_id": f"{msg_id}_{len(new_jobs_found)}",
                    "email_subject": subject,
                    "email_sender": sender,
                    "source_platform": platform,
                    "date_received": date_received,
                    "job_title": title,
                    "company_name": company,
                    "location": item.get("location", "Remote"),
                    "job_type": item.get("job_type", "Full-time"),
                    "apply_url": item.get("apply_url", ""),
                    "salary": item.get("salary", "Not specified"),
                    "skills": item.get("skills", []),
                    "experience_level": item.get("experience_level", "Mid-Senior"),
                    "summary": item.get("summary", ""),
                    "raw_email_snippet": body[:500],
                    "status": "NEW",
                    "applied_earlier": dup_check["applied_earlier"],
                    "previous_application_id": dup_check["previous_application_id"],
                    "previous_applied_date": dup_check["previous_applied_date"],
                    "previous_job_title": dup_check["previous_job_title"],
                    "match_score": item.get("match_score", 85)
                }

                job_id = insert_job(job_record)
                job_record["id"] = job_id
                new_jobs_found.append(job_record)

        # Update check history & next check estimate
        interval_mins = int(get_setting("check_interval_mins", "15"))
        next_check = datetime.now() + timedelta(minutes=interval_mins)
        update_settings({"next_check_at": next_check.isoformat()})

        status_msg = f"Scanned {emails_scanned} emails, found {len(new_jobs_found)} new job(s)."
        log_check_run(emails_scanned, len(new_jobs_found), status_msg, triggered_by=triggered_by)

        # Trigger Desktop & Mobile Notifications
        notification_service.notify_check_result(emails_scanned, new_jobs_found)

        # Broadcast event to Web Dashboard
        await self.broadcast_event("CHECK_COMPLETED", {
            "emails_scanned": emails_scanned,
            "new_jobs_found": len(new_jobs_found),
            "jobs": new_jobs_found,
            "next_check_at": next_check.isoformat(),
            "status_message": status_msg
        })

        return {
            "emails_scanned": emails_scanned,
            "new_jobs_count": len(new_jobs_found),
            "jobs": new_jobs_found,
            "status": "success",
            "message": status_msg
        }

    def start(self):
        """Starts the background scheduler and ensures public tunnel is active."""
        if not self.scheduler.running:
            # Start public tunnel for mobile access
            tunnel_service.start_tunnel()

            interval_mins = int(get_setting("check_interval_mins", "15"))
            self.scheduler.add_job(
                self.run_email_check_cycle,
                "interval",
                minutes=interval_mins,
                id="email_check_job",
                replace_existing=True
            )
            self.scheduler.start()
            self.is_running = True
            logger.info(f"Background Job Hunter Scheduler started (Interval: {interval_mins} mins).")

    def update_interval(self, new_interval_mins: int):
        """Dynamically updates the schedule interval."""
        if self.scheduler.running:
            self.scheduler.reschedule_job("email_check_job", trigger="interval", minutes=new_interval_mins)
            update_settings({"check_interval_mins": str(new_interval_mins)})
            logger.info(f"Rescheduled check interval to {new_interval_mins} minutes.")

job_scheduler = JobHunterScheduler()
