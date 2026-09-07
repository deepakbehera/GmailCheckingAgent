import subprocess
import os
import sys

# Ensure UTF-8 output
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REPO_NAME = "GmailCheckingAgent"
GITHUB_USER = "deepakbehera"

def run_git(cmd):
    print(f">> git {' '.join(cmd)}")
    res = subprocess.run(["git"] + cmd, cwd=PROJECT_DIR, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if res.stdout:
        print(res.stdout.strip())
    if res.stderr and res.returncode != 0:
        print(f"Error: {res.stderr.strip()}")
    return res.returncode == 0

def setup_and_push(repo_name=DEFAULT_REPO_NAME, is_private=True):
    print("=" * 60)
    print("🐙 Initializing Git & Preparing GitHub Push")
    print(f"Repository: https://github.com/{GITHUB_USER}/{repo_name}")
    print("=" * 60)

    # Check if .git exists in project dir
    git_dir = os.path.join(PROJECT_DIR, ".git")
    if not os.path.exists(git_dir):
        run_git(["init", "-b", "main"])
    else:
        run_git(["checkout", "-B", "main"])

    # Configure user if needed
    run_git(["config", "user.name", GITHUB_USER])
    run_git(["config", "user.email", "deepak.gvit@gmail.com"])

    # Add all files
    run_git(["add", "."])
    run_git(["commit", "-m", "Initial commit: AI Gmail Job Checking Agent with multi-job parser and public mobile tunnel"])

    # Configure remote
    remote_url = f"https://github.com/{GITHUB_USER}/{repo_name}.git"
    run_git(["remote", "remove", "origin"])
    run_git(["remote", "add", "origin", remote_url])

    print("\n" + "=" * 60)
    print(f"✅ Local repository initialized and committed.")
    print(f"🚀 To push to your private GitHub repository:")
    print(f"1. Make sure you have created an empty private repository at:")
    print(f"   👉 https://github.com/new (Name: {repo_name}, Visibility: Private)")
    print(f"2. Then run this command in terminal:")
    print(f"   git push -u origin main")
    print("=" * 60)

    # Attempt push directly (uses Windows Git Credential Manager)
    print("\nAttempting git push to origin main...")
    pushed = run_git(["push", "-u", "origin", "main"])
    if pushed:
        print("\n🎉 Repository successfully pushed to GitHub!")
    else:
        print("\nℹ️ If the remote repository does not exist yet on GitHub, please create it as Private at https://github.com/new and run:")
        print(f"   cd {PROJECT_DIR}")
        print("   git push -u origin main")

if __name__ == "__main__":
    setup_and_push()
