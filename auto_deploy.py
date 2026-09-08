import os
import sys
import subprocess
import requests
import json
from pathlib import Path
from dotenv import load_dotenv

# Ensure UTF-8 output
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

GITHUB_USER = "deepakbehera"
REPO_NAME = "GmailCheckingAgent"

def run_git(cmd):
    res = subprocess.run(["git"] + cmd, cwd=str(BASE_DIR), capture_output=True, text=True, encoding='utf-8', errors='replace')
    return res.returncode == 0, res.stdout.strip(), res.stderr.strip()

def deploy_github(github_token: str):
    print("\n" + "=" * 60)
    print("🐙 Deploying to GitHub & Enabling GitHub Pages")
    print("=" * 60)

    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json"
    }

    # 1. Create Private Repository if it doesn't exist
    repo_url = f"https://api.github.com/repos/{GITHUB_USER}/{REPO_NAME}"
    r_check = requests.get(repo_url, headers=headers)
    
    if r_check.status_code == 404:
        print(f"Creating private repository '{REPO_NAME}' on GitHub...")
        create_payload = {
            "name": REPO_NAME,
            "private": True,
            "description": "AI Gmail Job Hunter Agent with multi-job parser and live dashboard"
        }
        r_create = requests.post("https://api.github.com/user/repos", headers=headers, json=create_payload)
        if r_create.status_code == 201:
            print("✅ Private GitHub repository created successfully!")
        else:
            print(f"⚠️ Failed to create repository: {r_create.status_code} - {r_create.text}")
    elif r_check.status_code == 200:
        print(f"✅ GitHub repository '{REPO_NAME}' already exists.")
    else:
        print(f"⚠️ GitHub API returned status {r_check.status_code}. Check your GITHUB_TOKEN permissions.")

    # 2. Configure Git Remote with Token & Push
    auth_remote = f"https://x-access-token:{github_token}@github.com/{GITHUB_USER}/{REPO_NAME}.git"
    run_git(["remote", "remove", "origin"])
    run_git(["remote", "add", "origin", auth_remote])
    
    print("Pushing 'main' branch to GitHub...")
    success, out, err = run_git(["push", "-u", "origin", "main"])
    if success:
        print(f"✅ Successfully pushed to https://github.com/{GITHUB_USER}/{REPO_NAME} !")
    else:
        print(f"⚠️ Push failed: {err}")

    # 3. Enable GitHub Pages on /docs
    print("\nEnabling GitHub Pages (pointing to /docs folder)...")
    pages_url = f"https://api.github.com/repos/{GITHUB_USER}/{REPO_NAME}/pages"
    pages_payload = {
        "source": {
            "branch": "main",
            "path": "/docs"
        }
    }
    r_pages = requests.post(pages_url, headers=headers, json=pages_payload)
    if r_pages.status_code in [201, 204, 409]:
        print(f"✅ GitHub Pages enabled!")
        print(f"👉 Live URL: https://{GITHUB_USER}.github.io/{REPO_NAME}/")
    else:
        print(f"ℹ️ GitHub Pages status ({r_pages.status_code}): {r_pages.text}")

def deploy_vercel(vercel_token: str):
    print("\n" + "=" * 60)
    print("▲ Deploying to Vercel (https://vercel.com/deepakbeheras-projects)")
    print("=" * 60)

    cmd = ["npx", "-y", "vercel", "--prod", "--yes"]
    if vercel_token:
        cmd.extend(["--token", vercel_token])

    try:
        proc = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, shell=True)
        if proc.returncode == 0:
            print("✅ Vercel Deployment Successful!")
            print(proc.stdout)
        else:
            print(f"⚠️ Vercel deploy output:\n{proc.stdout}\n{proc.stderr}")
    except Exception as e:
        print(f"Error running Vercel: {e}")

def main():
    gh_token = os.getenv("GITHUB_TOKEN", "").strip()
    vc_token = os.getenv("VERCEL_TOKEN", "").strip()

    if gh_token:
        deploy_github(gh_token)
    else:
        print("ℹ️ GITHUB_TOKEN not found in .env.")

    if vc_token:
        deploy_vercel(vc_token)
    else:
        print("ℹ️ VERCEL_TOKEN not found in .env.")

    if not gh_token and not vc_token:
        print("\n" + "=" * 65)
        print("🔑 HOW TO ADD CREDENTIALS TO .env FOR AUTOMATED DEPLOYMENT:")
        print("=" * 65)
        print("Run either of these commands in PowerShell in this directory:\n")
        print("1. To set GITHUB_TOKEN (for auto private repo creation & GitHub Pages):")
        print('   Add-Content .env "GITHUB_TOKEN=ghp_your_personal_access_token_here"')
        print("\n2. To set VERCEL_TOKEN (for direct deployment to your Vercel account):")
        print('   Add-Content .env "VERCEL_TOKEN=your_vercel_token_here"')
        print("\nThen run:")
        print("   python auto_deploy.py")
        print("=" * 65)

if __name__ == "__main__":
    main()
