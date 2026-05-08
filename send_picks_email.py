"""
Standalone email sender — called by the email_picks.yml workflow.
Reads picks from data/picks_YYYY-MM-DD.json and sends via Gmail.
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(__file__))
from coordinator import _build_picks_email, _send_email

picks_date = os.environ.get("PICKS_DATE") or date.today().strftime("%Y-%m-%d")
pass_num   = int(os.environ.get("PASS_NUM") or 1)

body, html = _build_picks_email(picks_date, pass_num, 0)
_send_email(f"MLB Picks — {picks_date} (Pass {pass_num})", body, html)
