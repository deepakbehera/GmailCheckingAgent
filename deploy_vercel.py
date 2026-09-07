import subprocess
import os
import sys
from dotenv import load_dotenv

# Ensure UTF-8 output
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

def deploy_to_vercel():
    print("=" * 65)
    print("▲ Deploying AI Gmail Job Agent to Vercel")
    print("Project Name: gmail-checking-agent")
    print("=" * 65)

    vc_token = os.getenv("VERCEL_TOKEN", "").strip()

    cmd = ["npx", "-y", "vercel", "--name", "gmail-checking-agent", "--prod", "--yes"]
    if vc_token:
        cmd.extend(["--token", vc_token])

    print(f"Running: {' '.join([c if c != vc_token else '***' for c in cmd])}\n")

    try:
        proc = subprocess.run(cmd, cwd=PROJECT_DIR, text=True, shell=True)
        if proc.returncode == 0:
            print("\n🎉 Deployment completed! Check your Vercel Dashboard at:")
            print("👉 https://vercel.com/deepakbeheras-projects")
        else:
            print(f"\n⚠️ Vercel deployment returned code {proc.returncode}.")
    except Exception as e:
        print(f"Error launching Vercel CLI: {e}")

if __name__ == "__main__":
    deploy_to_vercel()
