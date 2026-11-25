"""Point in time context logged on every run, so later analysis and new replay
bots can see what the market looked like when a row was written.

THE RULE: nothing in here may change a run. Every fetcher has a short timeout
and one try, every failure becomes a blank plus a short token in
`context_errors`, and the callers in src/main.py wrap the lot in try/except.
The score, Bot 1's decision and every pre existing signals column are computed
before anything here is looked at.

Layout: small pure functions that take data already fetched (unit tested with
no network), then thin fetchers, then `gather` which glues them together.

Definitions worth knowing:

* Daily series: FRED DCOILBRENTEU (blank holiday values dropped) or, when FRED
  fails, backtest/brent_daily.csv. FRED lags about 5 days, so the series is
  extended with the bot's own log: for each later UTC weekday, the last logged
  `brent_price` of that date, then this run's live price as the final point
  (on a weekend it stands in for that weekend's Friday). Weekend log rows are
  skipped so a closed market cannot add false zero returns.
* vol20_pct, vol60_pct: SAMPLE standard deviation (n minus 1) of the last 20
  (60) daily percentage returns, in percent, so 2.31 means 2.31 percent a day.
* ma50, ma200: plain mean of the last 50 (200) points of that series. ma200 is
  blank when the live price is missing or there are fewer than 200 points.
* Momentum N: percent change of the last point over the point N trading days
  earlier. range20_pos: where the last point sits between the lowest (0) and
  highest (1) of the last 20 points.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import os
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import requests

from . import config, outcomes
from .sheets import CONTEXT, SCHEMAS

CONTEXT_VERSION = "c1"

NEW_YORK = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
DAILY_FILE = ROOT / "backtest" / "brent_daily.csv"
OPEC_FILE = ROOT / "data" / "opec_meetings.csv"

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_BRENT = "DCOILBRENTEU"
FRED_WTI = "DCOILWTICO"
FRED_OVX = "OVXCLS"
FRED_USD = "DTWEXBGS"
FRED_HISTORY_DAYS = 420
FRED_SHORT_DAYS = 45

# Fetch limits: one try each, in parallel, with a hard overall budget.
FETCH_TIMEOUT_SECONDS = 8
TOTAL_BUDGET_SECONDS = 30

# Events.
EIA_HOUR, EIA_MINUTE = 10, 30
EIA_WINDOW_BEFORE = timedelta(hours=4)
EIA_WINDOW_AFTER = timedelta(hours=2)

# How far back the nearest earlier row may be for each prior move horizon:
# hours -> tolerance in minutes. Runs are irregular, so a 1 hour move is often
# simply unavailable and stays blank.
PRIOR_HORIZONS: Dict[int, int] = {1: 45, 4: 90, 24: 240, 72: 480}

# EIA API v2, same open DEMO_KEY style as scripts/fetch_brent_daily.py. A real
# key in EIA_API_KEY overrides it. UNVERIFIED against the live API from the
# development machine (blocked there): run the context_check workflow.
EIA_URL = "https://api.eia.gov/v2/petroleum/stoc/wstk/data/"
EIA_SERIES = "WCESTUS1"

# CFTC Commitments of Traders, Disaggregated, futures only, via the public
# Socrata API. 067651 is NYMEX WTI. UNVERIFIED against the live API.
CFTC_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
CFTC_WTI_CODE = "067651"


# ============================================================ pure: maths ===

def _round(value: Optional[float], digits: int = 2) -> Any:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return ""
    return round(value, digits)


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def stdev_sample(values: Sequence[float]) -> float:
    """Sample standard deviation (divides by n minus 1)."""
    n = len(values)
    centre = mean(values)
    return math.sqrt(sum((v - centre) ** 2 for v in values) / (n - 1))


def pct_returns(prices: Sequence[float]) -> List[float]:
    return [(b / a - 1.0) * 100.0 for a, b in zip(prices, prices[1:]) if a]


def vol_pct(prices: Sequence[float], returns: int) -> Optional[float]:
    """Sample stdev of the last `returns` daily percent returns; None if short."""
    if len(prices) < returns + 1:
        return None
    rets = pct_returns(prices[-(returns + 1):])
    if len(rets) < returns:
        return None
    return stdev_sample(rets)


def moving_average(prices: Sequence[float], n: int) -> Optional[float]:
    if len(prices) < n:
        return None
    return mean(prices[-n:])


def momentum_pct(prices: Sequence[float], n: int) -> Optional[float]:
    if len(prices) < n + 1 or not prices[-1 - n]:
        return None
    return (prices[-1] / prices[-1 - n] - 1.0) * 100.0


def range_position(prices: Sequence[float], n: int = 20) -> Optional[float]:
    if len(prices) < n:
        return None
    window = prices[-n:]
    low, high = min(window), max(window)
    if high == low:
        return None
    return (prices[-1] - low) / (high - low)


# ================================================== pure: daily series ======

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_fred_csv(text: str) -> List[Tuple[str, float]]:
    """(date, value) pairs from a FRED fredgraph.csv, oldest first.

    Handles the header (observation_date or DATE) and the blank or "." values
    FRED writes on holidays by dropping them. Also reads backtest/brent_daily.csv,
    which has the same two columns.
    """
    found: Dict[str, float] = {}
    for rec in csv.reader(io.StringIO(text or "")):
        if len(rec) < 2:
            continue
        day, raw = rec[0].strip(), rec[1].strip()
        if not _DATE.match(day):
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if math.isfinite(value):
            found[day] = value
    return sorted(found.items())


def extend_daily_series(
    daily: Sequence[Tuple[str, float]],
    logged: Sequence[Tuple[datetime, float]],
    live_price: Optional[float],
    now_utc: datetime,
) -> Tuple[List[Tuple[str, float]], int]:
    """Daily history plus the bot's own log, then the live price.

    `logged` is (UTC time, price) pairs, oldest first. For every UTC weekday
    after the last daily date the last logged price of that date is added, then
    `live_price` is the final point (dated that weekday, or the previous Friday
    when today is a weekend). Returns (series, number of dates added).
    """
    points = list(daily)
    last = points[-1][0] if points else ""
    added: Dict[str, float] = {}
    for when, price in logged:
        if when.weekday() >= 5:
            continue
        key = when.date().isoformat()
        if key > last:
            added[key] = price  # later rows of the same date overwrite earlier ones
    if live_price is not None:
        day = now_utc.date()
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        key = day.isoformat()
        if key > last:
            added[key] = live_price
    extra = sorted(added.items())
    return points + extra, len(extra)


def daily_metrics(series: Sequence[Tuple[str, float]], live_price: Optional[float]) -> dict:
    prices = [p for _, p in series]
    ma50 = moving_average(prices, 50)
    ma200 = moving_average(prices, 200) if live_price is not None else None
    last = prices[-1] if prices else None
    dist = None
    if ma200 and live_price is not None:
        dist = (live_price / ma200 - 1.0) * 100.0
    return {
        "daily_points": len(prices),
        "ma50": _round(ma50),
        "ma200": _round(ma200),
        "vol20_pct": _round(vol_pct(prices, 20)),
        "vol60_pct": _round(vol_pct(prices, 60)),
        "dist_ma200_pct": _round(dist),
        "mom_21d_pct": _round(momentum_pct(prices, 21)),
        "mom_63d_pct": _round(momentum_pct(prices, 63)),
        "mom_252d_pct": _round(momentum_pct(prices, 252)),
        "range20_pos": _round(range_position(prices, 20), 3),
        "_last": last,
    }


def latest_value(points: Sequence[Tuple[str, float]]) -> Tuple[Any, Any]:
    """(value, as of date) of the newest point, or two blanks."""
    if not points:
        return "", ""
    day, value = points[-1]
    return value, day


def brent_wti_spread(brent: Sequence[Tuple[str, float]],
                     wti: Sequence[Tuple[str, float]]) -> Tuple[Any, Any]:
    """Brent minus WTI on the newest date both series have; (spread, date)."""
    wti_by_day = dict(wti)
    for day, value in reversed(list(brent)):
        if day in wti_by_day:
            return round(value - wti_by_day[day], 2), day
    return "", ""


# ======================================================= pure: price path ===

def price_path(series: Sequence[Tuple[datetime, float]], now_utc: datetime,
               live_price: Optional[float]) -> dict:
    """Percent change from the nearest earlier logged price at each horizon.

    The row used is the latest one at or before now minus the horizon, and it
    must be no older than the horizon's tolerance, else the cell is blank.
    """
    out = {}
    for hours, tolerance in PRIOR_HORIZONS.items():
        key = "prior_move_%dh_pct" % hours
        out[key] = ""
        if live_price is None:
            continue
        target = now_utc - timedelta(hours=hours)
        floor = target - timedelta(minutes=tolerance)
        chosen = None
        for when, price in series:
            if when <= target and when >= floor:
                chosen = price  # series is oldest first, so the last hit is the nearest
        if chosen:
            out[key] = _round((live_price / chosen - 1.0) * 100.0)
    return out


def run_history(rows: Sequence[dict], now_utc: datetime) -> dict:
    """Gap since the previous run, its score, and how many runs in 24 hours."""
    times = []
    for row in rows:
        when = outcomes.parse_utc(row.get("run_utc", ""))
        if when is not None:
            times.append(when)
    last_row = rows[-1] if rows else {}
    since = ""
    if times:
        since = _round((now_utc - max(times)).total_seconds() / 3600.0)
    score = last_row.get("score", "") if last_row else ""
    return {
        "hours_since_prev_run": since,
        "prev_score": "" if score is None else score,
        "runs_last_24h": sum(1 for t in times if now_utc - timedelta(hours=24) <= t <= now_utc),
    }


# ============================================================ pure: events ==

def eia_report_times(now_utc: datetime, days_back: int = 2, days_ahead: int = 9) -> List[datetime]:
    """Wednesday 10:30 New York report times around now, as UTC datetimes.

    Built from the New York wall clock, so daylight saving is handled by the
    zone rules. Compared in UTC: aware datetimes sharing one tzinfo subtract as
    naive wall clock times, which would be wrong across a clock change.
    """
    today = now_utc.astimezone(NEW_YORK).date()
    times = []
    for offset in range(-days_back, days_ahead + 1):
        day = today + timedelta(days=offset)
        if day.weekday() == 2:
            local = datetime(day.year, day.month, day.day, EIA_HOUR, EIA_MINUTE, tzinfo=NEW_YORK)
            times.append(local.astimezone(timezone.utc))
    return times


def in_eia_window(now_utc: datetime) -> bool:
    return any(t - EIA_WINDOW_BEFORE <= now_utc <= t + EIA_WINDOW_AFTER
               for t in eia_report_times(now_utc))


def hours_to_next_eia(now_utc: datetime) -> Any:
    upcoming = [t for t in eia_report_times(now_utc) if t >= now_utc]
    if not upcoming:
        return ""
    return _round((min(upcoming) - now_utc).total_seconds() / 3600.0, 1)


def parse_opec_csv(text: str) -> List[date]:
    """Dates from the committed meetings file (date,note). Bad lines are skipped."""
    found = set()
    for rec in csv.reader(io.StringIO(text or "")):
        if not rec or not _DATE.match(rec[0].strip()):
            continue
        try:
            found.add(date.fromisoformat(rec[0].strip()))
        except ValueError:
            continue
    return sorted(found)


def load_opec_dates(path: Path = OPEC_FILE) -> List[date]:
    """Meeting dates, or an empty list if the file is missing, empty or unreadable."""
    try:
        return parse_opec_csv(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def is_opec_day(now_utc: datetime, meetings: Sequence[date]) -> bool:
    """The run's UTC date is a meeting date, or the calendar day before one."""
    today = now_utc.astimezone(timezone.utc).date()
    return any(today == m or today == m - timedelta(days=1) for m in meetings)


def days_to_next_opec(now_utc: datetime, meetings: Sequence[date]) -> Any:
    today = now_utc.astimezone(timezone.utc).date()
    upcoming = [m for m in meetings if m >= today]
    return (min(upcoming) - today).days if upcoming else ""


def event_flags(now_utc: datetime, meetings: Optional[Sequence[date]] = None) -> str:
    """Semicolon separated tokens (eia_window, opec_day), empty when none."""
    if meetings is None:
        meetings = load_opec_dates()
    tokens = []
    if in_eia_window(now_utc):
        tokens.append("eia_window")
    if is_opec_day(now_utc, meetings):
        tokens.append("opec_day")
    return ";".join(tokens)


def events_columns(now_utc: datetime, meetings: Optional[Sequence[date]] = None) -> dict:
    """The three event columns. Needs no network, so it survives a failed gather."""
    if meetings is None:
        meetings = load_opec_dates()
    return {
        "event_flags": event_flags(now_utc, meetings),
        "hours_to_next_eia": hours_to_next_eia(now_utc),
        "days_to_next_opec": days_to_next_opec(now_utc, meetings),
    }


# =============================================== pure: weekly fundamentals ==

def _num(value) -> Optional[float]:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_eia_stocks(payload: dict) -> dict:
    """US commercial crude stocks from an EIA API v2 response.

    UNVERIFIED against the live API (api.eia.gov is blocked on the development
    machine). Written to the documented shape: response.data is a list of
    {"period": "YYYY-MM-DD", "value": "123456", ...} for series WCESTUS1, in
    thousand barrels. Returns level, week change and period; raises ValueError
    when no usable point is there.
    """
    rows = ((payload or {}).get("response") or {}).get("data") or []
    points = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = _num(row.get("value"))
        period = str(row.get("period", ""))[:10]
        if value is not None and _DATE.match(period):
            points.append((period, value))
    if not points:
        raise ValueError("no EIA points")
    points.sort(reverse=True)
    level_period, level = points[0]
    change = round(level - points[1][1], 1) if len(points) > 1 else ""
    return {"level": level, "change": change, "period": level_period}


def parse_cftc(rows: Any) -> dict:
    """Managed money net position for WTI from a Socrata JSON array.

    UNVERIFIED against the live API (publicreporting.cftc.gov is blocked on the
    development machine). Written to the documented field names of the
    Disaggregated Futures Only dataset: report_date_as_yyyy_mm_dd,
    m_money_positions_long_all, m_money_positions_short_all, open_interest_all
    and, as a fallback for the weekly change, change_in_m_money_long_all and
    change_in_m_money_short_all. Raises ValueError when no usable row is there.
    """
    if not isinstance(rows, list):
        raise ValueError("not a list")
    points = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        long_ = _num(row.get("m_money_positions_long_all"))
        short = _num(row.get("m_money_positions_short_all"))
        day = str(row.get("report_date_as_yyyy_mm_dd", ""))[:10]
        if long_ is None or short is None or not _DATE.match(day):
            continue
        points.append((day, long_ - short, _num(row.get("open_interest_all")), row))
    if not points:
        raise ValueError("no CFTC rows")
    points.sort(key=lambda p: p[0], reverse=True)
    day, net, open_interest, newest = points[0]
    if len(points) > 1:
        change: Any = net - points[1][1]
    else:
        d_long = _num(newest.get("change_in_m_money_long_all"))
        d_short = _num(newest.get("change_in_m_money_short_all"))
        change = d_long - d_short if d_long is not None and d_short is not None else ""
    return {
        "net": net, "change": change,
        "open_interest": "" if open_interest is None else open_interest,
        "report_date": day,
    }


# =================================================== pure: news and run meta ==

def news_columns(stats: Optional[dict], fresh: Sequence[Any], now_utc: datetime,
                 window_minutes: Optional[int]) -> dict:
    """News meta from news.LAST_STATS and the headlines sent to the scorer."""
    stats = stats or {}
    items = [h.as_dict() if hasattr(h, "as_dict") else dict(h) for h in fresh]
    ages = []
    for item in items:
        when = outcomes.parse_utc(item.get("published_utc", ""))
        if when is not None:
            ages.append((now_utc - when).total_seconds() / 60.0)
    return {
        "news_window_min": window_minutes if window_minutes is not None else "",
        "news_fetched": stats.get("fetched", ""),
        "news_outside_window": stats.get("outside_window", ""),
        "news_duplicates_dropped": stats.get("duplicates_dropped", ""),
        "news_over_cap_dropped": stats.get("over_cap_dropped", ""),
        "news_new_sent": len(items),
        "news_distinct_sources": len({str(i.get("source", "")).strip() for i in items if i.get("source")}),
        "news_newest_age_min": _round(min(ages), 1) if ages else "",
        "news_oldest_age_min": _round(max(ages), 1) if ages else "",
        "news_feed_errors": ";".join(stats.get("errors") or []),
    }


def prompt_hash() -> str:
    """First 12 hex chars of the SHA 256 of the scorer system prompt, or blank."""
    try:
        from . import scorer

        return hashlib.sha256(scorer.load_system_prompt().encode("utf-8")).hexdigest()[:12]
    except Exception:  # noqa: BLE001 - metadata only
        return ""


def run_meta_columns(now_utc: datetime, usage: Any, quote: Any, price_ms: Optional[int],
                     duration_ms: Optional[int], environ: Optional[dict] = None) -> dict:
    """Who ran, with what, and how long each external call took."""
    env = os.environ if environ is None else environ
    age: Any = ""
    quoted_at = outcomes.parse_utc(getattr(quote, "price_time_utc", "") or "") if quote else None
    if quoted_at is not None and getattr(quote, "ok", False):
        age = _round((now_utc - quoted_at).total_seconds() / 60.0, 1)
    return {
        "github_run_id": env.get("GITHUB_RUN_ID", ""),
        "github_run_attempt": env.get("GITHUB_RUN_ATTEMPT", ""),
        "trigger": env.get("GITHUB_EVENT_NAME", ""),
        "git_sha": (env.get("GITHUB_SHA", "") or "")[:7],
        "rules_version": config.RULES_VERSION,
        "model_id": config.ANTHROPIC_MODEL,
        "prompt_hash": prompt_hash(),
        "llm_input_tokens": getattr(usage, "input_tokens", "") if usage else "",
        "llm_output_tokens": getattr(usage, "output_tokens", "") if usage else "",
        "llm_latency_ms": (getattr(usage, "latency_ms", "") or "") if usage else "",
        "llm_stop_reason": getattr(usage, "stop_reason", "") if usage else "",
        "llm_attempts": (getattr(usage, "attempts", "") or "") if usage else "",
        "price_fetch_ms": "" if price_ms is None else price_ms,
        "price_age_min": age,
        "run_duration_ms": "" if duration_ms is None else duration_ms,
    }


# ================================================================ fetchers ==

@dataclass
class FeedResult:
    ok: bool
    payload: Any = None
    error: str = ""


def reason_for(exc: BaseException) -> str:
    """A short, secret free reason. Never str(exc): requests puts the URL in it,
    and the EIA URL carries an API key."""
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return "http%s" % status if status else "http"
    if isinstance(exc, requests.ConnectionError):
        return "connection"
    if isinstance(exc, ValueError):
        return "parse"
    return type(exc).__name__


def _get(url: str, params: Optional[dict] = None) -> requests.Response:
    # No custom User-Agent on purpose: FRED's edge stalls any request with an
    # unfamiliar one (observed: "oil-signal-logger/1.0" and a made up Mozilla
    # string both hang until the read times out) but answers the library
    # default in under a second.
    response = requests.get(url, params=params, timeout=FETCH_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response


def _soft(fn: Callable[[], Any]) -> FeedResult:
    try:
        return FeedResult(True, fn())
    except Exception as exc:  # noqa: BLE001 - every feed fails soft
        return FeedResult(False, None, reason_for(exc))


def fred_series(series_id: str, days: int, today: Optional[date] = None) -> List[Tuple[str, float]]:
    start = (today or datetime.now(timezone.utc).date()) - timedelta(days=days)
    text = _get(FRED_URL, {"id": series_id, "cosd": start.isoformat()}).text
    points = parse_fred_csv(text)
    if not points:
        raise ValueError("no points")
    return points


def eia_stocks() -> dict:
    key = os.environ.get("EIA_API_KEY", "").strip() or "DEMO_KEY"
    params = {
        "api_key": key, "frequency": "weekly", "data[0]": "value",
        "facets[series][]": EIA_SERIES, "sort[0][column]": "period",
        "sort[0][direction]": "desc", "length": "3",
    }
    return parse_eia_stocks(_get(EIA_URL, params).json())


def cftc_wti() -> dict:
    params = {
        "cftc_contract_market_code": CFTC_WTI_CODE,
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": "2",
    }
    return parse_cftc(_get(CFTC_URL, params).json())


def feed_jobs(now_utc: datetime) -> Dict[str, Callable[[], Any]]:
    today = now_utc.date()
    return {
        "fred_brent": lambda: fred_series(FRED_BRENT, FRED_HISTORY_DAYS, today),
        "fred_wti": lambda: fred_series(FRED_WTI, FRED_SHORT_DAYS, today),
        "fred_ovx": lambda: fred_series(FRED_OVX, FRED_SHORT_DAYS, today),
        "fred_usd": lambda: fred_series(FRED_USD, FRED_SHORT_DAYS, today),
        "eia_stocks": eia_stocks,
        "cftc_cot": cftc_wti,
    }


def fetch_all(now_utc: datetime, budget_seconds: float = TOTAL_BUDGET_SECONDS) -> Dict[str, FeedResult]:
    """Run every feed at once on daemon threads and wait at most the budget.

    A feed still running at the deadline is reported as a timeout and left to
    die with the process: daemon threads never hold the run open.
    """
    jobs = feed_jobs(now_utc)
    results: Dict[str, FeedResult] = {}

    def work(name: str, fn: Callable[[], Any]) -> None:
        results[name] = _soft(fn)

    threads = [threading.Thread(target=work, args=(n, f), daemon=True) for n, f in jobs.items()]
    deadline = datetime.now(timezone.utc) + timedelta(seconds=budget_seconds)
    for thread in threads:
        thread.start()
    for thread in threads:
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        thread.join(timeout=max(0.0, remaining))
    for name in jobs:
        results.setdefault(name, FeedResult(False, None, "budget"))
    return dict(results)


# =================================================================== glue ===

def _error(errors: List[str], name: str, result: Optional[FeedResult]) -> None:
    errors.append("%s:%s" % (name, (result.error if result else "") or "failed"))


def assemble(
    now_utc: datetime,
    live_price: Optional[float],
    signal_rows: Sequence[dict],
    feeds: Dict[str, FeedResult],
    file_points: Sequence[Tuple[str, float]],
    meetings: Sequence[date],
) -> dict:
    """Every gathered column (daily, cross market, events, fundamentals, path).

    Pure: all inputs are already fetched. Returns values keyed by context tab
    column plus `context_errors`.
    """
    errors: List[str] = []
    out: Dict[str, Any] = {}

    # Daily series: FRED, else the committed file, then the bot's own log.
    brent = feeds.get("fred_brent")
    if brent and brent.ok and brent.payload:
        daily, source = list(brent.payload), "fred"
    else:
        _error(errors, "fred_brent", brent)
        daily, source = list(file_points), ("file" if file_points else "")
        if not file_points:
            errors.append("daily_file:missing")
    logged = outcomes.build_price_series(signal_rows)
    series, added = extend_daily_series(daily, logged, live_price, now_utc)
    metrics = daily_metrics(series, live_price)
    metrics.pop("_last", None)
    out.update(metrics)
    out.update({
        "daily_source": source,
        "daily_last_date": daily[-1][0] if daily else "",
        "daily_log_days_added": added,
    })

    # Cross market.
    wti = feeds.get("fred_wti")
    if wti and wti.ok:
        out["wti_price"], out["wti_asof"] = latest_value(wti.payload)
        out["brent_wti_spread"], out["brent_wti_spread_asof"] = brent_wti_spread(daily, wti.payload)
    else:
        _error(errors, "fred_wti", wti)
    for name, price_col, date_col in (("fred_ovx", "ovx", "ovx_asof"),
                                      ("fred_usd", "usd_broad", "usd_broad_asof")):
        feed = feeds.get(name)
        if feed and feed.ok:
            out[price_col], out[date_col] = latest_value(feed.payload)
        else:
            _error(errors, name, feed)

    # Weekly fundamentals.
    eia = feeds.get("eia_stocks")
    if eia and eia.ok:
        out["eia_crude_stocks_kbbl"] = eia.payload["level"]
        out["eia_crude_stocks_chg_kbbl"] = eia.payload["change"]
        out["eia_crude_stocks_period"] = eia.payload["period"]
    else:
        _error(errors, "eia_stocks", eia)
    cftc = feeds.get("cftc_cot")
    if cftc and cftc.ok:
        out["cftc_mm_net"] = cftc.payload["net"]
        out["cftc_mm_net_chg"] = cftc.payload["change"]
        out["cftc_open_interest"] = cftc.payload["open_interest"]
        out["cftc_report_date"] = cftc.payload["report_date"]
    else:
        _error(errors, "cftc_cot", cftc)

    # No network needed from here on.
    out.update(price_path(logged, now_utc, live_price))
    out.update(run_history(signal_rows, now_utc))
    out.update(events_columns(now_utc, meetings))
    out["context_errors"] = ";".join(errors)
    return out


def load_daily_file(path: Path = DAILY_FILE) -> List[Tuple[str, float]]:
    try:
        return parse_fred_csv(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return []


def gather(now_utc: datetime, live_price: Optional[float], signal_rows: Sequence[dict],
           feeds: Optional[Dict[str, FeedResult]] = None) -> dict:
    """Fetch every external feed and assemble the gathered columns.

    May raise; src/main.py catches that and logs blanks. `feeds` lets a caller
    inject already fetched results.
    """
    if feeds is None:
        feeds = fetch_all(now_utc)
    file_points: List[Tuple[str, float]] = []
    brent = feeds.get("fred_brent")
    if not (brent and brent.ok and brent.payload):
        file_points = load_daily_file()
    return assemble(now_utc, live_price, signal_rows, feeds, file_points, load_opec_dates())


def signal_fields(now_utc: datetime, gathered: Optional[dict]) -> dict:
    """The 4 columns appended to the signals row. Never raises.

    context_version is always c1. vol20_pct and ma200 are blank unless the
    context was gathered. event_flags needs no network, so it is computed even
    when gathering failed, and blank only if that too fails.
    """
    gathered = gathered or {}
    try:
        flags = event_flags(now_utc)
    except Exception:  # noqa: BLE001
        flags = ""
    return {
        "context_version": CONTEXT_VERSION,
        "vol20_pct": gathered.get("vol20_pct", ""),
        "ma200": gathered.get("ma200", ""),
        "event_flags": flags,
    }


def build_row(now_utc: datetime, gathered: Optional[dict], news: dict, meta: dict,
              extra_errors: Sequence[str] = ()) -> dict:
    """The context tab row: run meta, news meta, then everything gathered."""
    row: Dict[str, Any] = {"run_utc": now_utc.isoformat(), "context_version": CONTEXT_VERSION}
    row.update(meta)
    row.update(news)
    if not gathered:
        try:
            row.update(events_columns(now_utc))
        except Exception:  # noqa: BLE001
            pass
    errors = [e for e in (gathered or {}).get("context_errors", "").split(";") if e]
    errors.extend(e for e in extra_errors if e)
    row.update(gathered or {})
    row["context_errors"] = ";".join(errors)
    unknown = set(row) - set(SCHEMAS[CONTEXT])
    for key in unknown:  # a stray key is a bug here, not a reason to drop the row
        row.pop(key)
    return row
