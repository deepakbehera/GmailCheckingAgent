import subprocess
import requests
import json
import logging
import os
import platform
import urllib.parse
from email.header import Header
from typing import Optional, Dict, Any, List
from config import DEFAULT_NTFY_TOPIC, PORT, PUBLIC_DASHBOARD_URL
from database import get_setting
from tunnel_service import tunnel_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def sanitize_header_text(text: str) -> str:
    """Encodes or strips non-latin1 characters so requests headers don't raise UnicodeEncodeError."""
    if not text:
        return ""
    try:
        # Test if latin-1 safe
        text.encode('latin-1')
        return text
    except UnicodeEncodeError:
        # Remove emojis or encode as MIME word
        return Header(text, 'utf-8').encode()

class NotificationService:
    def __init__(self):
        pass

    def get_effective_dashboard_url(self) -> str:
        """Returns the URL used for the ntfy 'Open Dashboard' action button.

        Priority:
          1. The production deployment URL (PUBLIC_DASHBOARD_URL) — always used
             so phone notifications never point at a dead cloudflared tunnel.
          2. When running locally AND a fresh tunnel URL exists in settings
             (this machine started cloudflared this session), that URL.
          3. Localhost otherwise.

        Only a trycloudflare.com / loca.lt URL that was written during THIS
        process lifetime is considered live; anything else in the settings
        table is stale and would 530 on the phone."""
        pub_url = get_setting("public_url", "").strip()
        if self._tunnel_url_is_fresh(pub_url):
            return pub_url
        if os.getenv("VERCEL"):
            return PUBLIC_DASHBOARD_URL
        return f"http://localhost:{PORT}"

    @staticmethod
    def _tunnel_url_is_fresh(url: str) -> bool:
        """True only when `url` is a tunnel URL set by THIS process (the
        tunnel_service writes it right after the tunnel comes up) and the
        tunnel subprocess is still alive."""
        if not url or not url.startswith("http"):
            return False
        host = urllib.parse.urlsplit(url).netloc.lower()
        is_tunnel_host = host.endswith(".trycloudflare.com") or host.endswith(".loca.lt")
        process_alive = bool(tunnel_service.process and tunnel_service.process.poll() is None)
        return bool(tunnel_service.is_running and process_alive
                    and tunnel_service.public_url
                    and tunnel_service.public_url.strip().rstrip("/") == url.strip().rstrip("/")
                    and is_tunnel_host)

    def send_desktop_notification(self, title: str, message: str, url: Optional[str] = None):
        """Sends a native Windows desktop toast/balloon notification."""
        desktop_enabled = get_setting("desktop_notify", "true").lower() == "true"
        if not desktop_enabled:
            return

        clean_title = title.replace('"', '`"').replace("'", "''")
        clean_message = message.replace('"', '`"').replace("'", "''")

        try:
            if platform.system() == "Windows":
                ps_script = f"""
                [void] [System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms")
                $objNotifyIcon = New-Object System.Windows.Forms.NotifyIcon
                $objNotifyIcon.Icon = [System.Drawing.SystemIcons]::Information
                $objNotifyIcon.BalloonTipIcon = "Info"
                $objNotifyIcon.BalloonTipTitle = "{clean_title}"
                $objNotifyIcon.BalloonTipText = "{clean_message}"
                $objNotifyIcon.Visible = $True
                $objNotifyIcon.ShowBalloonTip(7000)
                Start-Sleep -Seconds 1
                $objNotifyIcon.Dispose()
                """
                subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script], 
                                 creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
                logger.info(f"Desktop notification triggered: {title}")
        except Exception as e:
            logger.warning(f"Failed to trigger desktop notification: {e}")

    def send_mobile_push(self, title: str, message: str, apply_url: Optional[str] = None, tags: Optional[str] = "briefcase,incoming_envelope", priority: str = "default") -> bool:
        """Sends an instant push notification to mobile devices via ntfy.sh."""
        mobile_enabled = get_setting("mobile_notify", "true").lower() == "true"
        if not mobile_enabled:
            return False

        topic = get_setting("ntfy_topic", DEFAULT_NTFY_TOPIC).strip()
        if not topic:
            topic = DEFAULT_NTFY_TOPIC

        ntfy_url = f"https://ntfy.sh/{topic}"

        # Ensure header values are ASCII/MIME encoded for HTTP specification
        safe_title = sanitize_header_text(title)

        headers = {
            "Title": safe_title,
            "Priority": priority,
            "Tags": tags,
        }

        actions = []
        if apply_url and apply_url.startswith("http"):
            actions.append(f"view, Apply Online, {apply_url}")
        
        # Action to open Public Dashboard on mobile
        dashboard_url = self.get_effective_dashboard_url()
        actions.append(f"view, Open Dashboard, {dashboard_url}")

        if actions:
            headers["Actions"] = "; ".join(actions)

        try:
            response = requests.post(ntfy_url, data=message.encode("utf-8"), headers=headers, timeout=8)
            if response.status_code == 200:
                logger.info(f"Mobile push sent to ntfy.sh/{topic} (Dashboard URL: {dashboard_url})")
                return True
            else:
                logger.warning(f"ntfy.sh responded with {response.status_code}: {response.text}")
                return False
        except Exception as e:
            logger.warning(f"Failed to send mobile push notification: {e}")
            return False

    def notify_check_result(self, email_count: int, new_jobs: List[Dict[str, Any]]):
        """Unified notification dispatcher after a 15-minute email check."""
        if not new_jobs and email_count == 0:
            title = "Gmail Agent Check"
            message = "Checked deepak.gvit@gmail.com — No new emails received."
            self.send_desktop_notification(title, message)
            return

        if not new_jobs and email_count > 0:
            title = "Gmail Agent Check"
            message = f"Scanned {email_count} new email(s). None contained job openings."
            self.send_desktop_notification(title, message)
            return

        # When new job openings are detected!
        if len(new_jobs) == 1:
            job = new_jobs[0]
            platform = job.get("source_platform", "Email")
            title = f"[{platform}] {job.get('job_title')} @ {job.get('company_name')}"
            
            repeat_flag = " ⚠️ (Applied to this company before!)" if job.get("applied_earlier") else ""
            loc = f" | {job.get('location')}" if job.get('location') else ""
            sal = f" | {job.get('salary')}" if job.get('salary') and job.get('salary') != "Not specified" else ""
            
            msg = f"{job.get('company_name')}{loc}{sal}{repeat_flag}\nClick to view and apply."
            
            self.send_desktop_notification(title, msg, job.get("apply_url"))
            self.send_mobile_push(
                title=title,
                message=f"Location: {job.get('location', 'N/A')}\nSalary: {job.get('salary', 'N/A')}\nSource: {platform}{repeat_flag}\n\n{job.get('summary', '')[:120]}...",
                apply_url=job.get("apply_url"),
                priority="high",
                tags="tada,briefcase"
            )
        else:
            title = f"{len(new_jobs)} New Job Openings Detected!"
            sources = list(set([j.get('source_platform', 'Job Alert') for j in new_jobs]))
            companies = ", ".join(list(set([j.get('company_name', 'Unknown') for j in new_jobs]))[:3])
            
            msg = f"Found {len(new_jobs)} opportunities via {', '.join(sources)} from {companies} and more. Open your dashboard to view & apply!"
            
            self.send_desktop_notification(title, msg)
            self.send_mobile_push(
                title=title,
                message=msg,
                apply_url=new_jobs[0].get("apply_url") if new_jobs else None,
                priority="high",
                tags="fire,briefcase"
            )

notification_service = NotificationService()
