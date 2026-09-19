"""All tunable values for the experiment live here.

Nothing in this file may change during the 4 week test. If a value must
change, bump RULES_VERSION so the logged rows stay comparable.
"""

RULES_VERSION = "v1"

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
NEWS_LOOKBACK_MINUTES = 90
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

# Runs fire at :05, so the last run before the 16:00 Friday close is 15:05.
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
# calls. The cap bounds a catch up after a long outage; the rest waits an hour.
OUTCOME_API_CALLS_PER_RUN = 6

# -------------------------------------------------------------- weekend ----
MONDAY_COMPLETION_HOUR_UTC = 12
