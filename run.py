import uvicorn
import webbrowser
import threading
import time
import sys
from config import HOST, PORT

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
