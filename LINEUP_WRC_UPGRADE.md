# Lineup wRC+ Upgrade — COMPLETED (April 2026)

## What changed

- `get_batter_stats()` in `src/stats_fetcher.py` now returns `{player_id: wrc_plus}` instead of `{player_id: ops}`
  - Fetches full counting stats (H, 2B, 3B, HR, BB, IBB, HBP, SF, AB, R)
  - Computes individual wOBA using `_compute_woba()` (same linear weights as team model)
  - Derives league averages (lg_woba, lg_r_per_pa) from the same player pool
  - Converts to wRC+ via `_woba_to_wrc_plus()` at neutral park factor
  - Cache file renamed to `batters_wrc_{season}_{date}.json`

- `adjust_wrc_for_lineup()` in `src/name_matcher.py` updated:
  - Averages the lineup's individual wRC+ values directly (no more OPS-to-wRC+ proxy)
  - 60/40 blend (team season / lineup average) and ±15 cap unchanged
  - Minimum 6 players required unchanged

## Known follow-on (not yet implemented)
Individual batter wRC+ values are current-season only — no prior-year blend.
The team wRC+ they're blended with IS prior-blended, providing an anchor.
Consider adding per-player prior-year blending once players hit 150+ PA (around June).
