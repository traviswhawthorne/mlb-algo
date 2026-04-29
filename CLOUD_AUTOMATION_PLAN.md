# MLB Cloud Automation Plan

Fully free cloud automation using **GitHub Actions** (compute) + **Gmail** (notifications).
No local machine required. No paid services. No execution yet — this is the design document.

---

## How It Works

GitHub Actions has no concept of a persistent long-running process. Instead, a
lightweight **coordinator** workflow fires every 20 minutes during game hours. Each
run takes ~5–30 seconds to decide what to do, then either exits immediately or
runs the relevant script.

```
Every 20 min (noon → 1 AM ET):

  coordinator.py wakes up
    ↓
  Load today's state from data/state_YYYY-MM-DD.json
    ↓
  ┌─ PICKS PHASE ─────────────────────────────────────────────────────────┐
  │  Any upcoming game missing pitcher/lineup/umpire                      │
  │  AND starts within the next 20–45 min?                                │
  │  → run run.py, send Gmail with picks summary, save state              │
  └───────────────────────────────────────────────────────────────────────┘
    ↓ (once no more upcoming games have missing data)
  ┌─ RESULTS PHASE ────────────────────────────────────────────────────────┐
  │  Been 2+ hours since last game started?                                │
  │  AND all games show Final on MLB API?                                  │
  │  AND results not already run today?                                    │
  │  → sleep 15 min, run results.py, sleep 5 min, run tracker.py          │
  │  → send Gmail with P&L summary, save state                            │
  └────────────────────────────────────────────────────────────────────────┘
    ↓
  Commit updated state file back to repo
  Exit
```

---

## Timing Precision

GitHub Actions has a ~1–2 minute startup delay per run. Combined with a 20-minute
poll interval, picks will run between 20 and 45 minutes before a given game's
first pitch — close enough to capture lineup and umpire data that posts ~1 hour
before game time.

If a game's data posts 30 minutes before first pitch and the last poll was 19
minutes ago, the next poll fires 1 minute later and picks run 29 minutes before
first pitch. Acceptable.

---

## State Tracking

Each workflow run is stateless — it starts with no memory of prior runs. State
persists via a JSON file committed back to the repo at the end of each run.

**`data/state_YYYY-MM-DD.json`** (example):
```json
{
  "date": "2026-05-01",
  "picks_runs": [
    {"ran_at": "2026-05-01T16:22:00Z", "game_pks": [717120, 717121, 717122]},
    {"ran_at": "2026-05-01T19:45:00Z", "game_pks": [717123, 717124]}
  ],
  "results_done": false,
  "tracker_done": false
}
```

The coordinator reads this file at startup to know what has already happened today.
After each action it updates the file and commits it before exiting.

---

## Coordinator Logic (pseudocode)

```python
state  = load_or_create_state_today()
games  = get_todays_games()
now    = utcnow()

# ── PICKS PHASE ──────────────────────────────────────────────────────────────

upcoming_with_missing_data = [
    g for g in games
    if game_start(g) > now                     # hasn't started yet
    and not data_complete(g)                   # missing pitcher/lineup/umpire
    and game_start(g) - now <= timedelta(minutes=45)  # within trigger window
]

if upcoming_with_missing_data:
    run_picks()
    state.record_picks_run([g["game_pk"] for g in upcoming_with_missing_data])
    send_gmail(picks_summary())
    save_and_commit(state)
    exit()

# ── RESULTS PHASE ────────────────────────────────────────────────────────────

if state.results_done:
    exit()  # nothing left to do today

last_game_start = max(game_start(g) for g in games)

if now < last_game_start + timedelta(hours=2):
    exit()  # too early to check — games are definitely still running

if not all_games_final():
    exit()  # still in progress, check again next poll

# All games are Final — wait 15 min then score results
print("All games Final. Sleeping 15 min...")
sleep(900)
run_results()

print("Sleeping 5 min before tracker...")
sleep(300)
run_tracker()

state.results_done = True
state.tracker_done = True
send_gmail(results_summary())
save_and_commit(state)
```

---

## Minute Budget

GitHub Actions free tier: **2,000 minutes/month** on private repos.
**Making the repo public gives unlimited minutes** — the API key stays private
(stored in GitHub Secrets, never in code), so there is no downside.

Estimate if kept private:

| Run type | Duration | Frequency | Monthly |
|---|---|---|---|
| Quick check (no action) | ~1 min (billed minimum) | ~34/day × 30 days | ~1,020 min |
| run.py actual execution | ~5 min | ~5/day × 30 days | ~750 min |
| results.py + tracker.py | ~10 min + 15 min sleep | 1/day × 30 days | ~750 min |
| **Total** | | | **~2,520 min** |

**Recommendation: make the repo public** to avoid the budget concern entirely.
If staying private, widen the polling interval to 30 minutes to stay under 2,000.

---

## Gmail Notification Format

Sent via **Gmail SMTP with an App Password** (free, no extra library — just Python's
built-in `smtplib`). Requires 2-factor auth on your Google account, then generate
an App Password in Google account settings.

**Picks notification** (sent after each run.py):
```
Subject: MLB Picks — May 1 (Pass 2 of ~3)

8 games today | 3 picks this pass

• LAD -1.5 RL  +145  EV 12.3%  $28
• NYY ML       -115  EV  8.1%  $18
• CHC/STL O8.5 -110  EV  6.4%  $14

Data status:
  Pitchers confirmed:  8/8
  Lineups confirmed:   6/8  ← 2 games still pending
  Umpires confirmed:   7/8  ← 1 game still pending

Next run: ~6:45 PM ET (Cardinals/Cubs first pitch 7:05 PM)
```

**Results notification** (sent after tracker.py):
```
Subject: MLB Results — Apr 30

3 bets | 2 wins 1 loss | +$31.40

• LAD -1.5 RL  +145  WIN   +$28.00
• NYY ML       -115  WIN   +$17.39
• CHC/STL O8.5 -110  LOSS  -$14.00

Season: 47-31 | ROI +8.2% | P&L +$284
```

---

## What Needs to Change in the Codebase

### 1. `config.py` — remove hardcoded API key

```python
# Before
ODDS_API_KEY = "23d6c9e831180dd8136d8116fa629d90"

# After
import os
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
```

### 2. Output file paths — Google Drive API instead of local mount

`config.py` currently writes to `G:\My Drive\MLB_Picks.xlsx` (local Windows path
that doesn't exist on a GitHub runner). The scripts write Excel to a temp local
path, then a shared upload function pushes the file to Google Drive via API.

Requires a **Google service account** (free):
- Create a project in Google Cloud Console
- Enable the Drive API
- Create a service account, download the JSON credentials
- Share your Google Drive output folder with the service account email

The service account JSON is stored as a GitHub Secret and written to a temp file
at the start of each workflow run.

### 3. `push_picks_to_web.py` — replace local file copy with GitHub API push

Currently copies picks JSON to a local OneDrive path, then runs a local `git push`.
On a GitHub runner, replace this with a direct GitHub API call using a Personal
Access Token (PAT) with write access to the `resume-chat` repo.

### 4. New file: `coordinator.py`

New script that contains all the logic described above. The GitHub Actions workflow
simply runs `python coordinator.py` — coordinator handles everything else.

### 5. New file: `requirements.txt`

Needed so the GitHub runner can install dependencies. Currently there is no
`requirements.txt` — dependencies need to be inventoried from the existing imports.

### 6. Gmail credentials

Two new GitHub Secrets:
- `GMAIL_USER` — your Gmail address
- `GMAIL_APP_PASSWORD` — the App Password generated in Google account settings

---

## GitHub Actions Workflow

One workflow file: `.github/workflows/mlb_coordinator.yml`

```yaml
name: MLB Coordinator

on:
  schedule:
    - cron: '0,20,40 16-23 * * *'   # noon–7 PM ET  (16–23 UTC)
    - cron: '0,20,40 0-5 * * *'     # 8 PM–1 AM ET  (0–5 UTC)
  workflow_dispatch:                 # allow manual trigger for testing

permissions:
  contents: write                    # needed to commit state file back to repo

jobs:
  coordinate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - run: pip install -r requirements.txt

      - run: python coordinator.py
        env:
          ODDS_API_KEY:               ${{ secrets.ODDS_API_KEY }}
          GDRIVE_SERVICE_ACCOUNT_JSON: ${{ secrets.GDRIVE_SERVICE_ACCOUNT_JSON }}
          RESUME_REPO_TOKEN:          ${{ secrets.RESUME_REPO_TOKEN }}
          GMAIL_USER:                 ${{ secrets.GMAIL_USER }}
          GMAIL_APP_PASSWORD:         ${{ secrets.GMAIL_APP_PASSWORD }}
```

---

## Secrets Required in GitHub

| Secret | Value |
|---|---|
| `ODDS_API_KEY` | Current key from `config.py` |
| `GDRIVE_SERVICE_ACCOUNT_JSON` | Full JSON contents of the service account file |
| `RESUME_REPO_TOKEN` | GitHub PAT with `contents: write` on `resume-chat` repo |
| `GMAIL_USER` | `traviswhawthorne@gmail.com` |
| `GMAIL_APP_PASSWORD` | App Password from Google account → Security → App Passwords |

---

## Edge Cases

| Scenario | Behavior |
|---|---|
| Pitcher TBD at run time | Picks run without that pitcher's stats; model degrades gracefully |
| Lineup never posted | Final picks run happens at 45-min window with whatever data exists |
| Umpire not assigned until day-of | Next poll detects it and triggers a run |
| Doubleheader | Both games tracked independently in the data-readiness check |
| No games today | Coordinator exits immediately; no picks or results run |
| run.py crashes | Logged to Actions run; state not updated; next poll retries |
| Game suspended mid-inning | `all_games_final()` returns False; polling continues until it resumes and finishes |
| Extra innings / West Coast late games | No hard cutoff — polling continues until Final |
| Workflow run already in progress when next poll fires | GitHub queues or skips; state file prevents duplicate results runs |

---

## Setup Steps (when ready to execute)

1. Push MLB repo to GitHub (**public** recommended for unlimited Actions minutes)
2. Create Google Cloud project → enable Drive API → create service account → download JSON
3. Share your Google Drive output folder with the service account email address
4. Generate a Gmail App Password (Google account → Security → 2-Step Verification → App Passwords)
5. Create a GitHub PAT with `contents: write` scope on the `resume-chat` repo
6. Add all five secrets to the MLB repo's Settings → Secrets
7. Create `requirements.txt` from existing imports
8. Write `coordinator.py` with the logic above
9. Update `config.py` to read `ODDS_API_KEY` from environment
10. Add Google Drive upload function (shared utility used by `run.py`, `results.py`, `tracker.py`)
11. Update `push_picks_to_web.py` to use GitHub API instead of local file copy
12. Create `.github/workflows/mlb_coordinator.yml`
13. Test via `workflow_dispatch` (manual trigger) before relying on the schedule

---

## Estimated Effort

| Task | Complexity |
|---|---|
| Push repo to GitHub, configure secrets | Low |
| `requirements.txt` | Low |
| Update `config.py` for env vars | Low |
| Google Drive upload utility function | Medium |
| Update `push_picks_to_web.py` for GitHub API | Medium |
| Write `coordinator.py` | Medium |
| Gmail notification function | Low |
| Workflow YAML file | Low |
| End-to-end testing | Medium |

Total: 3–5 hours of focused work.
