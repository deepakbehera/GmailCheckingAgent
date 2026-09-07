import subprocess
import os
import sys

# Ensure UTF-8 output
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

def deploy_to_vercel():
    print("=" * 65)
    print("▲ Deploying AI Gmail Job Agent to Vercel")
    print("=" * 65)
    print("Running: npx -y vercel --prod\n")

    try:
        # Run npx vercel in interactive/auto mode
        subprocess.run(["npx", "-y", "vercel", "--prod"], cwd=PROJECT_DIR, check=True, shell=True)
        print("\n🎉 Deployment completed! Check your Vercel Dashboard at:")
        print("👉 https://vercel.com/deepakbeheras-projects")
    except subprocess.CalledProcessError as e:
        print(f"\n⚠️ Deployment process returned code {e.returncode}.")
        print("To deploy manually, open a terminal in this folder and run:")
        print("npx vercel")
    except Exception as e:
        print(f"Error launching Vercel CLI: {e}")

if __name__ == "__main__":
    deploy_to_vercel()
