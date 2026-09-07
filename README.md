# 📬 AI Gmail Job Hunter & Notification Agent

An intelligent, autonomous AI agent that monitors **`deepak.gvit@gmail.com`** every 15 minutes, checks incoming emails for new job openings using Gemini AI, sends instant desktop and mobile notifications, tracks duplicate/past company application history, and presents a consolidated local web dashboard with automatic 15-minute refresh and strikethrough/red status styling for applied jobs.

---

## 🌟 Key Features

1. **🕒 15-Minute Automated Checks**:
   - Background scheduler checks Gmail every 15 minutes.
   - Live 15-minute countdown clock on the dashboard with auto-refresh and Server-Sent Events (SSE).

2. **🔔 Dual Desktop & Mobile Notifications**:
   - **Desktop (Windows)**: Native Windows notifications alerting whether new emails or job openings arrived on every check cycle.
   - **Mobile (Push via ntfy.sh)**: Free, zero-setup instant push notifications with **1-Click Apply** buttons directly on your iPhone or Android phone.

3. **🤖 Gemini AI Job Classification & Extraction**:
   - Classifies if incoming emails are job openings or application confirmations.
   - Extracts Job Title, Company, Location, Compensation, Tech Stack, and direct Application Link.
   - Filters out non-job emails (security alerts, receipts, marketing).

4. **⚠️ Duplicate Company & Past Application Tracking**:
   - Cross-references previous applications in the local SQLite database.
   - Alerts you if you have applied to the same company or a similar role previously, showing past applied dates.

5. **🎯 Strikethrough & Red Marker for Applied Jobs**:
   - When a job is marked as **Applied** (or when an application confirmation email is received), the job is **struck through with a red line** and prominently labeled with a pulsing **`ALREADY APPLIED`** red badge.

6. **💻 Consolidated Glassmorphic Local Dashboard**:
   - Fast filtering (*All*, *Open Roles*, *Applied / Strikethrough*).
   - Real-time search across job title, company, skills, and location.
   - Built-in **"Check Gmail Now"** and **"Simulate Job Email"** buttons for one-click testing.

---

## 🚀 Quick Start

### 1. Launch the Dashboard
Run the Python launcher or double-click `start.bat`:
```bash
python run.py
```
This automatically starts the server and opens the local dashboard in your default browser at:
👉 **`http://localhost:8000`**

---

## ⚙️ Configuration & Credentials

Open the **⚙️ Settings** modal on the top right of the dashboard:

### A. Email Ingestion Options:
- **Simulator Mode (Default)**: Preloaded with realistic job email streams (LinkedIn, recruiters, tech firms) for instant testing.
- **Gmail IMAP (Recommended for live email)**:
  1. Go to your [Google Account Security](https://myaccount.google.com/security).
  2. Enable **2-Step Verification**.
  3. Go to **App passwords** (search "App passwords" in Google account search).
  4. Generate an app password named `JobAgent` (16 characters, e.g. `abcd efgh ijkl mnop`).
  5. Paste the password into the settings modal or `.env` file under `GMAIL_APP_PASSWORD`.

### B. Gemini API Key (Optional for Smart AI Extraction):
- Enter your Gemini API key in the Settings modal or in `.env` as `GEMINI_API_KEY=AIzaSy...`.
- If no key is set, the agent automatically uses built-in pattern and heuristic AI parsers.

### C. Mobile Notifications (ntfy.sh):
1. Click **📱 Mobile Sync** in the dashboard header.
2. Scan the displayed QR code with your phone camera, or install the free **ntfy** app and subscribe to `deepak-job-hunter-alerts`.
3. You will receive push alerts with direct apply links immediately!

---

## 📂 Project Architecture

```
c:\Users\beher\GmailCheckingAgent/
├── app.py                     # FastAPI REST & SSE Event Streaming Server
├── run.py                     # Launcher script with auto-browser opener
├── start.bat                  # One-click Windows starter batch file
├── config.py                  # Global configurations & environment loaders
├── database.py                # SQLite database management (jobs, logs, settings)
├── scheduler.py               # APScheduler 15-minute background check loop
├── email_service.py           # Gmail IMAP, OAuth2, and Simulator email ingestors
├── ai_extractor.py            # Gemini AI structured job extractor with fallback
├── job_matcher.py             # Duplicate company and past application matcher
├── notification_service.py    # Desktop Windows toast and Mobile ntfy.sh push
├── test_suite.py              # Automated test suite (all passed)
├── verify_live_server.py      # Live server endpoint verification script
├── requirements.txt           # Python dependencies
└── static/
    ├── index.html             # Glassmorphic single-page dashboard
    ├── css/style.css          # Modern dark UI, strikethrough & red badges
    └── js/app.js              # Real-time state, SSE, countdown, and modals
```

---

## 🧪 Testing

Run the automated test suite anytime:
```bash
python test_suite.py
```
Or verify the live server:
```bash
python verify_live_server.py
```
