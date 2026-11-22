"""Stress tests for the oil signal experiment's statistical findings.

    cd /home/user/oil-signal-logger && python3 scripts/stress.py

A one off robustness study run on 2026-11-22 against the 310 row backup. It
reads backup/signals.csv and backup/trade_story.json, needs no network and only
the standard library, uses seed 20261122 everywhere, and writes nothing. Its
numbers will drift as rows are added to the backup. The eight tests ask:

  1. Is the "calendar only" control valid, as shipped and when fixed?
  2. Does headline count predict the size of the move?
  3. Does the score predict the signed move?
  4. How do variants of the exit rules behave?
  5. How do variants of the entry threshold behave?
  6. Are the move cells contaminated (stale quotes, zero moves), and do the
     results survive on a clean sample?
  7. Is the strategy's result significant under a circular-shift null?
  8. Do scored longs beat the calendar control in the same week?
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
import sys
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import rules, config, stats  # noqa: E402
from src.outcomes import to_float, parse_utc  # noqa: E402
from src.rules import FLAT, Position  # noqa: E402
from scripts.replay import replay as replay_fn  # noqa: E402


def truthy(value):
    return str(value).strip().lower() in ("true", "1", "yes")


def calendar_only_as_shipped_before_fix(rows):
    """Verbatim copy of scripts.replay.calendar_only as it was before any fix."""
    position = FLAT
    trades = []
    seen_weeks = set()

    for row in rows:
        when = parse_utc(row.get("run_chicago", ""))
        if when is None:
            continue
        local = when.astimezone(rules.CHICAGO)
        price = to_float(row.get("brent_price"))
        if price is None or not rules.is_market_open(local):
            continue

        if position.is_open:
            position = rules.update_best_price(position, price)
            if rules.is_friday_close_run(local):
                usd, pct = rules.pnl("long", position.entry_price or 0.0, price)
                trades.append({"entry_utc": position.entry_utc, "entry_price": position.entry_price,
                               "exit_utc": str(row.get("run_utc", "")), "exit_price": price,
                               "pnl_usd": usd, "pnl_pct": pct})
                position = FLAT
            continue

        week = local.isocalendar()[:2]
        if week in seen_weeks or not rules.entries_allowed(local):
            continue
        seen_weeks.add(week)
        position = Position(side="long", entry_price=price, entry_utc=str(row.get("run_utc", "")),
                            entry_score=0, best_price=price, trade_id="CAL")

    return trades

SEED = 20261122
ROUNDS = 2000
HORIZONS = ("1h", "4h", "24h", "72h")
PLAIN = {}  # variant name -> plain shuffle p
P_VALUES = []  # every p value printed, as (label, p)


# ------------------------------------------------------------------ helpers
def reg(label, p):
    """Register a printed p value; returns its formatted string."""
    if p is None:
        return "n/a"
    P_VALUES.append((label, p))
    return "%.4f" % p


def fmt(v, spec="%.4f"):
    return "n/a" if v is None else spec % v


def header(t):
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


def sub(t):
    print()
    print("--- " + t)


def circ_shift_p(x, y, rounds=ROUNDS, seed=SEED):
    """Circular shift permutation p for Pearson r. Returns (r_obs, p) or (None, None)."""
    n = len(x)
    if n < 21 or len(y) != n:
        return None, None
    r_obs = stats.pearson(x, y)
    if r_obs is None:
        return None, None
    rng = random.Random(seed)
    cnt = 0
    for _ in range(rounds):
        k = rng.randint(10, n - 10)
        ys = y[k:] + y[:k]
        r = stats.pearson(x, ys)
        if r is not None and abs(r) >= abs(r_obs):
            cnt += 1
    return r_obs, (cnt + 1) / (rounds + 1)


def plain_p(x, y):
    res = stats.permutation_test(x, y, rounds=ROUNDS, seed=SEED)
    return None if res is None else res["p"]


def corr_report(label, x, y, tag):
    """n, r, plain p, circular p, registered."""
    n = len(x)
    r = stats.pearson(x, y)
    if r is None or n < 21:
        print("  %-34s n=%d  r=%s  (too few rows or undefined)" % (label, n, fmt(r)))
        return
    pp = plain_p(x, y)
    _, cp = circ_shift_p(x, y)
    print("  %-34s n=%-4d r=%+.4f  plain p=%s  circ-shift p=%s"
          % (label, n, r, reg(tag + " plain", pp), reg(tag + " circ", cp)))


def partial(rxy, rxv, ryv):
    if None in (rxy, rxv, ryv):
        return None
    d = (1 - rxv ** 2) * (1 - ryv ** 2)
    if d <= 0:
        return None
    return (rxy - rxv * ryv) / math.sqrt(d)


def resid_by_bucket(vals, buckets):
    sums, cnts = {}, {}
    for v, b in zip(vals, buckets):
        sums[b] = sums.get(b, 0.0) + v
        cnts[b] = cnts.get(b, 0) + 1
    return [v - sums[b] / cnts[b] for v, b in zip(vals, buckets)]


# --------------------------------------------------------------------- data
def load_rows():
    with open(os.path.join(ROOT, "backup", "signals.csv"), newline="") as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=lambda r: str(r.get("run_utc", "")))  # same as scripts.replay.main
    return rows


def build_records(rows):
    recs = []
    prev = None
    for r in rows:
        t = parse_utc(r.get("run_utc", ""))
        rec = {
            "t": t,
            "nh": to_float(r.get("new_headline_count")),
            "score": to_float(r.get("score")),
            "open": truthy(r.get("market_open")),
            "src": (r.get("score_source") or "").strip(),
            "hour": t.hour if t else None,
            "mv": {h: to_float(r.get("move_%s_pct" % h)) for h in HORIZONS},
            "v": None, "g": None,
            "pt": parse_utc(r.get("price_time_utc", "")),
        }
        if prev is not None:
            pm = prev["mv"]["1h"]
            rec["v"] = abs(pm) if pm is not None else None
            if t and prev["t"]:
                rec["g"] = (t - prev["t"]).total_seconds() / 60.0
        recs.append(rec)
        prev = rec
    return recs


# ------------------------------------------------------------------- TEST 1
def span_has_saturday(entry_utc, exit_utc):
    a = parse_utc(entry_utc).astimezone(rules.CHICAGO)
    b = parse_utc(exit_utc).astimezone(rules.CHICAGO)
    d = a.date()
    while d <= b.date():
        if d.weekday() == rules.SATURDAY:
            return True
        d += timedelta(days=1)
    return False


def hours_between(a, b):
    return (parse_utc(b) - parse_utc(a)).total_seconds() / 3600.0


def print_trades(trades, with_reason=False):
    for t in trades:
        print("    entry %-16s @%-7s exit %-16s @%-7s held %7.1fh  pnl $%+7.2f %+7.2f%%%s"
              % (str(t["entry_utc"])[:16], t["entry_price"], str(t["exit_utc"])[:16],
                 t["exit_price"], hours_between(t["entry_utc"], t["exit_utc"]),
                 t["pnl_usd"], t["pnl_pct"],
                 ("  [%s]" % t.get("reason")) if with_reason else ""))


def summarize(trades):
    n = len(trades)
    w = sum(1 for t in trades if t["pnl_usd"] > 0)
    return "trades=%d winners=%d total=$%+.2f/bbl summed=%+.2f%%" % (
        n, w, sum(t["pnl_usd"] for t in trades), sum(t["pnl_pct"] for t in trades))


def calendar_fixed(rows, use_stop):
    position = FLAT
    trades = []
    seen_weeks = set()
    for row in rows:
        when = parse_utc(row.get("run_chicago", ""))
        if when is None:
            continue
        local = when.astimezone(rules.CHICAGO)
        price = to_float(row.get("brent_price"))
        mo = rules.is_market_open(local)

        if position.is_open:
            reason = None
            if rules.is_weekend_shutdown(local):
                reason = "friday_late"
            elif mo:
                if price is not None:
                    position = rules.update_best_price(position, price)
                if rules.is_friday_close_run(local):
                    reason = "friday"
                elif use_stop and rules.trailing_stop_hit(position, price):
                    reason = "stop"
            if reason:
                exit_price = price if price is not None else position.best_price
                usd, pct = rules.pnl("long", position.entry_price or 0.0, exit_price or 0.0)
                trades.append({"entry_utc": position.entry_utc, "entry_price": position.entry_price,
                               "exit_utc": str(row.get("run_utc", "")), "exit_price": exit_price,
                               "pnl_usd": usd, "pnl_pct": pct, "reason": reason})
                position = FLAT
            continue

        if price is None or not mo:
            continue
        week = local.isocalendar()[:2]
        if week in seen_weeks or not rules.entries_allowed(local):
            continue
        seen_weeks.add(week)
        position = Position(side="long", entry_price=price, entry_utc=str(row.get("run_utc", "")),
                            entry_score=0, best_price=price, trade_id="CALF")
    return trades, position


def test1(rows):
    header("TEST 1: Is the 'calendar only' control valid?")
    sub("1(a) scripts.replay.calendar_only as shipped")
    cal = calendar_only_as_shipped_before_fix(rows)
    print_trades(cal)
    print("    %s" % summarize(cal))
    weekend = [t for t in cal if span_has_saturday(t["entry_utc"], t["exit_utc"])]
    if weekend:
        print("  VERDICT: YES. %d of %d trades were held across a weekend (a Saturday falls inside the holding span)."
              % (len(weekend), len(cal)))
        for t in weekend:
            print("    held over weekend: %s -> %s (%.1fh)" % (
                str(t["entry_utc"])[:16], str(t["exit_utc"])[:16], hours_between(t["entry_utc"], t["exit_utc"])))
        print("  The control therefore violates the experiment's flat-over-weekend rule; it skips closed rows, so a missed Friday close run carries the position to the next week.")
    else:
        print("  VERDICT: NO trade was held across a weekend.")

    sub("1(b) calendar_fixed (late weekend close like rules.decide)")
    fixed = {}
    for use_stop in (False, True):
        trades, pos = calendar_fixed(rows, use_stop)
        fixed[use_stop] = trades
        print("  use_stop=%s" % use_stop)
        print_trades(trades, with_reason=True)
        print("    %s" % summarize(trades))
        if pos.is_open:
            print("    still open at end: long from %s" % pos.entry_price)
        wk = [t for t in trades if span_has_saturday(t["entry_utc"], t["exit_utc"])]
        print("    trades spanning a Saturday: %d" % len(wk))

    sub("1(c) baseline scored strategy and buy-and-hold")
    base, _, _ = replay_fn(rows)
    with open(os.path.join(ROOT, "backup", "trade_story.json")) as f:
        story = json.load(f)["trades"]
    assert len(base) == len(story), "baseline replay trade count %d != story %d" % (len(base), len(story))
    for b, s in zip(base, story):
        assert b["side"] == s["side"], (b, s)
        assert str(b["entry_utc"])[:16] == s["entry_utc"][:16], (b, s)
        assert str(b["exit_utc"])[:16] == s["exit_utc"][:16], (b, s)
        assert abs(b["entry_price"] - s["entry_price"]) < 1e-9 and abs(b["exit_price"] - s["exit_price"]) < 1e-9, (b, s)
        assert abs(b["pnl_usd"] - s["pnl_usd"]) < 1e-6 and abs(b["pnl_pct"] - s["pnl_pct"]) < 1e-6, (b, s)
    print("  ASSERT PASSED: replay(rows) reproduces the %d trades in backup/trade_story.json" % len(story))
    print_trades(base, with_reason=True)
    print("    baseline scored: %s" % summarize(base))
    priced = [to_float(r.get("brent_price")) for r in rows]
    priced = [p for p in priced if p is not None]
    first, last = priced[0], priced[-1]
    print("    buy and hold: %.2f -> %.2f = $%+.2f/bbl = %+.2f%%" % (first, last, last - first, (last - first) / first * 100))
    print("    calendar_only (shipped): $%+.2f   calendar_fixed no stop: $%+.2f   calendar_fixed stop: $%+.2f"
          % (sum(t["pnl_usd"] for t in cal), sum(t["pnl_usd"] for t in fixed[False]),
             sum(t["pnl_usd"] for t in fixed[True])))


# ------------------------------------------------------------------- TEST 2
def pairs(recs, xf, yf):
    out = [r for r in recs if xf(r) is not None and yf(r) is not None]
    return out


def split_halves(sel, xf, yf):
    n = len(sel)
    h = n // 2
    res = []
    for part in (sel[:h], sel[h:]):
        res.append((len(part), stats.pearson([xf(r) for r in part], [yf(r) for r in part])))
    return res


def full_block(recs, xname, xf, h, tag, extras=False):
    yf = lambda r: abs(r["mv"][h]) if r["mv"][h] is not None else None  # noqa: E731
    sel = pairs(recs, xf, yf)
    x = [xf(r) for r in sel]
    y = [yf(r) for r in sel]
    print(" [%s vs |move_%s|]" % (xname, h))
    corr_report("(a) all rows", x, y, "%s %s" % (tag, h))
    # (c) time of day residualisation
    buckets = [r["hour"] // 4 for r in sel]
    if len(sel) >= 21:
        xr, yr = resid_by_bucket(x, buckets), resid_by_bucket(y, buckets)
        rr = stats.pearson(xr, yr)
        _, cp = circ_shift_p(xr, yr)
        print("  (c) time-of-day residualised       n=%-4d r=%s  circ-shift p=%s"
              % (len(sel), fmt(rr, "%+.4f"), reg("%s %s resid circ" % (tag, h), cp)))
    mo = [r for r in sel if r["open"]]
    print("  (c) market_open==TRUE only         n=%-4d r=%s" % (
        len(mo), fmt(stats.pearson([xf(r) for r in mo], [yf(r) for r in mo]), "%+.4f")))
    # (d), (e)
    if extras:
        d = [r for r in sel if r["v"] is not None]
        xd, yd, vd = [xf(r) for r in d], [yf(r) for r in d], [r["v"] for r in d]
        rxy, rxv, ryv = stats.pearson(xd, yd), stats.pearson(xd, vd), stats.pearson(yd, vd)
        print("  (d) lagged |move_1h| control       n=%-4d r_xy=%s r_xv=%s r_yv=%s partial r_xy.v=%s"
              % (len(d), fmt(rxy, "%+.4f"), fmt(rxv, "%+.4f"), fmt(ryv, "%+.4f"), fmt(partial(rxy, rxv, ryv), "%+.4f")))
        e = [r for r in sel if r["g"] is not None]
        xe, ye, ge = [xf(r) for r in e], [yf(r) for r in e], [r["g"] for r in e]
        rxy, rxg, ryg = stats.pearson(xe, ye), stats.pearson(xe, ge), stats.pearson(ye, ge)
        print("  (e) gap (minutes since prev row)   n=%-4d r_xy=%s r_xg=%s r_yg=%s partial r_xy.g=%s"
              % (len(e), fmt(rxy, "%+.4f"), fmt(rxg, "%+.4f"), fmt(ryg, "%+.4f"), fmt(partial(rxy, rxg, ryg), "%+.4f")))
    # (f) halves
    (n1, r1), (n2, r2) = split_halves(sel, xf, yf)
    print("  (f) split half by time             first half n=%d r=%s | second half n=%d r=%s"
          % (n1, fmt(r1, "%+.4f"), n2, fmt(r2, "%+.4f")))


def test2(recs):
    header("TEST 2: Does headline count predict the size of the move?")
    sub("2(b) caps and zeros of new_headline_count")
    have = [r for r in recs if r["nh"] is not None]
    cap = config.MAX_HEADLINES_PER_RUN
    print("  all rows with a count: n=%d  ==%d (cap): %.1f%% (%d)  ==0: %.1f%% (%d)" % (
        len(have), cap, 100.0 * sum(1 for r in have if r["nh"] == cap) / len(have), sum(1 for r in have if r["nh"] == cap),
        100.0 * sum(1 for r in have if r["nh"] == 0) / len(have), sum(1 for r in have if r["nh"] == 0)))
    for h in HORIZONS:
        sel = [r for r in have if r["mv"][h] is not None]
        if sel:
            print("  rows also having move_%-3s n=%-4d cap share %.1f%%  zero share %.1f%%" % (
                h, len(sel), 100.0 * sum(1 for r in sel if r["nh"] == cap) / len(sel),
                100.0 * sum(1 for r in sel if r["nh"] == 0) / len(sel)))
    xf = lambda r: r["nh"]  # noqa: E731
    for h in HORIZONS:
        sub("2 x=new_headline_count, horizon %s" % h)
        full_block(recs, "new_headline_count", xf, h, "T2 nh", extras=True)
    for h in HORIZONS:
        sub("2 x=score, horizon %s" % h)
        full_block(recs, "score", lambda r: r["score"], h, "T2 score")
    for h in HORIZONS:
        sub("2 x=abs(score), horizon %s" % h)
        full_block(recs, "abs(score)", lambda r: abs(r["score"]) if r["score"] is not None else None, h, "T2 |score|")


# ------------------------------------------------------------------- TEST 3
def test3(recs):
    header("TEST 3: Does the score predict the SIGNED move?")
    for h in HORIZONS:
        sub("horizon %s" % h)
        sel = [r for r in recs if r["score"] is not None and r["mv"][h] is not None]
        corr_report("all rows", [r["score"] for r in sel], [r["mv"][h] for r in sel], "T3 %s all" % h)
        mo = [r for r in sel if r["open"]]
        corr_report("market_open rows only", [r["score"] for r in mo], [r["mv"][h] for r in mo], "T3 %s open" % h)
        bf = [r for r in sel if r["src"].startswith("backfilled")]
        rest = [r for r in sel if not r["src"].startswith("backfilled")]
        for name, grp in (("backfilled", bf), ("not backfilled", rest)):
            print("  %-34s n=%-4d r=%s" % ("score_source " + name, len(grp), fmt(
                stats.pearson([r["score"] for r in grp], [r["mv"][h] for r in grp]), "%+.4f")))


# ------------------------------------------------------------------- TEST 4/5
_LOCAL = {}


def _local(row):
    key = row.get("run_chicago", "")
    if key not in _LOCAL:
        when = parse_utc(key)
        _LOCAL[key] = None if when is None else when.astimezone(rules.CHICAGO)
    return _LOCAL[key]


def replay_variant(rows, entry=6, use_reversal=True, use_stop=True, stop_pct=3.0):
    """Mirror of scripts.replay.replay + rules.decide, with switches."""
    position = FLAT
    trades = []
    band = stop_pct / 100.0

    for row in rows:
        local = _local(row)
        if local is None:
            continue
        raw_score = to_float(row.get("score"))
        score = int(raw_score) if raw_score is not None else None
        confidence = (row.get("confidence") or "").strip() or None
        price = to_float(row.get("brent_price"))
        run_utc = str(row.get("run_utc", ""))

        before = position
        closed_reason = None
        market_open = rules.is_market_open(local)

        if not market_open:
            if position.is_open and rules.is_weekend_shutdown(local):
                closed_reason = "friday"
                position = FLAT
        else:
            position = rules.update_best_price(position, price)
            if position.is_open:
                if rules.is_friday_close_run(local):
                    closed_reason = "friday"
                elif use_stop and price is not None and position.best_price is not None and (
                        (position.side == "long" and price <= position.best_price * (1 - band)) or
                        (position.side == "short" and price >= position.best_price * (1 + band))):
                    closed_reason = "stop"
                elif use_reversal and rules.reversal_exit(position, score):
                    closed_reason = "reversal"
                if closed_reason:
                    position = FLAT
            else:
                if score is not None and confidence in config.ENTRY_CONFIDENCES and price is not None:
                    wants_long = score >= entry
                    wants_short = score <= -entry
                    if (wants_long or wants_short) and rules.entries_allowed(local):
                        side = "long" if wants_long else "short"
                        position = Position(
                            side=side, entry_price=price, entry_utc=run_utc, entry_score=score,
                            best_price=price,
                            trade_id="R-%s-%s" % (run_utc[:16], side[0].upper()))

        if closed_reason and before.is_open:
            exit_price = price if price is not None else before.best_price
            usd, pct = rules.pnl(before.side, before.entry_price or 0.0, exit_price or 0.0)
            trades.append({"side": before.side, "entry_utc": before.entry_utc,
                           "entry_price": before.entry_price, "exit_utc": run_utc,
                           "exit_price": exit_price, "reason": closed_reason,
                           "pnl_usd": usd, "pnl_pct": pct})
    return trades, position


def shuffled_variant_totals(rows, rounds=1000, **kw):
    pairs_in = [(r.get("score", ""), r.get("confidence", "")) for r in rows]
    rng = random.Random(SEED)
    scratch = [dict(r) for r in rows]
    totals = []
    for _ in range(rounds):
        rng.shuffle(pairs_in)
        for row, (s, c) in zip(scratch, pairs_in):
            row["score"], row["confidence"] = s, c
        trades, _ = replay_variant(scratch, **kw)
        totals.append(sum(t["pnl_usd"] for t in trades))
    return totals


def variant_line(rows, name, **kw):
    trades, pos = replay_variant(rows, **kw)
    total = sum(t["pnl_usd"] for t in trades)
    totals = shuffled_variant_totals(rows, **kw)
    beaten = sum(1 for t in totals if t >= total)
    p = (beaten + 1) / (len(totals) + 1)
    PLAIN[name] = p
    ts = sorted(totals)
    wins = sum(1 for t in trades if t["pnl_usd"] > 0)
    print("  %-22s trades=%-3d winners=%-3d total=$%+7.2f/bbl summed=%+7.2f%%  shuffled p=%s  (beaten by %d/1000)"
          % (name, len(trades), wins, total, sum(t["pnl_pct"] for t in trades), reg("T4/5 " + name, p), beaten))
    print("  %-22s shuffled median $%+.2f   5th $%+.2f   95th $%+.2f%s"
          % ("", ts[len(ts) // 2], ts[int(0.05 * len(ts))], ts[int(0.95 * len(ts))],
             "   (open at end: %s)" % pos.side if pos.is_open else ""))
    return trades


def test4(rows):
    header("TEST 4: Exit rule variants")
    a, _, _ = replay_fn(rows)
    b, _ = replay_variant(rows)
    key = lambda ts: [(t["entry_utc"], t["exit_utc"], t["side"], t["pnl_usd"]) for t in ts]  # noqa: E731
    assert key(a) == key(b), "replay_variant defaults do not reproduce replay:\n%s\n%s" % (key(a), key(b))
    print("  ASSERT PASSED: replay_variant(defaults) reproduces scripts.replay.replay exactly (%d trades; no fixes needed to my copy)" % len(a))
    for name, kw in (
        ("baseline", {}),
        ("no reversal exit", {"use_reversal": False}),
        ("no trailing stop", {"use_stop": False}),
        ("neither (calendar only)", {"use_reversal": False, "use_stop": False}),
        ("stop 5%", {"stop_pct": 5.0}),
        ("stop 2%", {"stop_pct": 2.0}),
    ):
        trades = variant_line(rows, name, **kw)
        if name in ("baseline", "no reversal exit", "no trailing stop"):
            for t in trades:
                print("      %-5s %-16s -> %-16s %-9s $%+.2f" % (
                    t["side"], t["entry_utc"][:16], t["exit_utc"][:16], t["reason"], t["pnl_usd"]))


def test5(rows):
    header("TEST 5: Entry threshold variants (baseline exits)")
    for e in (4, 5, 6, 7):
        trades = variant_line(rows, "entry +/-%d" % e, entry=e)
        for t in trades:
            print("      %-5s %-16s -> %-16s %-9s $%+.2f" % (
                t["side"], t["entry_utc"][:16], t["exit_utc"][:16], t["reason"], t["pnl_usd"]))


# ------------------------------------------------------------------- TEST 6
def test6(recs):
    header("TEST 6: Contamination of move cells; do results survive on a clean sample?")
    sub("6(a) stale quotes and zero moves")
    half = timedelta(minutes=30)
    for label, flag in (("market_open TRUE", True), ("market_open FALSE", False)):
        grp = [r for r in recs if r["open"] == flag]
        blank = [r for r in grp if r["pt"] is None]
        stale = [r for r in grp if r["pt"] is not None and (r["t"] - r["pt"]) > half]
        print("  %-18s rows=%-4d price_time stale(>30min)=%-4d blank price_time=%-4d" % (
            label, len(grp), len(stale), len(blank)))
    for h in HORIZONS:
        for label, flag in (("market_open TRUE", True), ("market_open FALSE", False)):
            cells = [r["mv"][h] for r in recs if r["open"] == flag and r["mv"][h] is not None]
            z = sum(1 for c in cells if c == 0.0)
            print("  move_%-3s %-18s non-blank=%-4d exactly 0.0: %-3d (%.1f%%)" % (
                h, label, len(cells), z, (100.0 * z / len(cells)) if cells else 0.0))

    sub("6(b) clean sample sizes")
    clean = {}
    for h in HORIZONS:
        c = []
        for r in recs:
            if r["t"] is None or r["pt"] is None:
                continue
            if not rules.is_market_open(r["t"].astimezone(rules.CHICAGO)):
                continue
            if (r["t"] - r["pt"]) > half:
                continue
            if not rules.is_market_open((r["t"] + timedelta(hours=int(h[:-1]))).astimezone(rules.CHICAGO)):
                continue
            c.append(r)
        clean[h] = c
        have = [r for r in c if r["mv"][h] is not None]
        print("  horizon %-3s clean rows=%-4d (with non-blank move_%s: %d)" % (h, len(c), h, len(have)))

    sub("6(c) clean-sample correlations")
    for h in HORIZONS:
        c = [r for r in clean[h] if r["mv"][h] is not None]
        print(" [horizon %s, clean n=%d]" % (h, len(c)))
        absm = [abs(r["mv"][h]) for r in c]
        sg = [r["mv"][h] for r in c]
        sel = [r for r in c if r["nh"] is not None]
        corr_report("new_headline_count vs |move|", [r["nh"] for r in c if r["nh"] is not None],
                    [abs(r["mv"][h]) for r in sel], "T6 nh %s" % h)
        sel2 = [r for r in c if r["score"] is not None]
        corr_report("score vs |move|", [r["score"] for r in sel2], [abs(r["mv"][h]) for r in sel2], "T6 score %s" % h)
        corr_report("abs(score) vs |move|", [abs(r["score"]) for r in sel2], [abs(r["mv"][h]) for r in sel2], "T6 |score| %s" % h)
        corr_report("score vs signed move", [r["score"] for r in sel2], [r["mv"][h] for r in sel2], "T6 signed %s" % h)

    sub("6(d) clean sample, headline count vs |move|, time-of-day residualised")
    for h in HORIZONS:
        sel = [r for r in clean[h] if r["mv"][h] is not None and r["nh"] is not None]
        x = [r["nh"] for r in sel]
        y = [abs(r["mv"][h]) for r in sel]
        if len(sel) < 21:
            print("  %-4s n=%d too few rows" % (h, len(sel)))
            continue
        b = [r["hour"] // 4 for r in sel]
        xr, yr = resid_by_bucket(x, b), resid_by_bucket(y, b)
        _, cp = circ_shift_p(xr, yr)
        print("  %-4s n=%-4d resid r=%s  circ-shift p=%s" % (
            h, len(sel), fmt(stats.pearson(xr, yr), "%+.4f"), reg("T6 nh resid %s" % h, cp)))


# ------------------------------------------------------------------- TEST 7
def circ_shifted_totals(rows, **kw):
    n = len(rows)
    pairs_in = [(r.get("score", ""), r.get("confidence", "")) for r in rows]
    scratch = [dict(r) for r in rows]
    totals = []
    for k in range(10, n - 10 + 1):
        rot = pairs_in[k:] + pairs_in[:k]
        for row, (s, c) in zip(scratch, rot):
            row["score"], row["confidence"] = s, c
        trades, _ = replay_variant(scratch, **kw)
        totals.append(sum(t["pnl_usd"] for t in trades))
    return totals


def test7(rows):
    header("TEST 7: Strategy significance under a circular-shift null (score persistence kept)")
    print("  offsets k = 10 .. n-10 inclusive, exhaustive; p = (count shifted >= real + 1) / (offsets + 1)")
    for name, kw in (
        ("baseline", {}),
        ("no reversal exit", {"use_reversal": False}),
        ("neither (calendar only)", {"use_reversal": False, "use_stop": False}),
        ("entry +/-4", {"entry": 4}),
        ("entry +/-5", {"entry": 5}),
    ):
        trades, _ = replay_variant(rows, **kw)
        real = sum(t["pnl_usd"] for t in trades)
        totals = circ_shifted_totals(rows, **kw)
        beaten = sum(1 for t in totals if t >= real)
        p = (beaten + 1) / (len(totals) + 1)
        ts = sorted(totals)
        print("  %-22s real $%+7.2f  shifted median $%+.2f  5th $%+.2f  95th $%+.2f  beaten by %d/%d  circ p=%s  | plain shuffle p=%.4f"
              % (name, real, ts[len(ts) // 2], ts[int(0.05 * len(ts))], ts[int(0.95 * len(ts))],
                 beaten, len(totals), reg("T7 circ " + name, p), PLAIN.get(name, float("nan"))))


# ------------------------------------------------------------------- TEST 8
def test8(rows):
    header("TEST 8: Scored longs vs calendar_fixed (long Monday, flat by Friday) in the same week")
    base, _, _ = replay_fn(rows)
    cal, _ = calendar_fixed(rows, False)
    byweek = {parse_utc(t["entry_utc"]).astimezone(rules.CHICAGO).isocalendar()[:2]: t for t in cal}
    print("  %-34s | %-34s | %s" % ("scored long (entry -> exit, $/bbl)", "calendar_fixed same week", "scored minus calendar"))
    tot_s = tot_c = 0.0
    for t in base:
        if t["side"] != "long":
            continue
        wk = parse_utc(t["entry_utc"]).astimezone(rules.CHICAGO).isocalendar()[:2]
        c = byweek.get(wk)
        tot_s += t["pnl_usd"]
        if c is None:
            print("  %s -> %s $%+.2f | no calendar trade that week" % (t["entry_utc"][:16], t["exit_utc"][:16], t["pnl_usd"]))
            continue
        tot_c += c["pnl_usd"]
        print("  %s -> %s $%+6.2f | %s -> %s $%+6.2f | $%+.2f" % (
            t["entry_utc"][:16], t["exit_utc"][:16], t["pnl_usd"],
            c["entry_utc"][:16], c["exit_utc"][:16], c["pnl_usd"], t["pnl_usd"] - c["pnl_usd"]))
    print("  totals: scored longs $%+.2f   calendar same weeks $%+.2f   difference $%+.2f" % (tot_s, tot_c, tot_s - tot_c))


def main():
    rows = load_rows()
    recs = build_records(rows)
    print("oil-signal-logger stress tests   seed=%d   rows=%d   %s .. %s" % (
        SEED, len(rows), rows[0]["run_utc"][:16], rows[-1]["run_utc"][:16]))
    test1(rows)
    test2(recs)
    test3(recs)
    test4(rows)
    test5(rows)
    test6(recs)
    test7(rows)
    test8(rows)

    header("MULTIPLE COMPARISONS")
    n = len(P_VALUES)
    below = [(l, p) for l, p in P_VALUES if p < 0.05]
    print("  p values printed: %d" % n)
    print("  below 0.05: %d" % len(below))
    print("  expected below 0.05 by chance alone (0.05 x count): %.1f" % (0.05 * n))
    for l, p in sorted(below, key=lambda z: z[1]):
        print("    %.4f  %s" % (p, l))
    print("  note: plain-shuffle and circular-shift p values for the same pair are highly dependent, so the effective number of independent tests is smaller than the count.")


if __name__ == "__main__":
    main()
