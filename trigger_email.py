"""
Run after run.py to push today's picks JSON and trigger the GitHub email workflow.

Usage:
  python trigger_email.py             # today, pass 1
  python trigger_email.py 2026-05-05  # specific date
  python trigger_email.py 2026-05-05 2  # specific date + pass number
"""
import subprocess
import sys
from datetime import date

picks_date = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y-%m-%d")
pass_num   = sys.argv[2] if len(sys.argv) > 2 else "1"
picks_file = f"data/picks_{picks_date}.json"

# Push the picks JSON so the workflow can read it
subprocess.run(["git", "add", picks_file], check=True)
result = subprocess.run(
    ["git", "commit", "-m", f"picks: local run {picks_date}"],
    capture_output=True, text=True,
)
if result.returncode != 0 and "nothing to commit" not in (result.stdout + result.stderr):
    print(result.stdout, result.stderr)
    raise SystemExit("git commit failed")
subprocess.run(["git", "push"], check=True)

# Trigger the email-only workflow
subprocess.run([
    "gh", "workflow", "run", "email_picks.yml",
    "-f", f"date={picks_date}",
    "-f", f"pass_num={pass_num}",
], check=True)

print(f"Email workflow triggered for {picks_date} pass {pass_num}")
