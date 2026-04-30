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

import pytz
_ET = pytz.timezone("America/New_York")


# ── State ────────────────────────────────────────────────────────────────────

def _today():
    """Today's date in Eastern Time — MLB games run until ~1 AM ET."""
    return datetime.now(_ET).strftime("%Y-%m-%d")

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
    """Call MLB API to check if every game today (ET) has status = Final."""
    import requests as req
    today = _today()  # ET date
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

def _send_email(subject, body, html=None):
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
        if html:
            msg.add_alternative(html, subtype="html")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
        print(f"  [coord] Email sent: {subject}")
    except Exception as e:
        print(f"  [coord] Email error: {e}")


def _short_team(name):
    """'Los Angeles Angels' → 'Angels', 'Chicago White Sox' → 'White Sox'"""
    parts = name.split()
    if not parts:
        return name
    last = parts[-1]
    if last in ("Sox", "Jays") and len(parts) >= 2:
        return f"{parts[-2]} {last}"
    return last


def _parse_gametime(time_et):
    """'10:10 AM PT' → datetime for sorting."""
    try:
        return datetime.strptime(time_et.replace(" PT", "").strip(), "%I:%M %p")
    except Exception:
        return datetime.min


def _build_picks_email(picks_date, pass_num, total_games):
    picks_file = os.path.join(DATA_DIR, f"picks_{picks_date}.json")
    if not os.path.exists(picks_file):
        msg = f"{total_games} game(s) today | picks pass {pass_num} complete"
        return msg, f"<p>{msg}</p>"

    with open(picks_file) as f:
        picks = json.load(f)

    games    = picks.get("games", [])
    all_bets = [(g, b) for g in games for b in g.get("bets", [])]

    if not all_bets:
        msg = f"{total_games} game(s) today | no picks this pass"
        return msg, f"<p>{msg}</p>"

    n_priority = sum(1 for _, b in all_bets if b.get("priority"))
    n_fade     = sum(1 for _, b in all_bets if b.get("fade"))

    summary = (f"{total_games} game(s) today | {len(all_bets)} pick(s) | "
               f"{n_priority} priority ★ | {n_fade} fade ⚠")

    all_bets.sort(key=lambda x: _parse_gametime(x[0].get("game_time_et", "")))

    def _odds(o):
        try: return f"{int(o):+d}"
        except Exception: return str(o)

    def _data_status(g):
        """Data completeness: pitchers known + lineups confirmed."""
        ap = g.get("away_pitcher") or ""
        hp = g.get("home_pitcher") or ""
        pitchers_ok = ap not in ("", "TBD") and hp not in ("", "TBD")
        lineups_ok  = (g.get("away_lineup_count", 0) >= 8 and
                       g.get("home_lineup_count", 0) >= 8)
        if pitchers_ok and lineups_ok: return "Full"
        if not pitchers_ok:            return "No SP"
        return "No LU"

    def _flag(b):
        return ("★" if b.get("priority") else "") + ("⚠" if b.get("fade") else "")

    # Build rows: (time, matchup, pick, model, book, edge, status, flag, is_priority, is_fade)
    table_rows = []
    for g, b in all_bets:
        time_str = g.get("game_time_et", "").replace(" PT", "")
        matchup  = f"{_short_team(g.get('away_team', ''))} @ {_short_team(g.get('home_team', ''))}"
        if b.get("market") == "Total" or not b.get("bet_type_label"):
            pick = b.get("team", "")
        else:
            pick = f"{_short_team(b.get('team', ''))} {b.get('bet_type_label', '')}".strip()
        table_rows.append((
            time_str, matchup, pick,
            _odds(b.get("model_odds", "")),
            _odds(b.get("book_odds", "")),
            b.get("ev_pct", ""),
            _data_status(g),
            _flag(b),
            bool(b.get("priority")),
            bool(b.get("fade")),
        ))

    # ── Plain text ────────────────────────────────────────────────────────────
    display = [r[:8] for r in table_rows]
    headers = ("Time", "Matchup", "Pick", "Model", "Book", "Edge", "Status", "")
    cols    = list(zip(*display))
    widths  = [max(len(h), max(len(c) for c in col)) for h, col in zip(headers, cols)]

    def _fmt(vals):
        return "  ".join(f"{v:<{w}}" for v, w in zip(vals, widths)).rstrip()

    sep   = "  ".join("-" * w for w in widths)
    lines = [summary, "", _fmt(headers), sep] + [_fmt(r) for r in display]
    plain = "\n".join(lines)

    # ── HTML ──────────────────────────────────────────────────────────────────
    H  = "padding:8px 12px;font-size:13px;font-weight:bold;color:#fff;background:#1e3a5f;border:1px solid #1e3a5f;"
    HR = H + "text-align:right;"
    HC = H + "text-align:center;"
    HL = H + "text-align:left;"

    def _td(val, align="left", bg="#fff", extra=""):
        s = (f"padding:7px 12px;font-size:13px;border:1px solid #dde1e7;"
             f"text-align:{align};background:{bg};{extra}")
        return f'<td style="{s}">{val}</td>'

    def _status_color(s):
        return {"Full": "#22863a", "No LU": "#b36200", "No SP": "#cb2431"}.get(s, "#888")

    def _ev_color(ev):
        try:
            v = float(ev.replace("%", "").replace("+", ""))
            return "#22863a" if v >= 15 else "#2ea44f" if v >= 8 else "#333"
        except Exception:
            return "#333"

    rows_html = []
    for i, r in enumerate(table_rows):
        time_s, matchup, pick, model, book, edge, status, flag, is_pri, is_fade = r
        if is_pri:
            bg, lb = "#fffbea", "border-left:4px solid #f0b429;"
        elif is_fade:
            bg, lb = "#fff5f0", "border-left:4px solid #e36209;"
        else:
            bg, lb = ("#f5f7fa" if i % 2 else "#ffffff"), ""

        ec = _ev_color(edge)
        sc = _status_color(status)

        cells = "".join([
            _td(time_s,                                            bg=bg, extra=lb),
            _td(matchup,                                           bg=bg),
            _td(f"<strong>{pick}</strong>",                       bg=bg),
            _td(model,                         align="right",     bg=bg),
            _td(book,                          align="right",     bg=bg),
            _td(f'<b style="color:{ec}">{edge}</b>', align="right", bg=bg),
            _td(f'<span style="color:{sc}">{status}</span>', align="center", bg=bg),
            _td(flag,                          align="center",    bg=bg),
        ])
        rows_html.append(f"<tr>{cells}</tr>")

    html = f"""<div style="font-family:Arial,sans-serif;max-width:740px;margin:0 auto">
<p style="font-size:14px;color:#333;margin-bottom:10px">{summary}</p>
<table style="border-collapse:collapse;width:100%">
<thead><tr>
  <th style="{HL}">Time</th>
  <th style="{HL}">Matchup</th>
  <th style="{HL}">Pick</th>
  <th style="{HR}">Model</th>
  <th style="{HR}">Book</th>
  <th style="{HR}">Edge</th>
  <th style="{HC}">Status</th>
  <th style="{HC}"></th>
</tr></thead>
<tbody>{"".join(rows_html)}</tbody>
</table>
<p style="font-size:11px;color:#888;margin-top:8px">★ Priority &nbsp;|&nbsp; ⚠ Fade watch</p>
</div>"""

    return plain, html


def _build_results_email(picks_date):
    picks_file = os.path.join(DATA_DIR, f"picks_{picks_date}.json")
    if not os.path.exists(picks_file):
        msg = "Results and tracker processing complete. Check Google Drive for details."
        return msg, f"<p>{msg}</p>"

    try:
        sys.path.insert(0, HERE)
        from results import fetch_scores, get_margin_and_result, calc_profit

        with open(picks_file) as f:
            picks = json.load(f)

        game_pks = [g["game_pk"] for g in picks.get("games", []) if g.get("game_pk")]
        scores   = fetch_scores(game_pks)

        wins = losses = 0
        total_pl = 0.0
        bet_rows = []   # (matchup, label, odds, result, pl)

        for game in picks.get("games", []):
            pk    = game.get("game_pk")
            score = scores.get(pk)
            if not score:
                continue
            matchup = f"{_short_team(game['away_team'])} @ {_short_team(game['home_team'])}"
            for bet in game.get("bets", []):
                margin, result = get_margin_and_result(
                    bet, game["away_team"], score["away_score"], score["home_score"])
                if result in ("WIN", "LOSS"):
                    pl = calc_profit(result, bet["bet_amount"], bet["book_odds"])
                    total_pl += pl
                    wins   += result == "WIN"
                    losses += result == "LOSS"
                    label   = f"{bet['team']} {bet.get('bet_type_label', '')}".rstrip()
                    bet_rows.append((matchup, label, bet["book_odds"], result, pl))

        summary = f"{wins + losses} bet(s) | {wins}W {losses}L | ${total_pl:+.2f}"

        # ── Plain text ────────────────────────────────────────────────────────
        lines = [summary, ""]
        for matchup, label, odds, result, pl in bet_rows:
            lines.append(f"  • {label:<32}  {odds:+d}  {result}  ${pl:+.2f}")
        plain = "\n".join(lines)

        # ── HTML ──────────────────────────────────────────────────────────────
        pl_color = "#22863a" if total_pl >= 0 else "#cb2431"
        summary_html = (f"{wins + losses} bet(s) &nbsp;|&nbsp; {wins}W {losses}L &nbsp;|&nbsp; "
                        f'<b style="color:{pl_color}">${total_pl:+.2f}</b>')

        H  = "padding:8px 12px;font-size:13px;font-weight:bold;color:#fff;background:#1e3a5f;border:1px solid #1e3a5f;text-align:left;"
        HR = H + "text-align:right;" if False else H.replace("text-align:left", "text-align:right")

        def _td(val, align="left", bg="#fff"):
            s = f"padding:7px 12px;font-size:13px;border:1px solid #dde1e7;text-align:{align};background:{bg};"
            return f'<td style="{s}">{val}</td>'

        rows_html = []
        for i, (matchup, label, odds, result, pl) in enumerate(bet_rows):
            bg = "#f0fff4" if result == "WIN" else "#fff5f5"
            rc = "#22863a" if result == "WIN" else "#cb2431"
            pc = "#22863a" if pl >= 0 else "#cb2431"
            cells = "".join([
                _td(matchup, bg=bg),
                _td(f"<strong>{label}</strong>", bg=bg),
                _td(f"{odds:+d}", align="right", bg=bg),
                _td(f'<b style="color:{rc}">{result}</b>', align="center", bg=bg),
                _td(f'<b style="color:{pc}">${pl:+.2f}</b>', align="right", bg=bg),
            ])
            rows_html.append(f"<tr>{cells}</tr>")

        html = f"""<div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto">
<p style="font-size:15px;color:#333;margin-bottom:10px">{summary_html}</p>
<table style="border-collapse:collapse;width:100%">
<thead><tr>
  <th style="{H}">Matchup</th>
  <th style="{H}">Pick</th>
  <th style="{HR}">Odds</th>
  <th style="{H.replace('text-align:left','text-align:center')}">Result</th>
  <th style="{HR}">P&amp;L</th>
</tr></thead>
<tbody>{"".join(rows_html)}</tbody>
</table>
</div>"""

        return plain, html

    except Exception as e:
        msg = f"Results processed. Error building summary: {e}"
        return msg, f"<p>{msg}</p>"


# ── Drive upload ──────────────────────────────────────────────────────────────

def _upload_to_drive(local_path):
    """Update a pre-existing Google Drive file (service account as editor)."""
    if not os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON"):
        print(f"  [coord] Drive not configured — skipping: {os.path.basename(local_path)}")
        return
    if not os.path.exists(local_path):
        print(f"  [coord] Drive: file not found locally: {local_path}")
        return
    try:
        from gdrive_uploader import upload_file
        upload_file(local_path)
    except Exception as e:
        print(f"  [coord] Drive upload error ({os.path.basename(local_path)}): {e}")


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

        # Mark every game in the picks JSON as covered — run.py processes all games,
        # not just the ones that triggered this pass.
        picks_file = os.path.join(DATA_DIR, f"picks_{picks_date}.json")
        if os.path.exists(picks_file):
            with open(picks_file) as f:
                picks_json = json.load(f)
            all_pks = [g["game_pk"] for g in picks_json.get("games", [])]
            new_incomplete = [
                g["game_pk"] for g in picks_json.get("games", [])
                if g.get("away_pitcher") == "TBD" or g.get("home_pitcher") == "TBD"
                or g.get("away_lineup_count", 0) < 8 or g.get("home_lineup_count", 0) < 8
            ]
        else:
            all_pks = pks
            new_incomplete = [g["game_pk"] for g in upcoming_trigger if not _data_complete(g)]

        state["picks_runs"].append({"ran_at": now.isoformat(), "game_pks": all_pks})
        state["incomplete_at_run"] = new_incomplete
        save_state(state)

        body, html = _build_picks_email(picks_date, pass_num, len(games))
        _send_email(f"MLB Picks — {picks_date} (Pass {pass_num})", body, html)

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
    _upload_to_drive(os.path.join(HERE, tracker_file))

    state["results_done"] = True
    state["tracker_done"] = True
    save_state(state)

    body, html = _build_results_email(picks_date)
    _send_email(f"MLB Results — {picks_date}", body, html)

    _commit_state()


if __name__ == "__main__":
    main()
