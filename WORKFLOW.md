# MLB Betting Algorithm — How It Works

## Overview

Every morning, the algorithm runs through four steps:

1. **Pull stats** from the MLB Stats API (pitchers, team batting, bullpens, lineups, umpires, defense)
2. **Pull today's schedule** from the MLB API (who's playing, who's pitching, what time, where)
3. **Pull odds** from The Odds API (moneylines, run lines, totals, first-5 lines, strikeout props)
4. **Run the model** — for each game, project a score, convert to win probabilities, compare to the book, and flag positive-EV bets

Output goes to `MLB_Picks.xlsx` (Google Drive), a timestamped log file in `/logs`, a JSON file in `/data`, and automatically pushes to **traviswhawthorne.net/mlb**.

---

## Daily Workflow

### Running the model
Double-click `run.py` or type `py run.py` in the terminal.

**Multi-run day support**: You can run it multiple times in one day. Games already underway are not re-analyzed — their morning picks are preserved. New Preview games are added. The merge is logged: `Merging: X carried from earlier run + Y from this run`.

**Automated daily run**: `auto_run.py` is scheduled via Windows Task Scheduler to fire at 8am. It fetches the day's first game time from the MLB API, waits until 1 hour before that game, then runs picks automatically. Wake timers are enabled so this fires even if the PC is sleeping.

### After placing bets
1. Open `MLB_Picks.xlsx` → Bet Tracker tab
2. Fill in **Actual Odds Taken** (col F) and **Actual Bet ($)** (col H) for each bet you placed
3. Double-click `sync_actuals_to_web.bat` — reads your entries from Excel and pushes them to the website

**Actuals are preserved on re-run**: If you re-run the model mid-day, any values you already filled into the Bet Tracker are read first and carried forward into the new Excel. You won't lose what you entered.

### Checking results
Run `py results.py` the morning after. It fetches final scores, grades each bet, and outputs `MLB_Results_YYYY-MM-DD.xlsx`. If you filled in actual odds/amounts in the Bet Tracker, results uses those for P&L calculation instead of model values.

---

## The Website (traviswhawthorne.net/mlb)

A live picks page that updates automatically after each run. Shows:
- Recommended bets with bet size, odds, and edge %
- Live scores polled from the MLB Stats API every 60 seconds
- **Live / Upcoming / Completed** tabs — automatically switches to Live when games are in progress
- Win/Loss/Push status on each card as scores come in
- P&L summary bar (wins, losses, dollar profit/loss) for completed games
- Each card links to the MLB Gameday box score
- "Bet Tracker ↗" button links to the Google Sheet

The model auto-pushes to the website at the end of each run via `push_picks_to_web.py`, which copies the picks JSON and does a git commit/push to the resume-chat repo triggering a Vercel deploy.

If you filled in actual bets in the Tracker and want them on the website, run `sync_actuals_to_web.bat`.

---

## The Full Model, Step by Step

### Step 1 — Data Collection

**Pitcher stats** (MLB Stats API, current season + prior season):
- ERA, FIP (Fielding Independent Pitching), innings pitched, strikeout rate, games started
- Home/away ERA splits
- vs-Left / vs-Right ERA splits
- Last-30-day ERA (recent form)

**Team batting stats** (MLB Stats API, current + prior season):
- wRC+ (Weighted Runs Created Plus) — 100 = league average, 110 = 10% above average
- wRC+ splits vs. right-handed and left-handed pitchers
- Plate appearances (controls how much to trust current-season data)
- Team strikeout rate (used for strikeout prop projections)
- Recent 30-day offensive form

**Bullpen stats**: Average ERA for each team's bullpen, split by batter handedness

**Individual batter stats**: Per-player wRC+ and plate appearances computed from counting stats (H, 2B, 3B, HR, BB, IBB, HBP, SF, AB) using the same FanGraphs linear weights as the team model. Used to adjust team wRC+ when a confirmed lineup is available. Returns `{player_id: {"wrc": wrc_plus, "pa": plate_appearances}}`.

**Defensive stats**: Unearned runs allowed per game

**Umpire tendencies**: Historical run-per-game rates by home plate umpire

**Rest days**: Days since each team's last game

---

### Step 2 — ERA Estimation

#### Layer 1: FIP-to-ERA Regression
Raw ERA is luck-influenced. FIP (strikeouts, walks, home runs only) strips that out. The model starts with a FIP-based ERA estimate.

#### Layer 2: Blending Current Year with Prior Year

| Innings Pitched | Trust Current Year | Trust Prior Year |
|---|---|---|
| 0 IP | 0% | 100% |
| 12 IP | 30% | 70% |
| 24 IP | 60% | 40% |
| 48 IP | 80% | 20% |
| 96 IP | 100% | 0% |

The prior year uses that specific pitcher's own FIP — not a league average. A pitcher who posted a 2.80 FIP last year regresses toward 2.80.

**0-IP fallback**: If a pitcher has no current-season data (IL returnee, hasn't started yet), the model looks up their prior-year ERA_est directly rather than falling back to a generic 4.25 league average. Home/away split adjustment is also skipped for these pitchers — layering a regression-heavy prior-year H/A split on top of a prior-year overall ERA produces artifacts.

#### Layer 3: Home/Away Splits
Adjusted based on whether the pitcher is home or away today. Also blended current/prior by IP.

#### Layer 4: Platoon Splits (vs. Left / vs. Right)
ERA adjusted based on the opposing lineup's handedness composition.

#### Layer 5: Recent Form
Last 30 days blended in at 40% weight — **only once the pitcher reaches 96 IP**. Before that, recent ERA is just a slice of the same small sample already in the IP blend.

#### Layer 6: Deviation Guard
If the pitcher's actual ERA diverges significantly from the blended estimate, the model pulls toward actual:
- Outperforming by 15%+: pulls estimate down
- Underperforming by 25%+: pulls estimate up
- Asymmetric because ERA approaches zero as a floor.

---

### Step 3 — Offensive Strength (wRC+)

Team wRC+ is blended current/prior year using a PA-based curve (same shape as pitcher IP curve, scaled by PA ÷ 20). At ~450 PA (15 games), the blend is roughly 55% current / 45% prior.

Then adjusted in order:

1. **Platoon split**: Team's wRC+ vs. the opposing pitcher's hand (vs-R or vs-L)
2. **Lineup adjustment** (if confirmed lineup available): Per-player wRC+ values for the 9 starters are averaged and blended 60% team / 40% lineup average, capped at ±15 points. Requires 6+ players with known stats. Falls back to team wRC+ if lineup is unavailable. Each batter's contribution is weighted by a PA-based trust curve (same shape as the pitcher IP curve, scaled at PA ÷ 3) — a batter with 30 PA contributes much less than one with 200 PA. Fully trusted at ~288 PA.
3. **Recent form**: Last 30 days blended at 40% — only applied once the team hits ~1,920 PA (~96 games, second half of season)

---

### Step 4 — Run Environment Adjustments

**Park Factor**: FanGraphs 5-year regressed park factors. Applied to visiting offense only — home team's stats were already collected at their home park.

**Alternate Venue Override**: For international series or neutral-site games, the home team's park factor is replaced with a venue-specific override. Example: Estadio Alfredo Harp Helú (Mexico City) sits at ~7,350 ft elevation — higher than Coors Field — and uses a 1.20 run factor. Keyed by venue name in `ALTERNATE_VENUE_FACTORS` in `model.py`.

**Home/Away Team Factor**: Each team's historical home vs. road run-scoring tendency.

**Weather**: Temperature and wind for outdoor parks. Cold suppresses scoring; wind blowing out boosts it.

**Umpire**: Home plate umpire's historical run-per-game tendency, capped at ±10%.

**Rest Days**: Back-to-back = 2% run penalty on that team's offense.

**Defense**: Teams with high unearned runs per game give opponents more expected runs.

---

### Step 5 — Bullpen Blending

Starter innings estimated from ERA (worse pitchers get pulled earlier). Remaining innings covered by bullpen ERA. First 5 Innings bets exclude bullpen entirely.

**Quality-aware fatigue penalty**: When key relievers are unavailable (pitched 45+ pitches yesterday), the ERA penalty is scaled by how good that arm is relative to the team's bullpen average. Losing a closer (low ERA) hurts more than losing a mop-up arm (high ERA). Multiplier capped at 0.3×–2.0× base penalty; total adjustment capped at +0.45 ERA.

**Fresh bonus**: A well-rested bullpen (2+ days rest or ≤15 total pitches thrown yesterday) gets a -0.10 or -0.05 ERA adjustment. Based on 2025 data showing fresh bullpen ERA of 4.11 vs. 4.27 average.

---

### Step 6 — Run Projection

```
expected_runs = LEAGUE_AVG_RUNS × (wRC+ / 100) × (pitcher_ERA / league_avg_ERA)
                × park_factor × venue_factor × weather_factor × rest_factor
                × defense_factor
```

League average: 4.43 runs/team/game. Capped at [1.5, 10.0].

---

### Step 7 — Win Probability (Negative Binomial Model)

Run totals modeled as a **Negative Binomial distribution** (r = 3.14, fitted via MLE from 4,856 team-games in 2025). Poisson assumes variance = mean (~4.4 runs); actual MLB variance is ~10.5 — teams are "overdispersed," meaning blowouts and shutouts happen more than Poisson predicts. NB matches this: 0-run games 6.26% vs. actual 6.82% (Poisson only 1.17%).

All score combinations calculated via vectorized numpy outer product; win probability = sum of combinations where one team wins. Calibrated against 799-game backtests to correct for overconfidence at extreme probabilities.

**Run calibration offset**: The model systematically projects 0.13–0.20 fewer runs per team than actually score (confirmed across both 2024 and 2025 backtests with ~53% of games going over the model's projection). A fixed offset of +0.13 runs per team is applied to expected runs before win probability is computed. This reduces phantom under-value and brings average projection error close to zero.

---

### Step 8 — Bet Markets

| Market | Signal |
|---|---|
| Moneyline | Win probability |
| Run Line (-1.5 / +1.5) | Probability of winning by 2+ runs |
| Total (Over/Under) | Sum of expected runs vs. book line |
| First 5 Innings | Starter-only ERA, no bullpen |
| Strikeout Props | Pitcher K% × opposing team K rate × batters faced |

**Deduplication**: If the model likes both ML and Run Line for the same team, only the higher-EV bet is kept.

---

### Step 9 — Expected Value and Bet Sizing

```
EV = (win_probability × net_profit_if_win) − (loss_probability × $1 risked)
```

Minimum EV threshold: **5%** (set in `config.py` as `MIN_EV_THRESHOLD = 0.05`).

**Bet sizing**: Kelly criterion normalized to a $10–$55 range:
- Raw Kelly fraction computed, capped at 20% of bankroll
- Normalized so max Kelly = $55, min = $10
- Rounded down to nearest $5
- Higher conviction bets get larger sizes; weak edges get $10

---

### Step 10 — Output

**Excel** (`MLB_Picks.xlsx` on Google Drive):
- Sheet 1: Today's Picks — recommended bets + all games
- Sheet 2: Game Details — full model breakdown per game
- Sheet 3: Bet Tracker — pre-filled table; fill in Actual Odds and Actual Bet columns, and Result/P&L after games end

**Tracker** (`MLB_Tracker.xlsx`, run `py tracker.py`):
- 11 tabs: All Bets, Daily, By Market, By Confidence, By EV, By Home-Away, By Total Line, By Odds, EV × Odds Matrix, Bet Sizing, By Team
- "By Total Line" tab includes an **Over vs Under breakdown** section showing W/L/P, Win%, and ROI separately for over bets and under bets

**JSON** (`/data/picks_YYYY-MM-DD.json`): Machine-readable picks used by the website and results checker.

**Website** (traviswhawthorne.net/mlb): Auto-updated after each run. See website section above.

**Console + log**: All model output printed to screen and saved to `/logs`.

---

## Key Design Decisions

**Why blend current and prior year?** Early in the season (April, May), pitchers may have 15–20 innings. One bad start doubles their ERA. Blending with their established prior-year profile stabilizes estimates until the current-season sample is large enough to stand alone.

**Why apply park factor to away team only?** The home team's run-scoring data was collected at their home park — park effect is already embedded. Applying it again would double-count it.

**Why Negative Binomial instead of Poisson?** Poisson assumes variance equals mean (~4.4 runs). Real MLB run distributions have variance ~10.5 — teams score 0 runs or 10+ runs far more often than Poisson predicts. NB with r=3.14 matches the actual distribution much more closely, especially at the tails. This was a systematic source of miscalibration on over/under bets.

**Why calibrate?** The raw model is directionally correct but mathematically overconfident. Calibration maps raw probabilities to observed outcomes from 799-game backtests across 2024 and 2025.

**Why fractional Kelly?** Full Kelly is optimal for long-run growth but creates large bankroll swings. Quarter Kelly preserves most of the edge with much lower variance.

**Why per-player wRC+ instead of OPS for lineup adjustment?** OPS was a proxy — it required a rough conversion to put it on the same scale as wRC+. Per-player wRC+ is computed with the exact same linear weights and league averages as the team model, so the lineup adjustment is now internally consistent. Confirmed lineups now meaningfully move the wRC+ when stars are sitting or a weak lineup is fielded.

**Known limitation — early-season ERA estimates**: The book prices pitcher ERA aggressively based on current performance. Our blend still carries prior-year weight, so ERA estimates can run slightly high (worse pitching = more runs) compared to what the book implies. This diminishes as innings accumulate through May–June.

**Known limitation — individual batter small samples**: Per-player wRC+ is current-season only. In April with 20–50 PA per player, values can be noisy even with the PA trust curve applied. The lineup adjustment is most reliable from June onward when players have 150+ PA. The prior-blended team wRC+ that anchors the blend provides a stable floor.
