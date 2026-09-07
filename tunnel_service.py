import subprocess
import threading
import time
import re
import os
import logging
from pathlib import Path
from typing import Optional
from database import update_settings, get_setting
from config import BASE_DIR, PORT

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class PublicTunnelService:
    def __init__(self):
        self.public_url = ""
        self.process: Optional[subprocess.Popen] = None
        self.is_running = False

    def start_tunnel(self) -> str:
        """Starts a free public tunnel using Cloudflare or Localtunnel in a background thread."""
        if self.is_running and self.public_url:
            return self.public_url

        thread = threading.Thread(target=self._run_tunnel_process, daemon=True)
        thread.start()

        # Wait up to 10 seconds for public URL
        for _ in range(20):
            time.sleep(0.5)
            if self.public_url:
                logger.info(f"🚀 Public Tunnel Online: {self.public_url}")
                return self.public_url

        return self.public_url or f"http://localhost:{PORT}"

    def _run_tunnel_process(self):
        cloudflared_path = BASE_DIR / "cloudflared.exe"

        # Strategy 1: Cloudflare Quick Tunnel (Free, fast, HTTPS, zero signup)
        if cloudflared_path.exists():
            try:
                logger.info("Starting Cloudflare Quick Tunnel...")
                cmd = [str(cloudflared_path), "tunnel", "--url", f"http://127.0.0.1:{PORT}"]
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
                )
                self.is_running = True

                for line in iter(self.process.stdout.readline, ''):
                    # Look for https://*.trycloudflare.com
                    match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
                    if match:
                        url = match.group(0)
                        self.public_url = url
                        update_settings({"public_url": url})
                        logger.info(f"✅ Cloudflare Tunnel Active: {url}")
                        break

                # Keep process alive
                self.process.wait()
            except Exception as e:
                logger.warning(f"Cloudflare tunnel encountered error: {e}")

        # Strategy 2: Localtunnel fallback
        if not self.public_url:
            try:
                logger.info("Starting Localtunnel fallback...")
                cmd = ["npx", "-y", "localtunnel", "--port", str(PORT)]
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    shell=True,
                    bufsize=1
                )
                self.is_running = True

                for line in iter(self.process.stdout.readline, ''):
                    match = re.search(r'https://[a-zA-Z0-9-]+\.loca\.lt', line)
                    if match:
                        url = match.group(0)
                        self.public_url = url
                        update_settings({"public_url": url})
                        logger.info(f"✅ Localtunnel Active: {url}")
                        break

                self.process.wait()
            except Exception as e:
                logger.warning(f"Localtunnel failed: {e}")

    def stop_tunnel(self):
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass
        self.is_running = False
        self.public_url = ""
        update_settings({"public_url": ""})

tunnel_service = PublicTunnelService()
