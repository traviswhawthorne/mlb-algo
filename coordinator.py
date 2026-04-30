#!/usr/bin/env python3
"""
MLB Coordinator — runs via GitHub Actions every 20 minutes during game hours.

Picks phase:  when a game is 20-45 min from first pitch and has pending data
              (TBD pitcher, no lineup), run run.py and email picks.
Results phase: once all games are Final and 2+ hours have passed, run
              results.py + tracker.py and email P&L.

State persists via data/state_YYYY-MM-DD.json committed back to the repo.
"""

import os
import sys
import json
import subprocess
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone, date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
HERE = os.path.dirname(os.path.abspath(__file__))


# ── State ────────────────────────────────────────────────────────────────────

def _today():
    return date.today().strftime("%Y-%m-%d")

def _utcnow():
    return datetime.now(timezone.utc)

def _state_path(d=None):
    return os.path.join(DATA_DIR, f"state_{d or _today()}.json")

def load_state():
    path = _state_path()
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {
        "date": _today(),
        "picks_runs": [],
        "incomplete_at_run": [],
        "results_done": False,
        "tracker_done": False,
    }

def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_state_path(), "w") as f:
        json.dump(state, f, indent=2)


# ── Game helpers ──────────────────────────────────────────────────────────────

def _game_start_utc(game):
    ts = game.get("game_time", "")
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return _utcnow() + timedelta(hours=24)

def _data_complete(game):
    """True when pitcher names are known and both lineups have 8+ batters."""
    p1 = game.get("away_pitcher")
    p2 = game.get("home_pitcher")
    has_pitchers = bool(p1 and p1 != "TBD" and p2 and p2 != "TBD")
    has_lineups  = (len(game.get("away_lineup_ids", [])) >= 8 and
                    len(game.get("home_lineup_ids", [])) >= 8)
    return has_pitchers and has_lineups

def _all_games_final():
    """Call MLB API to check if every game today has status = Final."""
    import requests as req
    today = _today()
    try:
        url  = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={today}&gameType=R"
        data = req.get(url, timeout=15).json()
        for day in data.get("dates", []):
            for g in day.get("games", []):
                if g.get("status", {}).get("abstractGameState") != "Final":
                    return False
        return True
    except Exception as e:
        print(f"  [coord] all_games_final error: {e}")
        return False


# ── Subprocess runners ────────────────────────────────────────────────────────

def _run(script, *args):
    cmd = [sys.executable, os.path.join(HERE, script)] + list(args)
    print(f"  [coord] Running: {' '.join(os.path.basename(c) for c in cmd)}")
    result = subprocess.run(cmd, cwd=HERE)
    if result.returncode != 0:
        print(f"  [coord] WARNING: {script} exited with code {result.returncode}")
    return result.returncode


# ── Gmail ─────────────────────────────────────────────────────────────────────

def _send_email(subject, body):
    user     = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not password:
        print(f"  [coord] Gmail not configured — skipping: {subject}")
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = user
        msg["To"]      = user
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
        print(f"  [coord] Email sent: {subject}")
    except Exception as e:
        print(f"  [coord] Email error: {e}")


def _build_picks_email(picks_date, pass_num, total_games):
    picks_file = os.path.join(DATA_DIR, f"picks_{picks_date}.json")
    if not os.path.exists(picks_file):
        return f"{total_games} game(s) today | picks pass {pass_num} complete"

    with open(picks_file) as f:
        picks = json.load(f)

    games     = picks.get("games", [])
    all_bets  = [(g, b) for g in games for b in g.get("bets", [])]
    n_priority = sum(1 for _, b in all_bets if b.get("priority"))
    n_fade     = sum(1 for _, b in all_bets if b.get("fade"))

    lines = [
        f"{total_games} game(s) today | {len(all_bets)} pick(s) | "
        f"{n_priority} priority ★ | {n_fade} fade watch ⚠",
        ""
    ]

    def _bet_line(g, b):
        matchup = g.get("matchup", f"{g['away_team']} @ {g['home_team']}")
        label   = f"{b['team']}  {b.get('bet_type_label', '')}".rstrip()
        tag     = ""
        if b.get("priority"): tag += " ★"
        if b.get("fade"):     tag += " ⚠"
        return f"  {matchup}\n    {label:<30}  {b['book_odds']:+d}  EV {b['ev_pct']}  ${b['bet_amount']:.2f}{tag}"

    priority_pairs = [(g, b) for g, b in all_bets if b.get("priority")]
    other_pairs    = [(g, b) for g, b in all_bets if not b.get("priority")]

    if priority_pairs:
        lines.append("PRIORITY (★):")
        for g, b in priority_pairs:
            lines.append(_bet_line(g, b))
        lines.append("")

    if other_pairs:
        lines.append("Other picks:")
        for g, b in other_pairs:
            lines.append(_bet_line(g, b))

    return "\n".join(lines)


def _build_results_email(picks_date):
    picks_file = os.path.join(DATA_DIR, f"picks_{picks_date}.json")
    if not os.path.exists(picks_file):
        return "Results and tracker processing complete. Check Google Drive for details."

    try:
        sys.path.insert(0, HERE)
        from results import fetch_scores, get_margin_and_result, calc_profit

        with open(picks_file) as f:
            picks = json.load(f)

        game_pks = [g["game_pk"] for g in picks.get("games", []) if g.get("game_pk")]
        scores   = fetch_scores(game_pks)

        wins = losses = 0
        total_pl = 0.0
        bet_lines = []

        for game in picks.get("games", []):
            pk    = game.get("game_pk")
            score = scores.get(pk)
            if not score:
                continue
            for bet in game.get("bets", []):
                margin, result = get_margin_and_result(
                    bet, game["away_team"], score["away_score"], score["home_score"])
                if result in ("WIN", "LOSS"):
                    pl = calc_profit(result, bet["bet_amount"], bet["book_odds"])
                    total_pl += pl
                    wins   += result == "WIN"
                    losses += result == "LOSS"
                    label   = f"{bet['team']} {bet.get('bet_type_label', '')}".rstrip()
                    bet_lines.append(f"  • {label:<32}  {bet['book_odds']:+d}  {result}  ${pl:+.2f}")

        lines = [f"{wins + losses} bet(s) | {wins}W {losses}L | ${total_pl:+.2f}", ""]
        lines.extend(bet_lines)
        return "\n".join(lines)

    except Exception as e:
        return f"Results processed. Error building summary: {e}"


# ── Drive upload ──────────────────────────────────────────────────────────────
# NOTE: Google Drive upload is disabled — service accounts cannot create files
# in personal Google Drive (no storage quota). The email is the primary output.
# The picks JSON is committed back to the repo after each run.

def _upload_to_drive(local_path):
    pass


# ── Git commit ────────────────────────────────────────────────────────────────

def _commit_state():
    try:
        subprocess.run(["git", "config", "user.email", "mlb-bot@github-actions.com"],
                       cwd=HERE, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "MLB Bot"],
                       cwd=HERE, check=True, capture_output=True)
        subprocess.run(["git", "add", "data/"], cwd=HERE, check=True)
        result = subprocess.run(
            ["git", "commit", "-m", f"coordinator: update {_today()}"],
            cwd=HERE, capture_output=True, text=True
        )
        if result.returncode == 0:
            subprocess.run(["git", "push"], cwd=HERE, check=True)
            print("  [coord] State committed and pushed to repo.")
        elif "nothing to commit" in (result.stdout + result.stderr):
            print("  [coord] Nothing new to commit.")
        else:
            print(f"  [coord] git commit failed: {result.stderr.strip()}")
    except Exception as e:
        print(f"  [coord] git error: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    from games_fetcher import get_todays_games

    now = _utcnow()
    picks_date = _today()

    print(f"\n{'='*62}")
    print(f"  MLB COORDINATOR — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*62}")

    state = load_state()
    games = get_todays_games()

    if not games:
        print("[coord] No games today — exiting.")
        return

    print(f"[coord] {len(games)} game(s) found for {picks_date}.")

    # ── PICKS PHASE ───────────────────────────────────────────────────────────
    covered_pks    = {pk for run in state["picks_runs"] for pk in run["game_pks"]}
    incomplete_pks = set(state.get("incomplete_at_run", []))

    upcoming_trigger = [
        g for g in games
        if _game_start_utc(g) > now                              # not yet started
        and _game_start_utc(g) - now <= timedelta(minutes=45)   # within window
        and (g["game_pk"] not in covered_pks                    # not yet covered
             or g["game_pk"] in incomplete_pks)                 # or was incomplete last run
    ]

    if upcoming_trigger:
        pass_num = len(state["picks_runs"]) + 1
        pks      = [g["game_pk"] for g in upcoming_trigger]
        print(f"[coord] Picks pass {pass_num}: {len(upcoming_trigger)} game(s) "
              f"approaching (pks: {pks})")

        _run("run.py")

        output_file = os.environ.get("OUTPUT_FILE", "MLB_Picks.xlsx")
        _upload_to_drive(os.path.join(HERE, output_file))

        state["picks_runs"].append({"ran_at": now.isoformat(), "game_pks": pks})
        state["incomplete_at_run"] = [
            g["game_pk"] for g in upcoming_trigger if not _data_complete(g)
        ]
        save_state(state)

        body = _build_picks_email(picks_date, pass_num, len(games))
        _send_email(f"MLB Picks — {picks_date} (Pass {pass_num})", body)

        _commit_state()
        return

    # ── RESULTS PHASE ─────────────────────────────────────────────────────────
    if state.get("results_done"):
        print("[coord] Results already processed today — nothing to do.")
        return

    # Check if any picks were run today (no point scoring if we never ran)
    if not state["picks_runs"]:
        print("[coord] No picks runs recorded today — skipping results phase.")
        return

    last_start = max(_game_start_utc(g) for g in games)
    elapsed_min = int((now - last_start).total_seconds() / 60)
    if now < last_start + timedelta(hours=2):
        wait_min = int((last_start + timedelta(hours=2) - now).total_seconds() / 60)
        print(f"[coord] Last game started {elapsed_min}m ago — "
              f"need 2h buffer. Check again in ~{wait_min}m.")
        return

    if not _all_games_final():
        print("[coord] Games still in progress — will check again next poll.")
        return

    print("[coord] All games Final. Running results + tracker ...")
    _run("results.py")
    _run("tracker.py")

    output_file  = os.environ.get("OUTPUT_FILE", "MLB_Picks.xlsx")
    results_file = output_file.replace("MLB_Picks.xlsx", f"MLB_Results_{picks_date}.xlsx")
    tracker_file = output_file.replace("MLB_Picks.xlsx", "MLB_Tracker.xlsx")
    _upload_to_drive(os.path.join(HERE, results_file))
    _upload_to_drive(os.path.join(HERE, tracker_file))

    state["results_done"] = True
    state["tracker_done"] = True
    save_state(state)

    body = _build_results_email(picks_date)
    _send_email(f"MLB Results — {picks_date}", body)

    _commit_state()


if __name__ == "__main__":
    main()
