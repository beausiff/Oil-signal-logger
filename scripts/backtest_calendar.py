"""Bot 5 ("Calendar only") backtested on daily Brent prices.

    python -m scripts.backtest_calendar

Reads backtest/brent_daily.csv (date,price), no network. Each ISO week: buy at
the first available trading day's price, sell at the last available trading
day's price of that week; weeks with fewer than 2 prices are skipped. P&L comes
from src.rules.pnl, so the $0.05/bbl round-trip spread is charged. Two
variants: no stop, and a 3% trailing stop checked on daily prices.

Writes backtest/calendar_5y.txt and prints it. Standard library only.
"""

from __future__ import annotations

import csv
import math
import os
import random
import sys
from collections import defaultdict
from datetime import date
from typing import Dict, List, Optional, Tuple

from src import config, rules

DATA = os.path.join("backtest", "brent_daily.csv")
OUT = os.path.join("backtest", "calendar_5y.txt")
SIGNALS = os.path.join("backup", "signals.csv")
SEED = 20261122
RESAMPLES = 10000


def load_prices(path: str = DATA) -> List[Tuple[date, float]]:
    rows = []
    with open(path, newline="") as fh:
        for rec in csv.DictReader(fh):
            rows.append((date.fromisoformat(rec["date"]), float(rec["price"])))
    rows.sort()
    return rows


def group_weeks(prices: List[Tuple[date, float]]) -> List[List[Tuple[date, float]]]:
    weeks: Dict[tuple, list] = defaultdict(list)
    for d, p in prices:
        weeks[d.isocalendar()[:2]].append((d, p))
    return [weeks[k] for k in sorted(weeks)]


def trade_week(week: List[Tuple[date, float]], use_stop: bool) -> Optional[dict]:
    """One week's long trade, or None if the week has fewer than 2 prices."""
    if len(week) < 2:
        return None
    entry_day, entry = week[0]
    best = entry
    exit_day, exit_price, reason = week[-1][0], week[-1][1], "friday"
    for d, p in week[1:]:
        best = max(best, p)
        if use_stop and p <= best * (1 - config.TRAILING_STOP_PCT / 100.0):
            exit_day, exit_price, reason = d, p, "stop"
            break
    usd, pct = rules.pnl("long", entry, exit_price)
    return {"entry_date": entry_day, "exit_date": exit_day, "entry": entry,
            "exit": exit_price, "reason": reason, "usd": usd, "pct": pct,
            "week_last": week[-1], "year": entry_day.year}


def run(prices, use_stop: bool) -> List[dict]:
    trades = []
    for week in group_weeks(prices):
        t = trade_week(week, use_stop)
        if t:
            trades.append(t)
    return trades


def max_drawdown(values: List[float]) -> float:
    """Largest peak to trough fall of the cumulative sum, starting from 0."""
    cum = peak = worst = 0.0
    for v in values:
        cum += v
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def summarise(trades: List[dict]) -> dict:
    n = len(trades)
    usd = [t["usd"] for t in trades]
    pct = [t["pct"] for t in trades]
    wins = sum(1 for x in usd if x > 0)
    return {
        "n": n, "wins": wins, "win_pct": (100.0 * wins / n) if n else 0.0,
        "total_usd": sum(usd), "sum_pct": sum(pct),
        "avg_pct": (sum(pct) / n) if n else 0.0,
        "worst": min(pct) if pct else 0.0, "best": max(pct) if pct else 0.0,
        "max_dd": max_drawdown(usd),
    }


def luck_check(pct: List[float], seed: int = SEED, resamples: int = RESAMPLES) -> dict:
    n = len(pct)
    if n < 2:
        return {"se": 0.0, "t": 0.0, "lo": 0.0, "hi": 0.0}
    mean = sum(pct) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in pct) / (n - 1))
    se = sd / math.sqrt(n)
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(pct, k=n)) / n for _ in range(resamples))
    lo = means[int(0.025 * resamples)]
    hi = means[int(0.975 * resamples) - 1]
    return {"se": se, "t": (mean / se) if se else 0.0, "lo": lo, "hi": hi}


def buy_hold(prices) -> Tuple[float, float]:
    first, last = prices[0][1], prices[-1][1]
    return last - first, (last - first) / first * 100.0


def decomposition(prices, trades_nostop: List[dict]) -> dict:
    """Total change = gross inside the traded weeks + gaps between weeks.

    Walks the traded weeks in order: the in-week gross is last minus first, the
    gap is the move from one week's last price to the next week's first price.
    Weeks with one price contribute only to the gap part.
    """
    weeks = group_weeks(prices)
    gross = gap = 0.0
    prev_last = None
    for week in weeks:
        first, last = week[0][1], week[-1][1]
        if prev_last is not None:
            gap += first - prev_last
        gross += last - first
        prev_last = last
    return {"gross": gross, "gap": gap, "total": gross + gap,
            "weeks": len(weeks)}


def fmt_block(label: str, s: dict) -> str:
    return ("%-12s weeks %3d  winners %3d (%5.1f%%)  total $%+8.2f/bbl  "
            "sum %% %+8.2f  avg %% %+7.3f  worst %% %+7.2f  best %% %+7.2f"
            % (label, s["n"], s["wins"], s["win_pct"], s["total_usd"],
               s["sum_pct"], s["avg_pct"], s["worst"], s["best"]))


def live_check(prices) -> Optional[str]:
    """Daily-data Bot 5 over the live window versus scripts.replay.calendar_only."""
    if not os.path.exists(SIGNALS):
        return None
    start, end = date(2026, 9, 17), date(2026, 11, 22)
    window = [(d, p) for d, p in prices if start <= d <= end]
    if len(window) < 2:
        return "Sanity check: no daily prices inside 2026-09-17 to 2026-11-22 in the data file."
    t = run(window, use_stop=False)
    daily = sum(x["usd"] for x in t)
    live = None
    try:
        from scripts.replay import calendar_only
        with open(SIGNALS, newline="") as fh:
            rows = sorted(csv.DictReader(fh), key=lambda r: r.get("run_utc", ""))
        live = sum(x["pnl_usd"] for x in calendar_only(rows, use_stop=False))
    except Exception as exc:  # keep the report usable if the replay imports fail
        live_note = " (live replay unavailable: %s)" % type(exc).__name__
    else:
        live_note = ""
    live_txt = "%+.2f" % live if live is not None else "n/a"
    msg = ("Sanity check 2026-09-17 to 2026-11-22: daily-data Bot 5 made $%+.2f/bbl over %d "
           "weeks (last daily price %s); live logged rows via calendar_only made $%s/bbl "
           "(recorded earlier as +3.02 over 10 weeks)%s." % (daily, len(t), window[-1][0], live_txt, live_note))
    if live is not None:
        diff = abs(daily - live)
        verdict = ("same ballpark (within $1.00/bbl)" if diff <= 1.0 else
                   "same sign, but not the same ballpark" if daily * live > 0 else
                   "NOT in the same ballpark (opposite sign)")
        msg += " Verdict: %s; difference $%.2f. Exact match is not expected: daily spot prices versus hourly logged quotes." % (verdict, diff)
    return msg


def build_report(prices) -> str:
    out: List[str] = []
    w = out.append
    w("BOT 5 CALENDAR ONLY, daily Brent backtest")
    w("Data: %s, %d prices, %s to %s" % (DATA, len(prices), prices[0][0], prices[-1][0]))
    w("Rule: each ISO week buy at the first price, sell at the last price of the week; "
      "weeks with fewer than 2 prices skipped; spread $%.2f/bbl charged via rules.pnl; "
      "stop variant = %.1f%% trailing stop on daily prices." % (config.SPREAD_USD_PER_BBL, config.TRAILING_STOP_PCT))
    w("")
    variants = [("no stop", False), ("3% stop", True)]
    results = {name: run(prices, stop) for name, stop in variants}
    years = sorted({d.year for d, _ in prices})
    for y in years:
        w("== %d ==" % y)
        for name, _ in variants:
            w(fmt_block(name, summarise([t for t in results[name] if t["year"] == y])))
    w("")
    w("== Whole period ==")
    for name, _ in variants:
        s = summarise(results[name])
        w(fmt_block(name, s))
        w("%-12s max drawdown of cumulative $ P&L: $%.2f/bbl" % ("", s["max_dd"]))
    w("")
    bh_usd, bh_pct = buy_hold(prices)
    w("Buy and hold, same period: $%+.2f/bbl (%+.2f%%), %s %.2f to %s %.2f"
      % (bh_usd, bh_pct, prices[0][0], prices[0][1], prices[-1][0], prices[-1][1]))
    w("")
    w("Luck check on average weekly %% return, whole period (%d bootstrap resamples, seed %d)" % (RESAMPLES, SEED))
    for name, _ in variants:
        pct = [t["pct"] for t in results[name]]
        lc = luck_check(pct)
        avg = sum(pct) / len(pct)
        w("%-8s avg %+.4f%%  std error %.4f  t = %+.2f  95%% bootstrap interval [%+.4f%%, %+.4f%%]"
          % (name, avg, lc["se"], lc["t"], lc["lo"], lc["hi"]))
    w("")
    d = decomposition(prices, results["no stop"])
    w("Weekend plus Monday decomposition: buy and hold $%+.2f = calendar bot gross (before spread) $%+.2f "
      "+ last-price-to-next-first-price (weekend and Monday) $%+.2f"
      % (d["total"], d["gross"], d["gap"]))
    w("Note: the last week in the data (%s to %s) is incomplete, so its exit is the latest price, not a Friday."
      % (group_weeks(prices)[-1][0][0], prices[-1][0]))
    w("")
    chk = live_check(prices)
    if chk:
        w(chk)
    return "\n".join(out) + "\n"


def main() -> int:
    prices = load_prices()
    text = build_report(prices)
    with open(OUT, "w") as fh:
        fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
