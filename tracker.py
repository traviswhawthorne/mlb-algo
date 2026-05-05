#!/usr/bin/env python3
"""
MLB CUMULATIVE P&L TRACKER
===========================
Reads every picks file in data/ and builds a full-season tracker.

Run any time to refresh:
  py tracker.py

Output: MLB_Tracker.xlsx in Google Drive with 9 tabs:
  1. All Bets        — every individual bet across all days
  2. Daily           — one row per day, running cumulative P&L
  3. By Market       — moneyline vs run line vs total breakdown
  4. By Confidence   — High / Medium / Low breakdown
  5. By EV           — EV bucket breakdown
  6. By Home-Away    — away pick / home pick / total
  7. By Total Line   — o/u line bucket breakdown
  8. By Odds         — odds bracket breakdown
  9. EV × Odds Matrix — ROI heatmap: EV rows × odds columns
"""

import sys
import os
import json
import requests
import xlsxwriter
from datetime import datetime
from glob import glob

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
import config
from results import fetch_scores, get_margin_and_result, calc_profit, load_actual_bets

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
MLB_API  = "https://statsapi.mlb.com/api/v1"


# ------------------------------------------------------------------ #
# Load all picks + results
# ------------------------------------------------------------------ #

EV_BUCKETS = [
    ("5–10%",  5,  10),
    ("10–15%", 10, 15),
    ("15–20%", 15, 20),
    ("20–30%", 20, 30),
    ("30–40%", 30, 40),
    ("40%+",   40, float("inf")),
]

def _ev_float(b):
    try:
        return float(str(b.get("ev_pct", "0")).replace("%", "").replace("+", ""))
    except (ValueError, TypeError):
        return 0.0

def _ev_bucket(b):
    ev = _ev_float(b)
    for label, lo, hi in EV_BUCKETS:
        if lo <= ev < hi:
            return label
    return "40%+"


def load_all_bets(actuals=None):
    """
    Load every picks JSON file, fetch scores, and return a flat list
    of bet dicts with results attached. Sorted by date ascending.

    actuals: optional dict from load_actual_bets() — {(matchup, market, pick): entry}
    """
    files = sorted(glob(os.path.join(DATA_DIR, "picks_*.json")))
    if not files:
        raise FileNotFoundError(
            "No picks files found in data/. Run run.py on a game day first.")

    all_bets = []

    for path in files:
        with open(path) as f:
            picks = json.load(f)

        picks_date = picks["date"]
        game_pks   = [g["game_pk"] for g in picks["games"] if g.get("game_pk")]
        scores     = fetch_scores(game_pks)

        for game in picks["games"]:
            pk        = game.get("game_pk")
            away_team = game["away_team"]
            home_team = game["home_team"]
            score     = scores.get(pk)

            book_total  = game.get("book_total_line")

            for bet in game["bets"]:
                label      = bet["bet_type_label"]
                matchup    = f"{away_team}  @  {home_team}"
                pick_label = f"{bet['team']}  {label}".rstrip()

                ev_pct = bet.get("ev_pct") or (
                    f"+{bet['ev']*100:.1f}%" if bet.get("ev") else "—"
                )

                # Pull actual odds/amount/bet_placed from the Bet Tracker if available
                actual = (actuals or {}).get(
                    (matchup.strip(), bet["market"].strip(), pick_label.strip()), {}
                )
                has_actual  = bool(actual.get("odds") or actual.get("amount"))
                eff_odds    = int(float(actual["odds"]))   if actual.get("odds")   else bet["book_odds"]
                eff_amount  = float(actual["amount"])      if actual.get("amount") else bet["bet_amount"]

                # bet_placed: from actuals dict (Excel), or from JSON field, default True
                bp_excel = actual.get("bet_placed")  # "Y", "N", or absent
                bp_json  = bet.get("bet_placed")      # True/False/absent (set by sync_actuals_to_web)
                if bp_excel is not None:
                    bet_placed = bp_excel != "N"
                elif bp_json is not None:
                    bet_placed = bool(bp_json)
                else:
                    bet_placed = True  # default: assume placed (backwards compat)

                if score:
                    away_s = score["away_score"]
                    home_s = score["home_score"]
                    margin, result = get_margin_and_result(
                        bet, away_team, away_s, home_s)
                    pl     = calc_profit(result, bet["bet_amount"], bet["book_odds"])
                    eff_pl = calc_profit(result, eff_amount, eff_odds)
                else:
                    away_s = home_s = "—"
                    margin = None
                    result = "PENDING"
                    pl     = None
                    eff_pl = None

                priority  = bet.get("priority", False)
                fade_flag = bet.get("fade", False)
                if fade_flag and result in ("WIN", "LOSS"):
                    fade_result = "WIN" if result == "LOSS" else "LOSS"
                    fade_pl     = calc_profit(fade_result, eff_amount, eff_odds)
                else:
                    fade_result = None
                    fade_pl     = None

                # Home/Away classification (Total bets get "Total" side)
                mkt = bet["market"]
                if "Total" in mkt:
                    home_away = "Total"
                elif bet["team"] == away_team:
                    home_away = "Away Pick"
                else:
                    home_away = "Home Pick"

                # Total line bucket
                if book_total is None:
                    total_bucket = "Unknown"
                elif book_total <= 7.5:
                    total_bucket = "≤7.5"
                elif book_total <= 8.0:
                    total_bucket = "8"
                elif book_total <= 8.5:
                    total_bucket = "8.5"
                else:
                    total_bucket = "≥9"

                # Over / Under classification
                if bet["market"] in ("Total", "F5 Total"):
                    t = str(bet["team"])
                    if t.startswith("Over"):
                        over_under = "Over"
                    elif t.startswith("Under"):
                        over_under = "Under"
                    else:
                        over_under = "N/A"
                else:
                    over_under = "N/A"

                all_bets.append({
                    "date":         picks_date,
                    "game_time_et": game.get("game_time_et", "—"),
                    "matchup":      matchup,
                    "market":       bet["market"],
                    "pick_label":   pick_label,
                    "model_prob":   bet["model_prob"],
                    "model_odds":   bet.get("model_odds"),
                    "book_odds":    bet["book_odds"],
                    "ev_pct":       ev_pct,
                    "ev_bucket":    None,           # filled below after append
                    "confidence":   game.get("confidence", "Low"),
                    "bet_amount":   bet["bet_amount"],
                    "eff_odds":     eff_odds,
                    "eff_amount":   eff_amount,
                    "has_actual":   has_actual,
                    "bet_placed":   bet_placed,
                    "home_away":    home_away,
                    "total_bucket": total_bucket,
                    "over_under":   over_under,
                    "odds_bucket":  None,           # filled below after append
                    "proj_score":   game.get("proj_score", "—"),
                    "away_pitcher": game.get("away_pitcher", "TBD"),
                    "home_pitcher": game.get("home_pitcher", "TBD"),
                    "result":       result,
                    "away_team":    away_team,
                    "away_score":   away_s,
                    "home_team":    home_team,
                    "home_score":   home_s,
                    "margin":       margin,
                    "profit_loss":  pl,
                    "eff_pl":       eff_pl,
                    "priority":     priority,
                    "fade":         fade_flag,
                    "fade_result":  fade_result,
                    "fade_pl":      fade_pl,
                })

    # Assign EV and odds buckets after all bets are loaded
    for b in all_bets:
        b["ev_bucket"]   = _ev_bucket(b)
        b["odds_bucket"] = _odds_bucket(b["eff_odds"])

    return all_bets


ODDS_BUCKETS = [
    ("+200+",          200,   float("inf")),
    ("+150 to +199",   150,   199),
    ("+125 to +149",   125,   149),
    ("+100 to +124",   100,   124),
    ("-101 to -124",  -124,  -101),
    ("-125 to -149",  -149,  -125),
    ("-150 to -199",  -199,  -150),
    ("-200 and lower", float("-inf"), -200),
]

def _odds_bucket(odds):
    if odds is None:
        return "Unknown"
    for label, lo, hi in ODDS_BUCKETS:
        if lo <= odds <= hi:
            return label
    return "Unknown"


# ------------------------------------------------------------------ #
# Aggregation helpers
# ------------------------------------------------------------------ #

def _stats(bets):
    """Return summary stats for a list of bet dicts (uses effective odds/amount)."""
    done   = [b for b in bets if b["result"] != "PENDING"]
    wins   = sum(1 for b in done if b["result"] == "WIN")
    losses = sum(1 for b in done if b["result"] == "LOSS")
    pushes = sum(1 for b in done if b["result"] == "PUSH")
    staked = sum(b["eff_amount"] for b in done)
    pl     = sum(b["eff_pl"] for b in done if b["eff_pl"] is not None)
    total  = wins + losses + pushes
    win_pct = wins / (wins + losses) if (wins + losses) > 0 else 0.0
    roi     = pl / staked if staked > 0 else 0.0
    return {
        "bets":    total,
        "wins":    wins,
        "losses":  losses,
        "pushes":  pushes,
        "win_pct": win_pct,
        "staked":  staked,
        "pl":      pl,
        "roi":     roi,
        "pending": len(bets) - len(done),
    }


def _ev_bucket_rows(all_bets):
    """Aggregate stats by EV bucket in predefined order."""
    groups = {}
    for b in all_bets:
        groups.setdefault(b["ev_bucket"], []).append(b)
    return [(label, _stats(groups[label]))
            for label, _, _ in EV_BUCKETS if label in groups]


def _daily_rows(all_bets):
    """One row per date, with cumulative P&L column."""
    by_date = {}
    for b in all_bets:
        by_date.setdefault(b["date"], []).append(b)

    rows = []
    cum_pl = 0.0
    for d in sorted(by_date):
        s = _stats(by_date[d])
        cum_pl += s["pl"]
        rows.append({"date": d, **s, "cum_pl": round(cum_pl, 2)})
    return rows


def _group_rows(all_bets, key):
    """Aggregate stats by a given key (e.g. 'market' or 'confidence')."""
    groups = {}
    for b in all_bets:
        groups.setdefault(b[key], []).append(b)
    return {k: _stats(v) for k, v in sorted(groups.items())}


# ------------------------------------------------------------------ #
# Write Excel
# ------------------------------------------------------------------ #

def write_tracker(all_bets, output_file):
    # Metrics only count bets the user actually placed
    placed = [b for b in all_bets if b["bet_placed"]]

    wb = xlsxwriter.Workbook(output_file)

    # ---- Formats ----
    title_fmt = wb.add_format({
        "bold": True, "font_size": 15, "font_color": "#FFFFFF",
        "bg_color": "#1a3a5c", "align": "center", "valign": "vcenter"
    })
    header_fmt = wb.add_format({
        "bold": True, "font_size": 10, "font_color": "#FFFFFF",
        "bg_color": "#2c5f8a", "align": "center", "valign": "vcenter",
        "border": 1, "text_wrap": True
    })
    section_fmt = wb.add_format({
        "bold": True, "bg_color": "#1a3a5c", "font_color": "#FFFFFF",
        "border": 1, "align": "center"
    })
    green_cell  = wb.add_format({"bg_color": "#c6efce", "border": 1,
                                  "align": "center", "bold": True,
                                  "font_color": "#276221"})
    red_cell    = wb.add_format({"bg_color": "#ffc7ce", "border": 1,
                                  "align": "center", "bold": True,
                                  "font_color": "#9c0006"})
    yellow_cell = wb.add_format({"bg_color": "#ffeb9c", "border": 1,
                                  "align": "center", "font_color": "#9c5700"})
    conf_high   = wb.add_format({"bg_color": "#c6efce", "border": 1,
                                  "align": "center", "bold": True,
                                  "font_color": "#276221"})
    conf_med    = wb.add_format({"bg_color": "#ffeb9c", "border": 1,
                                  "align": "center", "font_color": "#9c5700"})
    conf_low    = wb.add_format({"bg_color": "#ffc7ce", "border": 1,
                                  "align": "center", "font_color": "#9c0006"})
    normal      = wb.add_format({"border": 1, "align": "center"})
    left        = wb.add_format({"border": 1, "align": "left"})
    bold_c      = wb.add_format({"border": 1, "align": "center", "bold": True})
    money       = wb.add_format({"border": 1, "align": "center",
                                  "num_format": "$#,##0.00"})
    money_pos   = wb.add_format({"border": 1, "align": "center",
                                  "num_format": "$#,##0.00",
                                  "font_color": "#276221", "bold": True})
    money_neg   = wb.add_format({"border": 1, "align": "center",
                                  "num_format": "$#,##0.00",
                                  "font_color": "#9c0006", "bold": True})
    pct_fmt     = wb.add_format({"border": 1, "align": "center",
                                  "num_format": "0.0%"})
    date_fmt    = wb.add_format({"border": 1, "align": "center",
                                  "num_format": "mmm dd yyyy"})
    pri_tag_fmt  = wb.add_format({"border": 1, "align": "center", "bold": True,
                                   "bg_color": "#FFD966", "font_color": "#7f5000"})
    fade_tag_fmt = wb.add_format({"border": 1, "align": "center", "bold": True,
                                   "bg_color": "#F4CCCC", "font_color": "#9c0006"})

    def _conf_fmt(t):
        return {"High": conf_high, "Medium": conf_med}.get(t, conf_low)

    def _result_fmt(r):
        return {"WIN": green_cell, "LOSS": red_cell}.get(r, yellow_cell)

    def _margin_fmt(m):
        if m is None: return normal
        return green_cell if m > 0 else (red_cell if m < 0 else yellow_cell)

    def _pl_fmt(pl):
        if pl is None: return normal
        return money_pos if pl > 0 else (money_neg if pl < 0 else money)

    def _fmt_odds(o):
        if o is None: return "—"
        return f"+{o}" if o > 0 else str(o)

    def _write_summary_row(ws, row, label, s, col_offset=0):
        """Write a stats summary row (used in Daily, By Market, By Confidence)."""
        c = col_offset
        ws.write(row, c,   label,       bold_c)
        ws.write(row, c+1, s["bets"],   normal)
        ws.write(row, c+2, s["wins"],   green_cell)
        ws.write(row, c+3, s["losses"], red_cell)
        ws.write(row, c+4, s["pushes"], yellow_cell)
        ws.write(row, c+5, s["win_pct"], pct_fmt)
        ws.write(row, c+6, s["staked"], money)
        ws.write(row, c+7, s["pl"],     _pl_fmt(s["pl"]))
        ws.write(row, c+8, s["roi"],    pct_fmt)

    # ================================================================
    # SHEET 1 — ALL BETS
    # ================================================================
    ws1 = wb.add_worksheet("All Bets")
    ws1.set_zoom(85)

    generated = datetime.now().strftime("%B %d, %Y  %I:%M %p")
    ws1.set_row(0, 28)
    ws1.merge_range("A1:T1",
                    f"MLB ALL BETS LOG  —  Generated {generated}", title_fmt)

    headers = [
        "Date", "Time (PT)", "Matchup", "Market", "Pick",
        "Bet Placed",
        "Model Prob", "Book Odds", "Odds Taken", "Edge", "Confidence",
        "Bet ($)", "Result", "Away Score", "Home Score", "Margin", "Profit / Loss",
        "Flag", "Fade Result"
    ]
    ws1.set_row(1, 30)
    for col, h in enumerate(headers):
        ws1.write(1, col, h, header_fmt)

    widths = [12, 11, 26, 11, 22, 10, 11, 11, 11, 9, 11, 12, 8, 13, 13, 9, 14, 9, 11]
    for i, w in enumerate(widths):
        ws1.set_column(i, i, w)

    odds_fmt_pos = wb.add_format({"border": 1, "align": "center",
                                   "num_format": '+#,##0;-#,##0;0'})
    odds_fmt_act = wb.add_format({"border": 1, "align": "center", "bold": True,
                                   "num_format": '+#,##0;-#,##0;0',
                                   "font_color": "#276221"})

    # Sort: date ascending, time ascending within each date
    done    = sorted([b for b in all_bets if b["result"] != "PENDING"],
                     key=lambda b: (b["date"], b["game_time_et"]))
    pending = sorted([b for b in all_bets if b["result"] == "PENDING"],
                     key=lambda b: (b["date"], b["game_time_et"]))

    skipped_fmt = wb.add_format({"border": 1, "align": "center",
                                  "font_color": "#999999", "italic": True})
    skipped_left = wb.add_format({"border": 1, "align": "left",
                                   "font_color": "#999999", "italic": True})
    placed_y_fmt = wb.add_format({"border": 1, "align": "center",
                                   "bg_color": "#c6efce", "font_color": "#276221", "bold": True})
    placed_n_fmt = wb.add_format({"border": 1, "align": "center",
                                   "bg_color": "#ffc7ce", "font_color": "#9c0006", "bold": True})

    for r, b in enumerate(done + pending, start=2):
        res = b["result"]
        m   = b["margin"]
        pl  = b["eff_pl"] if b["bet_placed"] else None
        is_placed = b["bet_placed"]

        pick_fmt  = _result_fmt(res) if (res != "PENDING" and is_placed) else (normal if is_placed else skipped_fmt)
        odds_disp = odds_fmt_act if b["has_actual"] else odds_fmt_pos
        row_norm  = normal      if is_placed else skipped_fmt
        row_left  = left        if is_placed else skipped_left
        row_money = money       if is_placed else skipped_fmt

        ws1.write(r, 0,  b["date"],                       row_norm)
        ws1.write(r, 1,  b["game_time_et"],               row_norm)
        ws1.write(r, 2,  b["matchup"],                    row_left)
        ws1.write(r, 3,  b["market"],                     row_norm)
        ws1.write(r, 4,  b["pick_label"],                 pick_fmt)
        ws1.write(r, 5,  "Y" if is_placed else "N",       placed_y_fmt if is_placed else placed_n_fmt)
        ws1.write(r, 6,  b["model_prob"],                 pct_fmt if is_placed else skipped_fmt)
        ws1.write(r, 7,  b["book_odds"],                  odds_fmt_pos if is_placed else skipped_fmt)
        ws1.write(r, 8,  b["eff_odds"],                   odds_disp    if is_placed else skipped_fmt)
        ws1.write(r, 9,  b["ev_pct"],                     row_norm)
        ws1.write(r, 10, b["confidence"],                 _conf_fmt(b["confidence"]) if is_placed else skipped_fmt)
        ws1.write(r, 11, b["eff_amount"] if is_placed else "—", row_money)
        ws1.write(r, 12, res,                             _result_fmt(res) if (res != "PENDING" and is_placed) else row_norm)
        ws1.write(r, 13, f"{b['away_team']} {b['away_score']}", row_norm)
        ws1.write(r, 14, f"{b['home_team']} {b['home_score']}", row_norm)
        ws1.write(r, 15, m  if m  is not None else "—",  _margin_fmt(m) if is_placed else skipped_fmt)
        ws1.write(r, 16, pl if pl is not None else "—",  _pl_fmt(pl)    if is_placed else skipped_fmt)

        flag_str = "★ Priority" if b.get("priority") else ("⚠ Fade" if b.get("fade") else "")
        flag_fmt = pri_tag_fmt if b.get("priority") else (fade_tag_fmt if b.get("fade") else normal)
        ws1.write(r, 17, flag_str, flag_fmt if is_placed else skipped_fmt)

        fr = b.get("fade_result")
        ws1.write(r, 18, fr if fr else "—",
                  _result_fmt(fr) if (fr and is_placed) else (normal if is_placed else skipped_fmt))

    # ================================================================
    # SHEET 2 — DAILY SUMMARY
    # ================================================================
    ws2 = wb.add_worksheet("Daily")
    ws2.set_zoom(90)

    ws2.set_row(0, 28)
    ws2.merge_range("A1:K1", "DAILY SUMMARY", title_fmt)

    daily_headers = [
        "Date", "Bets", "W", "L", "P", "Win %",
        "Staked", "P&L", "ROI", "Pending", "Cumulative P&L"
    ]
    ws2.set_row(1, 30)
    for col, h in enumerate(daily_headers):
        ws2.write(1, col, h, header_fmt)

    for i, w in enumerate([13,7,6,6,6,9,13,13,9,9,15]):
        ws2.set_column(i, i, w)

    daily_rows = _daily_rows(placed)
    for r, d in enumerate(daily_rows, start=2):
        ws2.write(r, 0,  d["date"],    normal)
        ws2.write(r, 1,  d["bets"],    normal)
        ws2.write(r, 2,  d["wins"],    green_cell)
        ws2.write(r, 3,  d["losses"],  red_cell)
        ws2.write(r, 4,  d["pushes"],  yellow_cell)
        ws2.write(r, 5,  d["win_pct"], pct_fmt)
        ws2.write(r, 6,  d["staked"],  money)
        ws2.write(r, 7,  d["pl"],      _pl_fmt(d["pl"]))
        ws2.write(r, 8,  d["roi"],     pct_fmt)
        ws2.write(r, 9,  d["pending"], normal)
        ws2.write(r, 10, d["cum_pl"],  _pl_fmt(d["cum_pl"]))

    # Totals row
    tot = _stats(placed)
    tr  = len(daily_rows) + 3
    ws2.merge_range(tr, 0, tr, 0, "TOTAL", section_fmt)
    ws2.write(tr, 1, tot["bets"],    section_fmt)
    ws2.write(tr, 2, tot["wins"],    section_fmt)
    ws2.write(tr, 3, tot["losses"],  section_fmt)
    ws2.write(tr, 4, tot["pushes"],  section_fmt)
    ws2.write(tr, 5, tot["win_pct"], wb.add_format({
        "bold": True, "bg_color": "#1a3a5c", "font_color": "#FFFFFF",
        "border": 1, "align": "center", "num_format": "0.0%"}))
    ws2.write(tr, 6, tot["staked"],  _pl_fmt(0))   # neutral money format
    ws2.write(tr, 7, tot["pl"],      _pl_fmt(tot["pl"]))
    ws2.write(tr, 8, tot["roi"],     wb.add_format({
        "bold": True, "bg_color": "#1a3a5c", "font_color": "#FFFFFF",
        "border": 1, "align": "center", "num_format": "0.0%"}))
    ws2.write(tr, 9,  tot["pending"], section_fmt)
    ws2.write(tr, 10, tot["pl"],      _pl_fmt(tot["pl"]))

    # ================================================================
    # SHEET 3 — BY MARKET
    # ================================================================
    ws3 = wb.add_worksheet("By Market")
    ws3.set_zoom(90)

    ws3.set_row(0, 28)
    ws3.merge_range("A1:I1", "PERFORMANCE BY MARKET TYPE", title_fmt)

    market_headers = ["Market", "Bets", "W", "L", "P", "Win %",
                      "Staked", "P&L", "ROI"]
    ws3.set_row(1, 30)
    for col, h in enumerate(market_headers):
        ws3.write(1, col, h, header_fmt)
    for i, w in enumerate([14,7,6,6,6,9,13,13,9]):
        ws3.set_column(i, i, w)

    by_market = _group_rows(placed, "market")
    for r, (market, s) in enumerate(by_market.items(), start=2):
        _write_summary_row(ws3, r, market, s)

    # Totals
    tr = len(by_market) + 3
    _write_summary_row(ws3, tr, "TOTAL", _stats(placed))

    # ================================================================
    # SHEET 4 — BY CONFIDENCE
    # ================================================================
    ws4 = wb.add_worksheet("By Confidence")
    ws4.set_zoom(90)

    ws4.set_row(0, 28)
    ws4.merge_range("A1:I1", "PERFORMANCE BY CONFIDENCE TIER", title_fmt)

    ws4.set_row(1, 30)
    for col, h in enumerate(market_headers):   # same headers
        ws4.write(1, col, h, header_fmt)
    for i, w in enumerate([14,7,6,6,6,9,13,13,9]):
        ws4.set_column(i, i, w)

    # Force order: High, Medium, Low
    by_conf = _group_rows(placed, "confidence")
    for r, tier in enumerate(["High", "Medium", "Low"], start=2):
        if tier in by_conf:
            _write_summary_row(ws4, r, tier, by_conf[tier])
        else:
            ws4.write(r, 0, tier, normal)
            for c in range(1, 9):
                ws4.write(r, c, "—", normal)

    tr = 6
    _write_summary_row(ws4, tr, "TOTAL", _stats(placed))

    # ================================================================
    # SHEET 5 — BY EV BUCKET
    # ================================================================
    ws5 = wb.add_worksheet("By EV")
    ws5.set_zoom(90)

    ws5.set_row(0, 28)
    ws5.merge_range("A1:I1", "PERFORMANCE BY EDGE (EV) BUCKET", title_fmt)

    ws5.set_row(1, 30)
    for col, h in enumerate(market_headers):   # same column structure
        ws5.write(1, col, h, header_fmt)
    for i, w in enumerate([14,7,6,6,6,9,13,13,9]):
        ws5.set_column(i, i, w)

    ev_rows = _ev_bucket_rows(placed)
    for r, (label, s) in enumerate(ev_rows, start=2):
        _write_summary_row(ws5, r, label, s)

    tr = len(ev_rows) + 3
    _write_summary_row(ws5, tr, "TOTAL", _stats(placed))

    # ================================================================
    # SHEET 6 — BY HOME / AWAY
    # ================================================================
    ws6 = wb.add_worksheet("By Home-Away")
    ws6.set_zoom(90)

    ws6.set_row(0, 28)
    ws6.merge_range("A1:I1", "PERFORMANCE BY HOME / AWAY PICK", title_fmt)

    ws6.set_row(1, 30)
    for col, h in enumerate(market_headers):
        ws6.write(1, col, h, header_fmt)
    for i, w in enumerate([14,7,6,6,6,9,13,13,9]):
        ws6.set_column(i, i, w)

    by_ha = _group_rows(placed, "home_away")
    for r, tier in enumerate(["Away Pick", "Home Pick", "Total"], start=2):
        if tier in by_ha:
            _write_summary_row(ws6, r, tier, by_ha[tier])
        else:
            ws6.write(r, 0, tier, normal)
            for c in range(1, 9):
                ws6.write(r, c, "—", normal)

    tr = 6
    _write_summary_row(ws6, tr, "TOTAL", _stats(placed))

    # ================================================================
    # SHEET 7 — BY TOTAL LINE
    # ================================================================
    ws7 = wb.add_worksheet("By Total Line")
    ws7.set_zoom(90)

    ws7.set_row(0, 28)
    ws7.merge_range("A1:I1", "PERFORMANCE BY BOOK TOTAL LINE", title_fmt)

    ws7.set_row(1, 30)
    for col, h in enumerate(market_headers):
        ws7.write(1, col, h, header_fmt)
    for i, w in enumerate([14,7,6,6,6,9,13,13,9]):
        ws7.set_column(i, i, w)

    by_total = _group_rows(placed, "total_bucket")
    for r, bucket in enumerate(["≤7.5", "8", "8.5", "≥9", "Unknown"], start=2):
        if bucket in by_total:
            _write_summary_row(ws7, r, bucket, by_total[bucket])
        else:
            ws7.write(r, 0, bucket, normal)
            for c in range(1, 9):
                ws7.write(r, c, "—", normal)

    tr = 8
    _write_summary_row(ws7, tr, "TOTAL", _stats(placed))

    # ---- Over vs Under section ----
    ou_start = tr + 2
    ws7.set_row(ou_start, 22)
    ws7.merge_range(ou_start, 0, ou_start, 8, "OVER vs UNDER  (Total bets only)", section_fmt)

    ws7.set_row(ou_start + 1, 28)
    for col, h in enumerate(market_headers):
        ws7.write(ou_start + 1, col, h, header_fmt)

    total_bets_placed = [b for b in placed if b["market"] in ("Total", "F5 Total")]
    by_ou = _group_rows(total_bets_placed, "over_under")
    ou_r = ou_start + 2
    for side in ("Over", "Under", "N/A"):
        if side in by_ou:
            _write_summary_row(ws7, ou_r, side, by_ou[side])
            ou_r += 1
    _write_summary_row(ws7, ou_r, "TOTAL", _stats(total_bets_placed))

    # ================================================================
    # SHEET 8 — BY ODDS BUCKET
    # ================================================================
    ws8 = wb.add_worksheet("By Odds")
    ws8.set_zoom(90)

    ws8.set_row(0, 28)
    ws8.merge_range("A1:I1", "PERFORMANCE BY ODDS BRACKET", title_fmt)

    ws8.set_row(1, 30)
    for col, h in enumerate(market_headers):
        ws8.write(1, col, h, header_fmt)
    for i, w in enumerate([16,7,6,6,6,9,13,13,9]):
        ws8.set_column(i, i, w)

    by_odds = _group_rows(placed, "odds_bucket")
    for r, (label, _lo, _hi) in enumerate(ODDS_BUCKETS, start=2):
        if label in by_odds:
            _write_summary_row(ws8, r, label, by_odds[label])
        else:
            ws8.write(r, 0, label, normal)
            for c in range(1, 9):
                ws8.write(r, c, "—", normal)

    tr = len(ODDS_BUCKETS) + 3
    _write_summary_row(ws8, tr, "TOTAL", _stats(placed))

    # ================================================================
    # SHEET 9 — EV × ODDS ROI MATRIX
    # ================================================================
    ws9 = wb.add_worksheet("EV × Odds Matrix")
    ws9.set_zoom(90)

    odds_labels = [label for label, _, _ in ODDS_BUCKETS]
    ev_labels   = [label for label, _, _ in EV_BUCKETS]

    # Build matrix: ev_bucket -> odds_bucket -> [bets]
    matrix = {ev: {od: [] for od in odds_labels} for ev in ev_labels}
    ev_totals   = {ev: [] for ev in ev_labels}
    odds_totals = {od: [] for od in odds_labels}
    for b in placed:
        ev = b["ev_bucket"]
        od = b["odds_bucket"]
        if ev in matrix and od in matrix[ev]:
            matrix[ev][od].append(b)
            ev_totals[ev].append(b)
            odds_totals[od].append(b)

    n_odds = len(odds_labels)

    # Title
    ws9.set_row(0, 28)
    ws9.merge_range(0, 0, 0, n_odds + 1,
                    "ROI MATRIX  —  EV Bucket (rows) × Odds Bracket (columns)", title_fmt)

    # Corner label
    ws9.write(1, 0, "EV \\ Odds", header_fmt)

    # Odds bucket column headers
    for c, od in enumerate(odds_labels, start=1):
        ws9.write(1, c, od, header_fmt)
    ws9.write(1, n_odds + 1, "Row Total", header_fmt)

    # Column widths
    ws9.set_column(0, 0, 14)
    for c in range(1, n_odds + 2):
        ws9.set_column(c, c, 16)

    # ROI cell formats
    roi_pos  = wb.add_format({"border": 1, "align": "center", "bold": True,
                               "num_format": "0.0%",
                               "bg_color": "#c6efce", "font_color": "#276221"})
    roi_neg  = wb.add_format({"border": 1, "align": "center", "bold": True,
                               "num_format": "0.0%",
                               "bg_color": "#ffc7ce", "font_color": "#9c0006"})
    roi_flat = wb.add_format({"border": 1, "align": "center",
                               "num_format": "0.0%"})
    empty_fmt = wb.add_format({"border": 1, "align": "center",
                                "font_color": "#aaaaaa"})

    def _roi_fmt(roi):
        return roi_pos if roi > 0.005 else (roi_neg if roi < -0.005 else roi_flat)

    def _write_roi_cell(ws, row, col, bets):
        s = _stats(bets)
        if s["bets"] == 0:
            ws.write(row, col, "—", empty_fmt)
        else:
            label = f"{s['roi']:+.1%}\n({s['wins']}W-{s['losses']}L)"
            fmt = _roi_fmt(s["roi"])
            ws.write(row, col, s["roi"], fmt)
            ws.set_row(row, 30)

    # EV rows
    for r, ev in enumerate(ev_labels, start=2):
        ws9.write(r, 0, ev, bold_c)
        for c, od in enumerate(odds_labels, start=1):
            _write_roi_cell(ws9, r, c, matrix[ev][od])
        # Row total
        _write_roi_cell(ws9, r, n_odds + 1, ev_totals[ev])

    # Column totals row
    tr = len(ev_labels) + 2
    ws9.write(tr, 0, "Col Total", bold_c)
    for c, od in enumerate(odds_labels, start=1):
        _write_roi_cell(ws9, tr, c, odds_totals[od])
    # Grand total
    _write_roi_cell(ws9, tr, n_odds + 1, placed)

    # Bet count sub-row
    tr2 = tr + 1
    ws9.write(tr2, 0, "# Bets", bold_c)
    for c, od in enumerate(odds_labels, start=1):
        s = _stats(odds_totals[od])
        ws9.write(tr2, c, s["bets"] if s["bets"] > 0 else "—", normal)
    ws9.write(tr2, n_odds + 1, _stats(placed)["bets"], bold_c)

    # ================================================================
    # SHEET 10 — BET SIZING: FLAT $20 vs FULL KELLY
    # ================================================================
    KELLY_START = 1000.0
    FLAT_AMT    = 20.0

    def _kelly_frac(model_prob, odds):
        p = model_prob
        q = 1 - p
        b = odds / 100 if odds > 0 else 100 / abs(odds)
        return max(0.0, (b * p - q) / b)

    def _bet_pl(result, stake, odds):
        if result == "WIN":
            return round(stake * (odds / 100) if odds > 0 else stake * (100 / abs(odds)), 2)
        if result == "LOSS":
            return round(-stake, 2)
        return 0.0  # PUSH

    ws10 = wb.add_worksheet("Bet Sizing")
    ws10.set_zoom(90)
    ws10.set_row(0, 28)
    ws10.merge_range("A1:M1",
                     f"BET SIZING COMPARISON  —  Flat ${FLAT_AMT:.0f} vs Full Kelly "
                     f"(starting bankroll ${KELLY_START:,.0f})", title_fmt)

    # ---- Build per-bet rows (chronological, placed + completed only) ----
    completed_placed = sorted(
        [b for b in placed if b["result"] not in ("PENDING",)],
        key=lambda b: (b["date"], b["game_time_et"])
    )

    flat_cum      = 0.0
    kelly_bank    = KELLY_START
    sizing_rows   = []
    for b in completed_placed:
        result = b["result"]
        odds   = b["eff_odds"]
        p      = b["model_prob"]

        # Flat
        fp  = _bet_pl(result, FLAT_AMT, odds)
        flat_cum += fp

        # Kelly
        kf   = _kelly_frac(p, odds)
        kbet = round(kelly_bank * kf, 2)
        kp   = _bet_pl(result, kbet, odds)
        kelly_bank += kp

        sizing_rows.append({
            "date":        b["date"],
            "matchup":     b["matchup"],
            "pick":        b["pick_label"],
            "result":      result,
            "model_prob":  p,
            "odds":        odds,
            "kelly_frac":  kf,
            "flat_bet":    FLAT_AMT,
            "flat_pl":     fp,
            "flat_cum":    round(flat_cum, 2),
            "kelly_bet":   kbet,
            "kelly_pl":    kp,
            "kelly_bank":  round(kelly_bank, 2),
        })

    n = len(sizing_rows)
    flat_staked   = FLAT_AMT * n
    flat_pl_total = flat_cum
    flat_roi      = flat_pl_total / flat_staked if flat_staked > 0 else 0.0
    kelly_staked  = sum(r["kelly_bet"] for r in sizing_rows)
    kelly_pl_total = kelly_bank - KELLY_START
    kelly_roi     = kelly_pl_total / kelly_staked if kelly_staked > 0 else 0.0

    # ---- Summary comparison box (rows 2–7) ----
    summ_hdr = wb.add_format({
        "bold": True, "bg_color": "#2c5f8a", "font_color": "#FFFFFF",
        "border": 1, "align": "center", "valign": "vcenter"
    })
    summ_lbl = wb.add_format({
        "bold": True, "bg_color": "#dce6f1", "font_color": "#1a3a5c",
        "border": 1, "align": "left", "valign": "vcenter"
    })
    pct_pos = wb.add_format({"border": 1, "align": "center", "bold": True,
                              "num_format": "0.0%",
                              "bg_color": "#c6efce", "font_color": "#276221"})
    pct_neg = wb.add_format({"border": 1, "align": "center", "bold": True,
                              "num_format": "0.0%",
                              "bg_color": "#ffc7ce", "font_color": "#9c0006"})

    ws10.merge_range(1, 0, 1, 3, "",          summ_hdr)
    ws10.merge_range(1, 4, 1, 6, f"Flat ${FLAT_AMT:.0f} / bet", summ_hdr)
    ws10.merge_range(1, 7, 1, 9, f"Full Kelly  (start ${KELLY_START:,.0f})", summ_hdr)

    summ_data = [
        ("Bets Resolved",   n,               n),
        ("Total Staked",    flat_staked,     kelly_staked),
        ("Total P&L",       flat_pl_total,   kelly_pl_total),
        ("ROI",             flat_roi,        kelly_roi),
        ("Final Bankroll",  KELLY_START + flat_pl_total, kelly_bank),
    ]
    for i, (lbl, fv, kv) in enumerate(summ_data, start=2):
        ws10.merge_range(i, 0, i, 3, lbl, summ_lbl)
        if lbl == "ROI":
            ws10.merge_range(i, 4, i, 6, fv, pct_pos if fv >= 0 else pct_neg)
            ws10.merge_range(i, 7, i, 9, kv, pct_pos if kv >= 0 else pct_neg)
        elif lbl == "Bets Resolved":
            ws10.merge_range(i, 4, i, 6, fv, normal)
            ws10.merge_range(i, 7, i, 9, kv, normal)
        else:
            ws10.merge_range(i, 4, i, 6, fv, _pl_fmt(fv))
            ws10.merge_range(i, 7, i, 9, kv, _pl_fmt(kv))

    # ---- Per-bet table (starting row 8) ----
    tbl_start = 8
    bet_hdrs = [
        "Date", "Matchup", "Pick", "Result",
        "Model Prob", "Odds", "Kelly %",
        "Flat Bet", "Flat P&L", "Flat Cumul.",
        "Kelly Bet", "Kelly P&L", "Kelly Bankroll",
    ]
    ws10.set_row(tbl_start - 1, 30)
    for col, h in enumerate(bet_hdrs):
        ws10.write(tbl_start - 1, col, h, header_fmt)

    col_widths = [12, 28, 22, 8, 11, 9, 9, 10, 11, 12, 11, 11, 15]
    for i, w in enumerate(col_widths):
        ws10.set_column(i, i, w)

    kpct_fmt = wb.add_format({"border": 1, "align": "center", "num_format": "0.0%"})
    kbank_fmt = wb.add_format({"border": 1, "align": "center",
                                "num_format": "$#,##0.00", "bold": True})

    for r, row in enumerate(sizing_rows, start=tbl_start):
        ws10.write(r, 0,  row["date"],       normal)
        ws10.write(r, 1,  row["matchup"],    left)
        ws10.write(r, 2,  row["pick"],       _result_fmt(row["result"]))
        ws10.write(r, 3,  row["result"],     _result_fmt(row["result"]))
        ws10.write(r, 4,  row["model_prob"], pct_fmt)
        ws10.write(r, 5,  _fmt_odds(row["odds"]), normal)
        ws10.write(r, 6,  row["kelly_frac"], kpct_fmt)
        ws10.write(r, 7,  row["flat_bet"],   money)
        ws10.write(r, 8,  row["flat_pl"],    _pl_fmt(row["flat_pl"]))
        ws10.write(r, 9,  row["flat_cum"],   _pl_fmt(row["flat_cum"]))
        ws10.write(r, 10, row["kelly_bet"],  money)
        ws10.write(r, 11, row["kelly_pl"],   _pl_fmt(row["kelly_pl"]))
        ws10.write(r, 12, row["kelly_bank"], kbank_fmt)

    # ================================================================
    # SHEET 11 — BY TEAM
    # ================================================================
    from collections import defaultdict

    # Accumulate per-team records from placed + completed bets only
    _for     = defaultdict(lambda: {"w":0,"l":0,"p":0,"staked":0.0,"pl":0.0})
    _against = defaultdict(lambda: {"w":0,"l":0,"p":0,"staked":0.0,"pl":0.0})
    _totals  = defaultdict(lambda: {"w":0,"l":0,"p":0})

    for b in placed:
        if b["result"] not in ("WIN","LOSS","PUSH"):
            continue
        res    = b["result"]
        market = b["market"]
        amt    = b["eff_amount"]
        odds   = b["eff_odds"]
        pl     = (amt*(odds/100) if odds>0 else amt*(100/abs(odds))) if res=="WIN" \
                 else (-amt if res=="LOSS" else 0.0)

        if market in ("Moneyline","Run Line"):
            if b["home_away"] == "Away Pick":
                bet_team = b["away_team"]
                opponent = b["home_team"]
            else:
                bet_team = b["home_team"]
                opponent = b["away_team"]

            d = _for[bet_team]
            d["w"] += res=="WIN"; d["l"] += res=="LOSS"; d["p"] += res=="PUSH"
            d["staked"] += amt;   d["pl"] += pl

            d = _against[opponent]
            d["w"] += res=="WIN"; d["l"] += res=="LOSS"; d["p"] += res=="PUSH"
            d["staked"] += amt;   d["pl"] += pl

        elif market == "Total":
            for tn in (b["away_team"], b["home_team"]):
                d = _totals[tn]
                d["w"] += res=="WIN"; d["l"] += res=="LOSS"; d["p"] += res=="PUSH"

    # Gather all teams seen
    all_teams = sorted(set(list(_for) + list(_against) + list(_totals)))

    def _wl(d):
        return d["w"] + d["l"] + d["p"]

    def _winpct(d):
        tot = d["w"] + d["l"]
        return d["w"] / tot if tot > 0 else None

    def _roi(d):
        return d["pl"] / d["staked"] if d["staked"] > 0 else None

    # Sort: most total bets first
    all_teams.sort(key=lambda t: (
        _wl(_for[t]) + _wl(_against[t]) + _wl(_totals[t])
    ), reverse=True)

    ws11 = wb.add_worksheet("By Team")
    ws11.set_zoom(90)
    ws11.set_row(0, 28)
    ws11.merge_range("A1:Q1", "PERFORMANCE BY TEAM", title_fmt)

    # ---- Group header row ----
    grp_fmt = wb.add_format({
        "bold": True, "bg_color": "#1a3a5c", "font_color": "#FFFFFF",
        "border": 1, "align": "center", "valign": "vcenter"
    })
    grp_for     = wb.add_format({"bold":True,"bg_color":"#1e5c1e","font_color":"#FFFFFF","border":1,"align":"center"})
    grp_against = wb.add_format({"bold":True,"bg_color":"#5c1e1e","font_color":"#FFFFFF","border":1,"align":"center"})
    grp_totals  = wb.add_format({"bold":True,"bg_color":"#1a3a5c","font_color":"#FFFFFF","border":1,"align":"center"})

    ws11.set_row(1, 22)
    ws11.write(1, 0, "", grp_fmt)
    ws11.merge_range(1, 1, 1, 5,  "BET FOR  (ML + Run Line on this team)", grp_for)
    ws11.merge_range(1, 6, 1, 10, "BET AGAINST  (ML + Run Line vs this team)", grp_against)
    ws11.merge_range(1, 11, 1, 14, "TOTALS  (Over/Under on games with this team)", grp_totals)

    # ---- Column headers ----
    col_hdrs = [
        "Team",
        "W","L","P","Win%","ROI",          # For
        "W","L","P","Win%","ROI",          # Against
        "W","L","P","Win%",                # Totals
    ]
    ws11.set_row(2, 28)
    for c, h in enumerate(col_hdrs):
        ws11.write(2, c, h, header_fmt)

    col_w = [22, 5,5,5,8,9,  5,5,5,8,9,  5,5,5,8]
    for i, w in enumerate(col_w):
        ws11.set_column(i, i, w)

    pct_c   = wb.add_format({"border":1,"align":"center","num_format":"0%"})
    pct_pos2 = wb.add_format({"border":1,"align":"center","num_format":"0.0%",
                               "font_color":"#276221","bold":True})
    pct_neg2 = wb.add_format({"border":1,"align":"center","num_format":"0.0%",
                               "font_color":"#9c0006","bold":True})
    dash_fmt = wb.add_format({"border":1,"align":"center","font_color":"#aaaaaa"})

    def _write_pct(ws, r, c, val):
        if val is None:
            ws.write(r, c, "—", dash_fmt)
        else:
            ws.write(r, c, val, pct_c)

    def _write_roi(ws, r, c, val):
        if val is None:
            ws.write(r, c, "—", dash_fmt)
        else:
            ws.write(r, c, val, pct_pos2 if val >= 0 else pct_neg2)

    for row_i, team in enumerate(all_teams, start=3):
        f  = _for[team]
        a  = _against[team]
        t  = _totals[team]

        ws11.write(row_i, 0, team, left)

        ws11.write(row_i, 1, f["w"] or "—" if _wl(f) else "—", green_cell if f["w"] else (normal if _wl(f) else dash_fmt))
        ws11.write(row_i, 2, f["l"] or "—" if _wl(f) else "—", red_cell  if f["l"] else (normal if _wl(f) else dash_fmt))
        ws11.write(row_i, 3, f["p"] or "—" if _wl(f) else "—", yellow_cell if f["p"] else (normal if _wl(f) else dash_fmt))
        _write_pct(ws11, row_i, 4, _winpct(f))
        _write_roi(ws11, row_i, 5, _roi(f))

        ws11.write(row_i, 6,  a["w"] or "—" if _wl(a) else "—", green_cell if a["w"] else (normal if _wl(a) else dash_fmt))
        ws11.write(row_i, 7,  a["l"] or "—" if _wl(a) else "—", red_cell  if a["l"] else (normal if _wl(a) else dash_fmt))
        ws11.write(row_i, 8,  a["p"] or "—" if _wl(a) else "—", yellow_cell if a["p"] else (normal if _wl(a) else dash_fmt))
        _write_pct(ws11, row_i, 9,  _winpct(a))
        _write_roi(ws11, row_i, 10, _roi(a))

        ws11.write(row_i, 11, t["w"] or "—" if _wl(t) else "—", green_cell if t["w"] else (normal if _wl(t) else dash_fmt))
        ws11.write(row_i, 12, t["l"] or "—" if _wl(t) else "—", red_cell  if t["l"] else (normal if _wl(t) else dash_fmt))
        ws11.write(row_i, 13, t["p"] or "—" if _wl(t) else "—", yellow_cell if t["p"] else (normal if _wl(t) else dash_fmt))
        _write_pct(ws11, row_i, 14, _winpct(t))

    # ================================================================
    # SHEET 12 — MODEL CHANGELOG
    # ================================================================
    ws12 = wb.add_worksheet("Model Changelog")
    ws12.set_zoom(90)
    ws12.set_row(0, 28)
    ws12.merge_range("A1:D1", "MODEL CHANGELOG  —  Significant changes affecting results interpretation", title_fmt)

    cl_hdr = wb.add_format({
        "bold": True, "font_size": 10, "font_color": "#FFFFFF",
        "bg_color": "#2c5f8a", "align": "center", "valign": "vcenter",
        "border": 1, "text_wrap": True
    })
    cl_date  = wb.add_format({"border": 1, "align": "center", "bold": True, "bg_color": "#dce6f1"})
    cl_left  = wb.add_format({"border": 1, "align": "left", "text_wrap": True})
    cl_left_bold = wb.add_format({"border": 1, "align": "left", "bold": True, "text_wrap": True})
    cl_impact_pos = wb.add_format({"border": 1, "align": "left", "text_wrap": True,
                                    "bg_color": "#c6efce", "font_color": "#276221"})
    cl_impact_neu = wb.add_format({"border": 1, "align": "left", "text_wrap": True,
                                    "bg_color": "#ffeb9c", "font_color": "#7f5000"})

    ws12.set_row(1, 22)
    for c, h in enumerate(["Effective Date", "Change", "Detail", "Impact on Results"]):
        ws12.write(1, c, h, cl_hdr)
    ws12.set_column(0, 0, 14)
    ws12.set_column(1, 1, 28)
    ws12.set_column(2, 2, 52)
    ws12.set_column(3, 3, 36)

    changelog = [
        (
            "2026-04-26",
            "Negative Binomial distribution",
            "Replaced Poisson with Negative Binomial (r=3.14, fitted from 4,856 team-games). "
            "Poisson assumes variance = mean (~4.4 runs); actual MLB variance ~10.5. "
            "NB matches shutouts and blowouts far better (0-run game: NB 6.26% vs Poisson 1.17%, actual 6.82%).",
            "Improves over/under calibration. "
            "Bets before this date used Poisson — overs were systematically undervalued."
        ),
        (
            "2026-04-26",
            "Run calibration offset (+0.13/team)",
            "Model consistently under-projected game totals by 0.13–0.40 runs/game "
            "(confirmed across 2024 and 2025 backtests; ~53% of games went over model projection). "
            "Fixed by adding +0.13 runs per team to expected runs before win probability is computed.",
            "Fewer phantom under bets. "
            "Avg projection error: 2025 improved from -0.40 to -0.14 runs; "
            "2024 from -0.13 to +0.13 runs."
        ),
    ]

    for r, (dt, change, detail, impact) in enumerate(changelog, start=2):
        ws12.set_row(r, 60)
        ws12.write(r, 0, dt,     cl_date)
        ws12.write(r, 1, change, cl_left_bold)
        ws12.write(r, 2, detail, cl_left)
        ws12.write(r, 3, impact, cl_impact_pos)

    # ================================================================
    # SHEET 13 — PRIORITY / FADE PERFORMANCE
    # ================================================================
    ws13 = wb.add_worksheet("Priority & Fade")
    ws13.set_zoom(90)
    ws13.set_row(0, 28)
    ws13.merge_range("A1:I1", "PRIORITY & FADE PERFORMANCE", title_fmt)

    pf_headers = ["Group", "Bets", "W", "L", "P", "Win %", "Staked", "P&L", "ROI"]
    ws13.set_row(1, 30)
    for col, h in enumerate(pf_headers):
        ws13.write(1, col, h, header_fmt)
    for i, w in enumerate([22, 7, 6, 6, 6, 9, 13, 13, 9]):
        ws13.set_column(i, i, w)

    priority_bets = [b for b in placed if b.get("priority")]
    fade_bets     = [b for b in placed if b.get("fade")]

    # Fade stats graded as the model's pick (model direction)
    def _fade_stats_model(bets):
        done   = [b for b in bets if b["result"] != "PENDING"]
        wins   = sum(1 for b in done if b["result"] == "WIN")
        losses = sum(1 for b in done if b["result"] == "LOSS")
        pushes = sum(1 for b in done if b["result"] == "PUSH")
        staked = sum(b["eff_amount"] for b in done)
        pl     = sum(b["eff_pl"] for b in done if b["eff_pl"] is not None)
        win_pct = wins / (wins + losses) if (wins + losses) > 0 else 0.0
        roi     = pl / staked if staked > 0 else 0.0
        return {"bets": len(done), "wins": wins, "losses": losses, "pushes": pushes,
                "win_pct": win_pct, "staked": staked, "pl": pl, "roi": roi}

    # Fade stats graded as the opposite bet (fade direction — what you'd actually bet)
    def _fade_stats_opposite(bets):
        done   = [b for b in bets if b.get("fade_result") is not None]
        wins   = sum(1 for b in done if b["fade_result"] == "WIN")
        losses = sum(1 for b in done if b["fade_result"] == "LOSS")
        pushes = sum(1 for b in done if b["fade_result"] == "PUSH")
        staked = sum(b["eff_amount"] for b in done)
        pl     = sum(b["fade_pl"] for b in done if b["fade_pl"] is not None)
        win_pct = wins / (wins + losses) if (wins + losses) > 0 else 0.0
        roi     = pl / staked if staked > 0 else 0.0
        return {"bets": len(done), "wins": wins, "losses": losses, "pushes": pushes,
                "win_pct": win_pct, "staked": staked, "pl": pl, "roi": roi}

    section_gold = wb.add_format({
        "bold": True, "bg_color": "#FFD966", "font_color": "#7f5000",
        "border": 1, "align": "center", "font_size": 11
    })
    section_red = wb.add_format({
        "bold": True, "bg_color": "#F4CCCC", "font_color": "#9c0006",
        "border": 1, "align": "center", "font_size": 11
    })

    rows_data = [
        ("★ Priority (model pick)",    priority_bets, _fade_stats_model,    section_gold),
        ("All Bets",                   placed,        _stats,               section_fmt),
        ("⚠ Fade — model direction",   fade_bets,     _fade_stats_model,    fade_tag_fmt),
        ("⚠ Fade — opposite (bet Under)", fade_bets,  _fade_stats_opposite, section_gold),
    ]

    for row_i, (label, bets, stat_fn, lbl_fmt) in enumerate(rows_data, start=2):
        s = stat_fn(bets)
        ws13.write(row_i, 0, label,       lbl_fmt)
        ws13.write(row_i, 1, s["bets"],   normal)
        ws13.write(row_i, 2, s["wins"],   green_cell)
        ws13.write(row_i, 3, s["losses"], red_cell)
        ws13.write(row_i, 4, s["pushes"], yellow_cell)
        ws13.write(row_i, 5, s["win_pct"], pct_fmt)
        ws13.write(row_i, 6, s["staked"], money)
        ws13.write(row_i, 7, s["pl"],     _pl_fmt(s["pl"]))
        ws13.write(row_i, 8, s["roi"],    pct_fmt)

    ws13.write(7, 0,
        "Note: 'Fade — opposite' grades each Fade-flagged Over bet as if you bet the Under instead.",
        wb.add_format({"italic": True, "font_color": "#666666", "align": "left"}))

    wb.close()
    print(f"  Saved: {output_file}")
    return output_file


# ------------------------------------------------------------------ #
# Main
# ------------------------------------------------------------------ #

def main():
    print()
    print("=" * 62)
    print("  MLB CUMULATIVE P&L TRACKER")
    print("=" * 62)

    try:
        print("\n  Loading all picks and fetching scores ...")
        actuals = load_actual_bets(config.OUTPUT_FILE)
        if actuals:
            print(f"  Actual bet overrides loaded: {len(actuals)} row(s)")
        all_bets = load_all_bets(actuals=actuals)
    except FileNotFoundError as e:
        print(f"\n  ERROR: {e}")
        if not os.environ.get("CI"):
            input("\nPress Enter to close...")
        return

    placed  = [b for b in all_bets if b["bet_placed"]]
    skipped = [b for b in all_bets if not b["bet_placed"]]
    done    = [b for b in placed if b["result"] != "PENDING"]
    pending = [b for b in placed if b["result"] == "PENDING"]
    s       = _stats(placed)

    print(f"\n  Total bets:  {s['bets']}  ({s['pending']} pending)"
          + (f"  |  {len(skipped)} skipped" if skipped else ""))
    print(f"  Record:      {s['wins']}W - {s['losses']}L - {s['pushes']}P  "
          f"({s['win_pct']:.1%})")
    print(f"  P&L:         ${s['pl']:+.2f}")
    print(f"  ROI:         {s['roi']:+.1%}")

    print()
    by_market = _group_rows(done, "market")
    print("  By market:")
    for market, ms in by_market.items():
        print(f"    {market:<12}  {ms['wins']}W-{ms['losses']}L  "
              f"ROI: {ms['roi']:+.1%}  P&L: ${ms['pl']:+.2f}")

    print()
    out_file = config.OUTPUT_FILE.replace("MLB_Picks.xlsx", "MLB_Tracker.xlsx")
    print("  Writing tracker ...")
    write_tracker(all_bets, out_file)

    print()
    print("=" * 62)
    print("  DONE — tracker saved to Google Drive")
    print("=" * 62)
    print()

    if not os.environ.get("CI"):
        input("Press Enter to close...")


if __name__ == "__main__":
    main()
