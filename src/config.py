"""All tunable values for the experiment live here.

Nothing in this file may change during the 4 week test. If a value must
change, bump RULES_VERSION so the logged rows stay comparable.
"""

# v1.1: the Friday close and the weekend snapshot no longer depend on a run
# landing inside one specific hour, because GitHub's scheduler drops runs. The
# thresholds are untouched. No trade had been taken under v1.
#
# v2: the news window widens to everything published since the previous run
# (it used to be the last 90 minutes, which missed most of the news when runs
# land hours apart), and the multi bot race starts (scripts/leaderboard.py).
# Trading thresholds are unchanged. Rows are not comparable with v1.x on input.
RULES_VERSION = "v2"

# ---------------------------------------------------------------- model ----
ANTHROPIC_MODEL = "claude-sonnet-5"
# Sonnet 5 runs adaptive thinking and rejects `temperature`, so the budget has
# to cover reasoning tokens as well as the JSON. `effort` is the cost dial.
ANTHROPIC_MAX_TOKENS = 2000
ANTHROPIC_EFFORT = "low"
# USD per million tokens, used only to write an estimated cost into `notes`.
COST_PER_MTOK_INPUT = 2.00
COST_PER_MTOK_OUTPUT = 10.00

# ----------------------------------------------------------------- news ----
# The window for a run is the time since the previous run plus a margin,
# clamped to [NEWS_LOOKBACK_MINUTES, NEWS_LOOKBACK_MAX_MINUTES]; with no
# previous run it is NEWS_LOOKBACK_MINUTES.
NEWS_LOOKBACK_MINUTES = 90
NEWS_LOOKBACK_MAX_MINUTES = 720
NEWS_WINDOW_MARGIN_MINUTES = 10
MAX_HEADLINES_PER_RUN = 40
DEDUPE_LOOKBACK_ROWS = 1000

SEARCH_TERMS = [
    "oil",
    "Brent",
    "crude",
    "OPEC",
    "Iran",
    "Hormuz",
    "Saudi",
    "Aramco",
    "tanker",
    "pipeline",
    "ceasefire",
    "sanctions",
    "Red Sea",
]

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
GOOGLE_NEWS_PARAMS = {"hl": "en-US", "gl": "US", "ceid": "US:en"}
GNEWS_ENDPOINT = "https://gnews.io/api/v4/search"

# ---------------------------------------------------------------- price ----
OILPRICE_BASE_URL = "https://api.oilpriceapi.com/v1"
OILPRICE_LATEST_PATH = "/prices/latest"
OILPRICE_RANGE_PATH = "/prices"
# Observed: the range endpoint caps a page at 100 points and ignores a larger
# per_page. A wide window therefore comes back silently truncated, so we ask in
# narrow windows around each target instead of one sweep.
OILPRICE_RANGE_PER_PAGE = 100
OILPRICE_RANGE_PAGE_CAP = 100
BRENT_CODE = "BRENT_CRUDE_USD"
HTTP_TIMEOUT_SECONDS = 20

# --------------------------------------------------------- market hours ----
# CME crude: Sunday 17:00 open, Friday 16:00 close, daily break 16:00 to 17:00.
MARKET_TZ = "America/Chicago"
SUNDAY_OPEN_HOUR = 17
FRIDAY_CLOSE_HOUR = 16
DAILY_BREAK_START_HOUR = 16
DAILY_BREAK_END_HOUR = 17

# The Friday close is meant to be made by a run inside the 15:00 hour. GitHub
# delivers only a few runs a day, so that run is often missing; the first run
# after the 16:00 close then closes the position late (see rules.decide).
FRIDAY_NO_NEW_ENTRY_HOUR = 10
FRIDAY_FORCED_CLOSE_HOUR = 15

# --------------------------------------------------------- trading rules ----
ENTRY_LONG_SCORE = 6
ENTRY_SHORT_SCORE = -6
EXIT_LONG_SCORE = -3
EXIT_SHORT_SCORE = 3
ENTRY_CONFIDENCES = ("medium", "high")
TRAILING_STOP_PCT = 3.0
SPREAD_USD_PER_BBL = 0.05
POSITION_SIZE_UNITS = 1

# --------------------------------------------------------------- broker ----
# Optional mirror of the paper position onto an IG demo account (src/broker.py).
# Off unless BROKER_MODE=demo. None of this changes the paper rules above.
#
# The stop held at IG is a backstop for the hours between runs, not a rule.
# It is wider than the 3% trailing stop so the paper rules normally exit first
# and the demo account tracks the paper trades.
BROKER_STOP_PCT = 5.0
# Hard ceiling on IG_SIZE, the stake per point.
BROKER_MAX_SIZE = 5

# ------------------------------------------------------------- outcomes ----
CONTEXT_SIGNAL_ROWS = 24
OUTCOME_HORIZONS_HOURS = {"1h": 1, "4h": 4, "24h": 24, "72h": 72}
OUTCOME_MATCH_WINDOW_MINUTES = 20

# Forward prices are looked up from the price API rather than inferred from
# whichever runs happened to fire. GitHub's scheduler drops runs, so relying on
# our own logged prices leaves holes in exactly the columns the experiment is
# built to measure. At most ONE range call per run, covering every pending
# horizon at once.
OUTCOME_USE_PRICE_API = True
OUTCOME_API_MAX_RANGE_DAYS = 8
# Windows are merged where they overlap, so a steady hour needs one or two
# calls. The cap bounds a catch up after a long outage; the rest waits for the next run.
OUTCOME_API_CALLS_PER_RUN = 6

# ------------------------------------------------------------ scheduling ----
# The job is scheduled hourly, but GitHub delivers only about 3 to 4 scheduled
# runs a day for this repository. Asking every 10 minutes was tried and made no
# measurable difference, so the cron is hourly again. This guard stays because
# it is harmless and an external scheduler needs it: a run that lands less
# than this many minutes after the previous one is skipped before any news,
# price or model call. 55 rather than 60 so a run at :03 does not block the
# one at :58.
MIN_MINUTES_BETWEEN_RUNS = 55

# --------------------------------------------------------------- alerts ----
# The job used to exit 0 while logging nothing, so a dead API key looked like a
# healthy run for 19 days. After this many consecutive scoring failures the run
# exits non zero, which is what makes GitHub send its own failure email.
ALERT_AFTER_CONSECUTIVE_ERRORS = 3
# The price feed fails soft to a blank cell, exactly as scoring used to, so it
# can die as quietly as the Anthropic key did. A few blanks are normal (the
# vendor rate limits); a long run of them is a dead key or a dead account.
ALERT_AFTER_CONSECUTIVE_BLANK_PRICES = 6

# -------------------------------------------------------------- weekend ----
MONDAY_COMPLETION_HOUR_UTC = 12
# How close a run must be to a weekend target before its own price may stand in
# for the API's. Beyond this the cell stays blank and waits: a price recorded
# hours from the moment its column is named after is worse than no price.
WEEKEND_FALLBACK_WINDOW_MINUTES = 120
# A weekend price still missing this long after its target is given up on,
# so it stops costing a price API call on every run. It is left blank.
WEEKEND_GIVE_UP_DAYS = 7
