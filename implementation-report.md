# Implementation report — 관심 종목 일일 리포트 (rules-based watchlist tab)

Scope: this clone only. Not pushed. No secrets, AI/search APIs, or new dependencies (stdlib + curl).

## TDD evidence (RED → GREEN)

All runs: `python3 -m unittest discover -s tests -v` (from the repo root).

| Slice | RED (before production code) | GREEN |
|---|---|---|
| 1 URL safety, payload normalization, indicators | `Ran 1 test … FAILED (errors=1)` — `ModuleNotFoundError: No module named 'watchlist'` | `Ran 28 tests … OK` |
| 2 score / labels / missing / non-finite | `Ran 52 tests … FAILED (errors=24)` — `AttributeError: module 'watchlist' has no attribute 'score_indicators' / 'label_for'` | `Ran 52 tests … OK` |
| 3 per-symbol failure, stale preservation, safe errors, `fetch_data` wiring | `Ran 72 tests … FAILED (errors=20)` — missing `build_stock`, `refresh_watchlist`, `fetch_data.STEPS`, … | `Ran 72 tests … OK` |
| 4 generator (3rd tab, 3 cards, escaping, no-AI, formula, dates, unavailable) + freshness | `Ran 96 tests … FAILED (errors=21)` — missing `generate.build_page`, `watchlist.freshness` | `Ran 96 tests … OK` |
| 5 workflow runs tests first | part of slice-4 RED: `ValueError: substring not found` (no test step in `update.yml`) | included in the 96 OK |
| 6 (QA follow-up) watchlist CSS text ≥ 12px | `FAILED (failures=1)` | `Ran 97 tests … OK` (final) |

Honest notes on the cycle:
- Slice 1 first GREEN attempt had **1 failure that was a real bug**: the symbol regex used `$`, which accepts a trailing newline (`"AAPL\n"`). Fixed with `fullmatch`.
- Slice 4 first GREEN attempt had 3 failures that were **bugs in my tests**, fixed in the tests (not by weakening production behavior): the card slicer also captured the trailing `<details>` text; the advice-word check matched `과매도` (RSI term) as `매도`; the tab-label needles didn't match the existing `<span class="flag">` markup. The `nan/inf` check was also rewritten to word boundaries (`Finance` contains "nan").
- I first wrote the RSI test against StockCharts' published 70.53; hand-summing Wilder's example gives gains 3.34 / losses 1.40 → 70.46 (StockCharts rounds intermediate averages). The test locks the hand-derived 70.46 plus a hand-computed Wilder smoothing step.
- The 3 workflow guard tests (single workflow, schedules unchanged, no secrets/extra actions) passed before the change on purpose: they are regression guards, not RED tests. Only "tests run before fetch/generate" was RED.
- Fixtures in `tests/fixtures/` are **synthetic** payloads in Yahoo's chart schema (regenerate: `python3 tests/make_fixtures.py`), plus an error object, a too-short payload and a malformed payload. They are not recorded live responses.

Formula (locked by tests; also shown in the UI `<details>` and README): start 50; close vs SMA20 ±6 / SMA50 ±8 / SMA200 ±10; SMA20 vs SMA50 ±6; 20d return 0.5 pt per 1%p capped ±8; RSI14 ≥70 −8, ≤30 +4; volume ≥1.5× prior-20 avg with up day +4 / down day −4. Round half up, clamp 0–100. Label: ≥65 우호, ≤40 주의, else 관망. Any missing input → 0 pts for it, `quality=partial`, score capped at 64.
RSI14: Wilder (seed = simple mean of first 14 changes, then `(prev*13+cur)/14`).

## Verification

- `python3 -m unittest discover -s tests -v` → `Ran 97 tests … OK`
- `python3 -m py_compile fetch_data.py generate.py watchlist.py tests/test_watchlist.py tests/make_fixtures.py` → OK
- `git diff --check` → clean
- `python3 generate.py && cp dashboard.html index.html` → OK; `dashboard.html` identical to `index.html`
- HTML assertions: exactly 3 `wcard` articles; `pane-us`/`pane-kr`/`pane-wl` + 3 radios present; 0 `<script`; no `/Users/`, `/home/`, `api_key`, `secret`, `utm_`, `token=`, reference-site URL, or placeholder/fake wording.

### Live `python3 fetch_data.py` (run at 2026-11-13 16:01 UTC)
Exit 0; `updated: hy, vix, trend_us, real_yield, fx, cape, watchlist / kept previous: 없음`.

| Symbol | status | source date | daily points | close | score / label |
|---|---|---|---|---|---|
| 005930.KS | ok | 2026-11-12 | 244 | 208,000 KRW | 20 / 주의 |
| 000660.KS | ok | 2026-11-12 | 244 | 1,254,000 KRW | 20 / 주의 |
| AAPL | ok | 2026-11-12 | 251 | 354.88 USD | 82 / 우호 |

Why all three show 11-12: checked against raw Yahoo responses — AAPL's 11-13 bar was still in progress (regular session 14:30–21:00 UTC) so it is excluded by design; Yahoo's 11-13 bars for the KS symbol had `close: null` after the close, so they are dropped. Nothing was invented or back-filled.

### Browser QA
Local server on an unused port (18431) serving a temp copy of `index.html` with a unique title sentinel (`WLQA-…`), verified via `curl` before QA. Playwright/Chromium; mobile used true CDP `Emulation.setDeviceMetricsOverride` (390×844, dpr 2, mobile). For each of US / KR / 관심 종목 tabs at 1440×900 and 390×844:
- only the selected pane visible; document `scrollWidth` == viewport (1440 / 390); 0 elements past the viewport edge; 0 clipped-overflow elements (also with the formula `<details>` opened)
- 0 console errors, 0 failed requests
- tap targets (3 tabs + formula summary) all 44px
- watchlist pane: no text under 12px; 3 cards with name, symbol, label, score, price, 1d/20d, SMA20/50/200 relation, RSI14, volume ratio, reason chips, source date + freshness

Screenshots (not committed — they embed live data and would go stale): `/tmp/wl-qa/shots/{desktop-1440x900,mobile-390x844}-{us,kr,wl,wl-formula-open}.png`.

## Known limits / not done
- Reasons for 우호/관망/주의 are mechanical; the score is a summary of past price action, not a forecast.
- Pre-existing, untouched: `scripts/update_toss_market_dashboard.sh` contains a local absolute path (outside this change's scope).
- `data.json` also carries the live refresh of the existing `us`/`kr` auto-indicators produced by the required `fetch_data.py` run.
- GitHub Actions cannot be exercised locally; the workflow change is covered by the text-level test only.

---

# Follow-up: two independent-review deployment blockers

Scope: `fetch_data.py`, `watchlist.py`, tests/fixtures, README. Score formula, default 3 symbols, UI, schedules, US/KR logic untouched. Not pushed; new commit on top of `bfa71a2`.

## Blocker 1 — redirect host validation
- Problem: `build_chart_url` validates only the **initial** URL; `curl -L` could follow a 3xx to another host, and `--proto-redir =https` limits the scheme only.
- Fix: `fetch_data.fetch(..., follow_redirects=True)` (default = unchanged legacy behavior, `-sL`). The watchlist default fetcher passes `follow_redirects=False`: command uses `-s` (no `-L`), keeps `--proto =https --proto-redir =https`, adds `-w "\n%{http_code}"`, and raises `ConnectionError` unless the HTTP status is 2xx. A 3xx therefore is never parsed as stock data → existing fail-soft (prior → `stale`, none → `unavailable`, code `fetch_failed`).
- Wording: this is **not** effective-URL validation; redirects are simply not followed. README/comments say so.
- Side effect (accepted): a non-2xx Yahoo reply (e.g. 404 JSON error) is now `fetch_failed` instead of `invalid_payload`; both are fail-soft.

## Blocker 2 — incomplete-session exclusion failed open
- Problem: `normalize_chart` swallowed missing/malformed `meta.currentTradingPeriod.regular` and could keep the in-progress daily bar.
- Fix: new `_regular_session(meta)` is always required: `regular` must be a dict; `start`/`end` true ints (not bool), positive, `start < end`; otherwise `WatchlistDataError` → existing fail-soft (`invalid_payload`). Active session `[start, end)` still drops the last row when it belongs to that session; closed session (now ≥ end) keeps it.
- Synthetic fixtures (`tests/make_fixtures.py`, regenerated) and `ramp_payload` now include valid regular-session metadata (the only fixture change).

## TDD evidence
RED (tests added first; production untouched; `ramp_payload` + fixtures already carrying valid metadata so old tests stayed green):
`python3 -m unittest discover -s tests` → `Ran 119 tests … FAILED (failures=13, errors=2)`
- Blocker 2 (11 failures): missing `currentTradingPeriod`, missing `regular` key, non-dict period/regular, missing start/end, non-int/bool/float/str/NaN bounds, non-positive bounds, reversed/empty window, plus 2 fail-soft tests (prior→stale, none→unavailable).
- Blocker 1: 2 errors (`fetch()` has no `follow_redirects` → `TypeError`) and 3 failures (command still contained `-sL`; 3xx status accepted/parsed; default fetch path).
- Passed-before-change on purpose (regression guards, not RED): 3 legacy-fetch tests (`-sL` command unchanged, `https_only` still follows redirects, browser UA), active/closed-session boundary tests, bar-from-earlier-session test, fixture-metadata test.

GREEN (after minimal production change): `python3 -m unittest discover -s tests -v` → `Ran 119 tests … OK`.

## Verification
- `python3 -m unittest discover -s tests -v` → **Ran 119 tests, OK**
- `python3 -m py_compile fetch_data.py generate.py watchlist.py tests/test_watchlist.py tests/make_fixtures.py` → OK
- `python3 generate.py && cp dashboard.html index.html` → OK; `cmp dashboard.html index.html` identical; 3 `wcard` articles, 0 `<script`
- `git diff --check` → clean

### Live `python3 fetch_data.py` (2026-11-13 16:11 UTC, redirects off)
Exit 0; `updated: hy, vix, trend_us, real_yield, fx, cape, watchlist / kept previous: 없음`.

| Symbol | status | source date | close | score / label |
|---|---|---|---|---|
| 005930.KS | ok | 2026-11-12 | 208,000 KRW | 20 / 주의 |
| 000660.KS | ok | 2026-11-12 | 1,254,000 KRW | 20 / 주의 |
| AAPL | ok | 2026-11-12 | 354.88 USD | 82 / 우호 |

Same values/dates as the previous run; live Yahoo meta still supplies valid `regular` bounds. `data.json` changes besides this are the live refresh of existing `us` auto-indicators (VIX/S&P note).
