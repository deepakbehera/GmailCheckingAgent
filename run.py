import uvicorn
import webbrowser
import threading
import time
import sys
from config import HOST, PORT

# Ensure emoji-rich console output works even when stdout is redirected
# (Windows defaults to cp1252, which cannot encode the rocket/emoji banners).
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

def open_browser():
    time.sleep(1.2)
    url = f"http://localhost:{PORT}"
    print(f"\n🌐 Opening AI Gmail Job Hunter Dashboard at {url} ...\n")
    webbrowser.open(url)

if __name__ == "__main__":
    print("=" * 65)
    print("🚀 AI Gmail Job Checking & Notification Agent")
    print(f"📧 Monitoring Target: deepak.gvit@gmail.com (Every 15 mins)")
    print(f"🖥️ Local Dashboard: http://localhost:{PORT}")
    print("=" * 65)

    # Launch browser automatically
    threading.Thread(target=open_browser, daemon=True).start()

    # Start FastAPI server
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False, log_level="info")
