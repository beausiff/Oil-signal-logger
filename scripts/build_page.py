"""Build the Oil Bot Race page as one self contained HTML fragment.

    python3 -m scripts.build_page [--csv backup/signals.csv] [--out backup/race_page.html]

Reads the signals backup (no credentials, no network), replays the six bots with
scripts.leaderboard and scripts.replay, and writes a page fragment. Every number
on the page is computed from the rows. Nothing depends on the clock: "updated"
is the last row's run_utc, so the same CSV always gives the same bytes.

A hidden --simulate-race N flag exists for previews and tests only. It relabels
the last N rows as race rows IN MEMORY and says SIMULATED in the eyebrow line.
The default output never relabels anything.
"""

from __future__ import annotations

import argparse
import csv
import html
import math
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from scripts import leaderboard, replay
from scripts.trade_story import trim
from src import rules
from src.outcomes import parse_utc, to_float

RACE_DAYS = 28
MINUS = "−"

RULE_TEXT = {
    "original": "Buys when the news score reaches +6, sells short at −6. Gets out if the score flips, "
                "if price moves 3% against its best level, or on Friday.",
    "hold_longer": "Same as Bot 1 but does not get out when the score flips.",
    "lower_bar": "Same as Bot 1 but acts at +4 and −4.",
    "lower_bar_hold": "Acts at +4 and −4 and does not get out when the score flips.",
    "calendar": "Reads no news. Buys at the start of each week and sells on Friday. "
                "The others need to beat this.",
    "opposite": "Does the reverse of Bot 1. If this one wins, the signal is backwards.",
}
REASON_TEXT = {"friday": "Friday close", "stop": "stop loss", "reversal": "score flipped"}

LEARNED = [
    ("Does the score predict where oil goes next?", "No link at 1, 4, 24 or 72 hours", "No edge found"),
    ("Was Bot 1's profit skill or luck?", "Random timing matched it 26% of the time", "Could be luck"),
    ("Does the model give the same score twice?", "Yes, 83% identical, 97% within one point",
     "It is consistent"),
    ("Does counting headlines predict big moves?", "Not once time of day is taken out", "No"),
    ("Is Bot 1 better than just buying Monday and selling Friday?", "No clear gap either way", "Not proven"),
    ("Does holding longer help?", "It did on this data, by a lot, but it was found after looking",
     "Being tested in the race"),
]

HOW_IT_RUNS = [
    ("When", "Runs every hour, triggered by cron-job.org"),
    ("News", "Reads every headline since the last run"),
    ("Scoring", "Scores with Claude, about $7 a month"),
    ("Prices", "Prices from OilPriceAPI"),
    ("Data", "All data in the Oil Signal Log Google Sheet, backed up weekly to GitHub"),
    ("Money", "No broker, no real money"),
]

CSS = """/* Layout: a full-bleed instrument panel up top, then sections in a single reading column. */
:root{
  --ink:#10211f; --paper:#f6f4ef; --panel:#fffdf8; --line:#d9d4c7;
  --muted:#6c7470; --accent:#b4621a; --win:#1f6b4a; --loss:#9e2b25;
  --grid:#e4dfd2;
  --display:"IBM Plex Sans",ui-sans-serif,system-ui,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ink:#e8e4da; --paper:#0f1513; --panel:#161d1b; --line:#2b3633;
  --muted:#8e9894; --accent:#e08a3c; --win:#4fb286; --loss:#d9635c; --grid:#232d2b;
  color-scheme:dark}}
:root[data-theme="dark"]{
  --ink:#e8e4da; --paper:#0f1513; --panel:#161d1b; --line:#2b3633;
  --muted:#8e9894; --accent:#e08a3c; --win:#4fb286; --loss:#d9635c; --grid:#232d2b;
  color-scheme:dark}

body{background:var(--paper);color:var(--ink);font-family:var(--display);line-height:1.55;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1060px;margin:0 auto;
  padding-left:max(16px,env(safe-area-inset-left,0px));
  padding-right:max(16px,env(safe-area-inset-right,0px));
  padding-block:2.6rem 4rem}
h1{font-size:clamp(1.9rem,5vw,3rem);line-height:1.08;font-weight:700;letter-spacing:-.025em;
  text-wrap:balance;margin:0 0 .5rem}
.standfirst{font-size:clamp(1rem,2.2vw,1.18rem);color:var(--muted);max-width:60ch;margin:0 0 2rem}
.eyebrow{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;
  color:var(--accent);margin:0 0 .7rem}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(138px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:3px;overflow:hidden;margin-bottom:2.4rem}
.stat{background:var(--panel);padding:.85rem 1rem;min-width:0}
.stat b{display:block;font-family:var(--mono);font-size:1.3rem;font-weight:600;
  font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.stat b.sm{font-size:1rem;line-height:1.3;overflow-wrap:anywhere}
.stat span{display:block;font-size:.72rem;color:var(--muted);margin-top:.15rem}

figure{margin:0 0 1rem}
.chartbox{overflow-x:auto;border:1px solid var(--line);border-radius:3px;background:var(--panel)}
svg{display:block;width:100%;min-width:620px;height:auto}
.grid{stroke:var(--grid);stroke-width:1}
.thr{stroke:var(--grid);stroke-width:1;stroke-dasharray:3 3}
.ax{fill:var(--muted);font-family:var(--mono);font-size:10px;text-anchor:end}
.am{text-anchor:middle}
.pl{fill:none;stroke:var(--muted);stroke-width:1.4;opacity:.55}
.ar{fill:var(--accent);opacity:.05}
.shut{fill:var(--muted);opacity:.1}
.seg{fill:none;stroke-width:3.4;stroke-linecap:round}
.seg.win{stroke:var(--win)} .seg.loss{stroke:var(--loss)}
.dot.win{fill:var(--win);stroke:var(--panel);stroke-width:1.6}
.dot.loss{fill:var(--loss);stroke:var(--panel);stroke-width:1.6}
.dot.hollow.win{fill:var(--panel);stroke:var(--win);stroke-width:2.4}
.dot.hollow.loss{fill:var(--panel);stroke:var(--loss);stroke-width:2.4}
.num{fill:var(--ink);font-family:var(--mono);font-size:11px;font-weight:600;text-anchor:middle}
.sl{fill:none;stroke:var(--accent);stroke-width:1.2;opacity:.75}
.plabel{fill:var(--muted);font-family:var(--mono);font-size:10px;letter-spacing:.1em}
figcaption{font-size:.78rem;color:var(--muted);margin-top:.6rem;max-width:68ch}

h2{font-size:1.45rem;font-weight:600;letter-spacing:-.015em;margin:3rem 0 .3rem}
h3{font-size:1.05rem;font-weight:600;margin:1.6rem 0 .3rem}
.lede{color:var(--muted);margin:0 0 1.4rem;max-width:62ch}

.trade{border:1px solid var(--line);border-radius:3px;background:var(--panel);
  padding:1.1rem 1.2rem;margin-bottom:1rem}
.trade header{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;margin-bottom:.9rem}
.idx{font-family:var(--mono);font-size:.78rem;font-weight:600;width:1.55rem;height:1.55rem;
  display:grid;place-items:center;border-radius:50%;background:var(--ink);color:var(--paper);flex:none}
.side{font-family:var(--mono);font-size:.7rem;font-weight:600;letter-spacing:.1em;
  padding:.16rem .5rem;border-radius:2px;border:1px solid currentColor}
.side.long{color:var(--win)} .side.short{color:var(--loss)}
.tag{font-size:.7rem;color:var(--muted)}
.pnl{margin-left:auto;font-family:var(--mono);font-weight:600;font-size:1.05rem;
  font-variant-numeric:tabular-nums}
.trade.win .pnl{color:var(--win)} .trade.loss .pnl{color:var(--loss)}

.legs{display:flex;align-items:flex-start;gap:.9rem;flex-wrap:wrap;
  padding-bottom:.95rem;border-bottom:1px solid var(--line);margin-bottom:.95rem}
.leg{min-width:0;display:flex;flex-direction:column}
.leg.held{margin-left:auto;text-align:right}
.lbl{font-family:var(--mono);font-size:.64rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.val{font-family:var(--mono);font-size:1.12rem;font-weight:600;font-variant-numeric:tabular-nums}
.sub{font-size:.73rem;color:var(--muted)}
.arrow{color:var(--muted);align-self:center;padding-top:.7rem}

.quote{border-left:2px solid var(--accent);padding-left:.85rem;margin-bottom:.75rem}
.quote.out{border-left-color:var(--line)}
.q{margin:0;font-size:.95rem}
.src{margin:.25rem 0 0;font-family:var(--mono);font-size:.68rem;letter-spacing:.06em;
  text-transform:uppercase;color:var(--muted)}
.heads{font-size:.76rem;color:var(--muted);margin:0 0 .85rem;padding-left:.9rem;max-width:72ch}

.verdict{border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:3px;
  background:var(--panel);padding:1.3rem 1.4rem;margin-top:2.6rem}
.verdict h2{margin-top:0}
.verdict p{max-width:64ch}
.verdict p:last-child{margin-bottom:0}
table{width:100%;border-collapse:collapse;font-size:.88rem;margin:.4rem 0 0}
th,td{text-align:left;padding:.5rem .6rem;border-bottom:1px solid var(--line)}
th{font-family:var(--mono);font-size:.66rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:500}
td.n,th.n{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
th.n{font-family:var(--mono)}
.tablebox{overflow-x:auto}
.tablebox table{min-width:560px}
td.bot{min-width:12rem}
.pos{color:var(--win)} .neg{color:var(--loss)} .zero{color:var(--ink)}
tr.bench td{color:var(--muted);font-style:italic}
tr.bench td.pos,tr.bench td.neg{font-style:normal}
tr.lead td:first-child{font-weight:600}
.note{font-size:.85rem;color:var(--muted);max-width:64ch;margin:.9rem 0 0}
.empty{color:var(--muted);margin:.2rem 0 0}

.rule{margin:0 0 .5rem;max-width:62ch}
.rec{font-family:var(--mono);font-size:.76rem;color:var(--muted);margin:0;font-variant-numeric:tabular-nums}
.tl{list-style:none;margin:.9rem 0 0;padding:0}
.tl li{border-top:1px solid var(--line);padding:.7rem 0 .2rem}
.tr{display:flex;flex-wrap:wrap;align-items:baseline;gap:.35rem .9rem;font-size:.82rem;
  font-variant-numeric:tabular-nums;margin-bottom:.5rem}
.tr .res{margin-left:auto;font-family:var(--mono);font-weight:600}
.tl .quote{margin:.1rem 0 .6rem}
.tl .q{font-size:.86rem}

.defs{margin:.8rem 0 0}
.defs div{display:flex;flex-wrap:wrap;gap:.1rem 1.2rem;padding:.6rem 0;border-bottom:1px solid var(--line)}
.defs dt{font-family:var(--mono);font-size:.68rem;letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted);min-width:5.5rem;padding-top:.2rem}
.defs dd{margin:0;flex:1 1 16rem;min-width:0}
footer{margin-top:2.6rem;padding-top:1.1rem;border-top:1px solid var(--line);
  font-size:.75rem;color:var(--muted)}
@media (max-width:460px){.stat:last-child:nth-child(odd){grid-column:1/-1}.leg.held{margin-left:0;text-align:left}.arrow{display:none}
  .tr .res{margin-left:0}.verdict{padding:1.1rem 1rem}}
"""


# ------------------------------------------------------------- formatting --

def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def money(value: float, signed: bool = True) -> str:
    rounded = round(value, 2)
    if not signed:
        return "$%.2f" % abs(rounded) if rounded >= 0 else MINUS + "$%.2f" % abs(rounded)
    sign = "+" if rounded > 0 else (MINUS if rounded < 0 else "")
    return "%s$%.2f" % (sign, abs(rounded))


def pct(value: float) -> str:
    rounded = round(value, 2)
    sign = "+" if rounded > 0 else (MINUS if rounded < 0 else "")
    return "%s%.2f%%" % (sign, abs(rounded))


def tone(value: Optional[float]) -> str:
    if value is None or round(value, 2) == 0:
        return "zero"
    return "pos" if value > 0 else "neg"


def score_text(value: Optional[float]) -> str:
    if value is None:
        return "none"
    n = int(value)
    return "+%d" % n if n > 0 else (MINUS + str(abs(n)) if n < 0 else "0")


def day_text(dt: datetime) -> str:
    return "%d %s %d" % (dt.day, dt.strftime("%b"), dt.year)


def when_text(value: str) -> str:
    dt = parse_utc(value)
    if dt is None:
        return "unknown time"
    return "%d %s, %s" % (dt.day, dt.strftime("%b"), dt.strftime("%H:%M"))


def plural(n: int, one: str, many: str) -> str:
    return "%d %s" % (n, one if n == 1 else many)


# ------------------------------------------------------------------ chart --

def _nice_step(span: float, target: int) -> float:
    for step in (0.5, 1, 2, 5, 10, 20, 50):
        if span / step <= target:
            return step
    return 100


def _time_ticks(t0: datetime, t1: datetime):
    """[(datetime, label)] sensible for the span: months, days or hours."""
    span = t1 - t0
    ticks = []
    if span > timedelta(days=40):
        cur = datetime(t0.year, t0.month, 1, tzinfo=timezone.utc)
        while cur <= t1:
            if cur > t0:
                ticks.append((cur, cur.strftime("%b")))
            cur = datetime(cur.year + (cur.month == 12), cur.month % 12 + 1, 1, tzinfo=timezone.utc)
        return ticks
    for step in (timedelta(hours=1), timedelta(hours=3), timedelta(hours=6), timedelta(hours=12),
                 timedelta(days=1), timedelta(days=2), timedelta(days=7)):
        if span / step <= 8:
            break
    epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
    cur = epoch + math.ceil((t0 - epoch) / step) * step
    while cur <= t1:
        if step >= timedelta(days=1):
            label = "%d %s" % (cur.day, cur.strftime("%b"))
        else:
            label = cur.strftime("%H:%M")
        ticks.append((cur, label))
        cur += step
    return ticks


def _shut_spans(t0: datetime, t1: datetime):
    """Stretches of 12 hours or more when the oil market is closed (weekends)."""
    spans, start = [], None
    cur = t0.replace(minute=0, second=0, microsecond=0)
    while cur <= t1 + timedelta(hours=1):
        shut = not rules.is_market_open(cur.astimezone(rules.CHICAGO))
        if shut and start is None:
            start = cur
        elif not shut and start is not None:
            if cur - start >= timedelta(hours=12):
                spans.append((start, cur))
            start = None
        cur += timedelta(hours=1)
    if start is not None and cur - start >= timedelta(hours=12):
        spans.append((start, cur))
    return spans


def chart_svg(rows: List[dict], trades: List[dict], aria: str, score_lim: float,
              thresholds, shade: bool, numbered: bool) -> str:
    """Price line on top, score track below. Geometry follows the original page."""
    pts = []
    for row in rows:
        when, price = parse_utc(row.get("run_utc", "")), to_float(row.get("brent_price"))
        if when is not None and price is not None:
            pts.append((when, price, to_float(row.get("score"))))
    if len(pts) < 2:
        return ""
    t0, t1 = pts[0][0], pts[-1][0]
    span = max((t1 - t0).total_seconds(), 1.0)
    prices = [p[1] for p in pts]
    step = _nice_step(max(prices) - min(prices), 6)
    lo = math.floor(min(prices) / step) * step
    hi = math.ceil(max(prices) / step) * step
    if hi <= lo:
        hi = lo + step

    W, H, L, R, TP, BT, SH = 1000.0, 300.0, 46.0, 14.0, 22.0, 24.0, 110.0

    def x(dt):
        return L + (dt - t0).total_seconds() / span * (W - L - R)

    def y(p):
        return TP + (hi - p) / (hi - lo) * (H - TP - BT)

    def sy(s):
        return H + 30 + (score_lim - s) / (2.0 * score_lim) * SH

    xy = [(x(t), y(p)) for t, p, _ in pts]
    line = "M" + " L".join("%.1f %.1f" % q for q in xy)
    area = line + " L%.1f %.1f L%.1f %.1f Z" % (xy[-1][0], y(lo), xy[0][0], y(lo))
    total_h = H + 30 + SH + 16

    out = ['<svg viewBox="0 0 %d %d" role="img" aria-label="%s">' % (W, total_h, esc(aria))]
    if shade:
        for a, b in _shut_spans(t0, t1):
            xa, xb = max(x(a), L), min(x(b), W - R)
            if xb > xa:
                out.append('<rect class="shut" x="%.1f" y="%.1f" width="%.1f" height="%.1f"/>'
                           % (xa, TP, xb - xa, total_h - 16 - TP))
    out.append('<path class="ar" d="%s"/>' % area)
    tick = lo
    while tick <= hi + 1e-9:
        label = ("%d" % tick) if float(tick).is_integer() else ("%.1f" % tick)
        out.append('<line class="grid" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                   '<text class="ax" x="%.1f" y="%.1f">%s</text>'
                   % (L, y(tick), W - R, y(tick), L - 8, y(tick) + 3.5, label))
        tick += step
    for dt, label in _time_ticks(t0, t1):
        out.append('<line class="grid" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                   '<text class="ax am" x="%.1f" y="%.1f">%s</text>'
                   % (x(dt), TP, x(dt), H - BT, x(dt), H - BT + 14, esc(label)))
    out.append('<path class="pl" d="%s"/>' % line)

    for i, t in enumerate(trades, 1):
        a, b = parse_utc(str(t["entry_utc"])), parse_utc(str(t["exit_utc"]))
        if a is None or b is None:
            continue
        inside = [(x(dt), y(p)) for dt, p, _ in pts if a <= dt <= b]
        if len(inside) < 2:
            inside = [(x(a), y(t["entry_price"])), (x(b), y(t["exit_price"]))]
        cls = "win" if t["pnl_usd"] > 0 else "loss"
        out.append('<path class="seg %s" d="%s"/>' % (cls, "M" + " L".join("%.1f %.1f" % q for q in inside)))
        out.append('<circle class="dot %s" cx="%.1f" cy="%.1f" r="4.5"/>' % (cls, x(a), y(t["entry_price"])))
        out.append('<circle class="dot hollow %s" cx="%.1f" cy="%.1f" r="4.5"/>' % (cls, x(b), y(t["exit_price"])))
        if numbered:
            out.append('<text class="num" x="%.1f" y="%.1f">%d</text>' % (x(a), y(t["entry_price"]) - 11, i))

    out.append('<text class="plabel" x="46" y="%.1f">NEWS SCORE</text>' % (H + 14))
    for value in thresholds:
        out.append('<line class="thr" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                   '<text class="ax" x="%.1f" y="%.1f">%s</text>'
                   % (L, sy(value), W - R, sy(value), L - 8, sy(value) + 3.5, score_text(value)))
    spts = [(x(t), sy(max(-score_lim, min(score_lim, s)))) for t, _, s in pts if s is not None]
    if len(spts) >= 2:
        out.append('<path class="sl" d="%s"/>' % ("M" + " L".join("%.1f %.1f" % q for q in spts)))
    out.append("</svg>")
    return "".join(out)


# --------------------------------------------------------------- sections --

def _race_context(rows: List[dict]):
    race = leaderboard.race_rows(rows)
    table = leaderboard.standings(rows, with_luck=False)
    return race, table


def _bot_by_key(table: dict) -> dict:
    return {b["key"]: b for b in table.get("bots", [])}


def open_text(bot: dict) -> str:
    o = bot.get("open")
    if not o:
        return "flat"
    text = "%s from %s" % (o["side"], money(o["entry_price"], signed=False) if o["entry_price"] is not None else "n/a")
    if o["unrealised_usd"] is not None:
        text += ", %s now" % money(o["unrealised_usd"])
    return text


def header_html(rows, race, table, simulated: int) -> str:
    last = parse_utc(rows[-1].get("run_utc", "")) if rows else None
    updated = ("%s %s" % (day_text(last), last.strftime("%H:%M"))) if last else "no data yet"
    eyebrow = "Paper trading · %supdated %s UTC" % (
        ("SIMULATED PREVIEW, not real results · " if simulated else ""), esc(updated))

    now_price = leaderboard.last_price(rows)
    if race:
        start = parse_utc(race[0]["run_utc"])
        last_race = parse_utc(race[-1]["run_utc"])
        day = (last_race.date() - start.date()).days + 1
        day_stat = ("%d of %d" % (min(day, RACE_DAYS), RACE_DAYS),
                    "race day, ends %s" % day_text(start + timedelta(days=RACE_DAYS)))
        bench = table["benchmark"]
        since = (pct(bench["pct"]), "Brent since the race began") if bench and bench["pct"] is not None \
            else ("n/a", "Brent since the race began")
        ranked = leaderboard.ranked(table)
        untouched = all(b["balance_now"] == leaderboard.STARTING_BALANCE and not b["open"] for b in ranked)
        leader = "no trades yet" if untouched else ranked[0]["label"]
    else:
        day_stat = ("Not started", "the race runs %d days" % RACE_DAYS)
        since = ("n/a", "Brent since the race began")
        leader = "no trades yet"

    stats = [
        ("<b>%s</b>" % esc(day_stat[0]), day_stat[1]),
        ("<b>%d</b>" % len(race), "hours logged in the race"),
        ("<b>%s</b>" % (money(now_price, signed=False) if now_price is not None else "n/a"), "Brent now"),
        ("<b>%s</b>" % esc(since[0]), since[1]),
        ('<b class="sm">%s</b>' % esc(leader), "leading bot"),
    ]
    cells = "".join('\n <div class="stat">%s<span>%s</span></div>' % (b, esc(s)) for b, s in stats)
    return (
        '<p class="eyebrow">%s</p>\n<h1>Oil bot race</h1>\n'
        '<p class="standfirst">Six bots read the same oil news scores and trade Brent on paper with '
        'different rules. This page shows who is ahead and why.</p>\n'
        '<div class="stats">%s\n</div>' % (eyebrow, cells)
    )


def _pnl_cell(value: float, text: str) -> str:
    return '<td class="n %s">%s</td>' % (tone(value), esc(text))


def ret_pct(balance: float) -> float:
    """Compounded return in percent of the starting balance."""
    return (balance / leaderboard.STARTING_BALANCE - 1.0) * 100.0


def balance_cells(balance: Optional[float], bold: bool = True) -> str:
    """Two cells: the balance (main number) and the compounded return, both coloured."""
    if balance is None:
        return '<td class="n zero">n/a</td><td class="n zero">n/a</td>'
    t = tone(balance - leaderboard.STARTING_BALANCE)
    text = money(balance, signed=False)
    return '<td class="n %s">%s</td><td class="n %s">%s</td>' % (
        t, ("<b>%s</b>" % esc(text)) if bold else esc(text), t, esc(pct(ret_pct(balance))))


def balances_after(trades: List[dict]) -> List[float]:
    """Running balance after each closed trade, compounding in closing order."""
    out, balance = [], leaderboard.STARTING_BALANCE
    for t in trades:
        balance *= 1.0 + t["pnl_pct"] / 100.0
        out.append(balance)
    return out


def scoreboard_html(table: dict, race) -> str:
    out = ['<section id="scoreboard">\n<h2>The scoreboard</h2>']
    started = table["started"]
    if started:
        bots = leaderboard.ranked(table)
    else:
        bots = [{"label": b["label"], "trades": 0, "winners": 0,
                 "balance_now": leaderboard.STARTING_BALANCE, "open": None} for b in leaderboard.BOTS]
        out.append('<p class="lede">The race has not started yet. Every bot sits at $100 until the first '
                   'race hour is logged.</p>')
    head = ('<tr><th class="n">Rank</th><th>Bot</th><th class="n">Closed trades</th>'
            '<th class="n">Winners</th><th class="n">$100 is now</th><th class="n">Return</th>'
            '<th>Open position</th></tr>')
    body = []
    for rank, bot in enumerate(bots, 1):
        body.append('<tr%s><td class="n">%s</td><td class="bot">%s</td><td class="n">%d</td><td class="n">%d</td>%s<td>%s</td></tr>'
                    % (' class="lead"' if started and rank == 1 and (bot["trades"] or bot["open"]) else "",
                       str(rank) if started else "", esc(bot["label"]), bot["trades"], bot["winners"],
                       balance_cells(bot["balance_now"]), esc(open_text(bot))))
    bench = table.get("benchmark") or {}
    bench_cells = balance_cells(bench.get("balance_now") if started else None)
    body.append('<tr class="bench"><td class="n"></td><td class="bot">Just holding oil</td><td class="n"></td>'
                '<td class="n"></td>%s<td>held the whole time</td></tr>' % bench_cells)
    out.append('<div class="tablebox"><table>\n<thead>%s</thead>\n<tbody>\n%s\n</tbody></table></div>'
               % (head, "\n".join(body)))
    out.append('<p class="note">Each bot started the race with $100 of pretend money and puts all of it into '
               'every trade, with no borrowing. With six bots, one will be ahead by luck, so a winner has to '
               'win a second four weeks before it counts.</p>\n</section>')
    return "\n".join(out)


def race_chart_html(race) -> str:
    priced = [r for r in race if to_float(r.get("brent_price")) is not None]
    out = ['<section id="race">\n<h2>The race so far</h2>']
    svg = chart_svg(race, [], "Brent crude price during the race, with the news score below",
                    score_lim=10, thresholds=(6, 4, 0, -4, -6), shade=True, numbered=False) \
        if len(priced) >= 2 else ""
    if not svg:
        out.append('<p class="empty">The chart appears once the race has a few hours of data.</p>')
    else:
        out.append(
            '<figure>\n<div class="chartbox">\n%s\n</div>\n<figcaption>The grey line is Brent in dollars a '
            'barrel, every hour the logger ran. Below it is the news score from −10 to +10. The dashed '
            'lines at +6 and −6 are where Bots 1 and 2 act. The lines at +4 and −4 are where Bots 3 '
            'and 4 act. Shaded bands are weekends, when the oil market is closed.</figcaption>\n</figure>' % svg)
    out.append("</section>")
    return "\n".join(out)


def _row_text(by_run: dict, utc: str) -> dict:
    return by_run.get(str(utc).strip(), {})


def bot_cards_html(race, table, by_run) -> str:
    out = ['<section id="bots">\n<h2>The six bots</h2>\n'
           '<p class="lede">Each bot follows its own rule. Trades below are race trades only. '
           'Times are UTC.</p>']
    by_key = _bot_by_key(table)
    for number, bot in enumerate(leaderboard.BOTS, 1):
        trades, position = leaderboard.run_bot(bot, race) if race else ([], rules.FLAT)
        stat = by_key.get(bot["key"])
        balance_now = stat["balance_now"] if stat else leaderboard.STARTING_BALANCE
        running = balances_after(trades)
        winners = sum(1 for t in trades if t["pnl_usd"] > 0)
        record = "%s, %s, $100 is now %s" % (plural(len(trades), "trade", "trades"),
                                                plural(winners, "winner", "winners"),
                                                money(balance_now, signed=False))
        if stat and stat["open"]:
            record += ", open: %s" % open_text(stat)
        card = ['<article class="trade">\n<header><span class="idx">%d</span><h3 style="margin:0">%s</h3></header>'
                % (number, esc(bot["label"])),
                '<p class="rule">%s</p>' % esc(RULE_TEXT[bot["key"]]),
                '<p class="rec">%s</p>' % esc(record)]
        if not trades:
            card.append('<p class="empty" style="margin-top:.8rem">No trades yet.</p>')
        else:
            card.append('<ol class="tl">')
            for t, after in zip(trades, running):
                entry_row = _row_text(by_run, t["entry_utc"])
                cls = tone(t["pnl_pct"])
                card.append(
                    '<li><div class="tr"><span class="side %s">%s</span>'
                    '<span>In %s on %s</span><span>Out %s on %s</span><span>%s</span>'
                    '<span class="res %s">%s</span><span>balance %s</span></div>'
                    % (esc(t["side"]), esc(t["side"].upper()),
                       money(t["entry_price"], signed=False), esc(when_text(t["entry_utc"])),
                       money(t["exit_price"], signed=False), esc(when_text(t["exit_utc"])),
                       esc(REASON_TEXT.get(t["reason"], t["reason"])), cls, esc(pct(t["pnl_pct"])),
                       esc(money(after, signed=False))))
                if bot["key"] != "calendar":
                    reasoning = trim(entry_row.get("reasoning"), 240)
                    src = "The model, on entry"
                    if bot.get("negate"):
                        src += " · this bot traded against this view"
                    if reasoning:
                        card.append('<div class="quote"><p class="q">“%s”</p><p class="src">%s</p></div>'
                                    % (esc(reasoning), esc(src)))
                card.append("</li>")
            card.append("</ol>")
        card.append("</article>")
        out.append("\n".join(card))
    out.append("</section>")
    return "\n".join(out)


def archive_html(archive, by_run) -> str:
    out = ['<section id="archive">\n<h2>Before the race: what the first ten weeks showed</h2>']
    if not archive:
        out.append('<p class="empty">There are no rows from before the race.</p>\n</section>')
        return "\n".join(out)

    first, last = parse_utc(archive[0]["run_utc"]), parse_utc(archive[-1]["run_utc"])
    out.append('<p class="lede">%s were logged from %s to %s, before the race rules were set. '
               'Everything in this part is the past.</p>'
               % (plural(len(archive), "hour", "hours"), esc(day_text(first)), esc(day_text(last))))

    relabelled = [dict(r, rules_version="v2") for r in archive]
    table = leaderboard.standings(relabelled, with_luck=False)
    out.append("<h3>What $100 would have become over the first ten weeks</h3>")
    head = ('<tr><th>Bot</th><th class="n">Closed trades</th><th class="n">Winners</th>'
            '<th class="n">$100 became</th><th class="n">Return</th></tr>')
    body = []
    for bot in table["bots"]:
        body.append('<tr><td class="bot">%s</td><td class="n">%d</td><td class="n">%d</td>%s</tr>'
                    % (esc(bot["label"]), bot["trades"], bot["winners"], balance_cells(bot["balance_now"])))
    bench = table.get("benchmark") or {}
    bc = balance_cells(bench.get("balance_now"))
    body.append('<tr class="bench"><td class="bot">Just holding oil</td><td class="n"></td><td class="n"></td>%s</tr>' % bc)
    out.append('<div class="tablebox"><table>\n<thead>%s</thead>\n<tbody>\n%s\n</tbody></table></div>'
               % (head, "\n".join(body)))
    out.append('<p class="note">Bots 2, 3 and 4 were designed by looking at this same data, so they were '
               'always going to look good here. The race is the real test.</p>')

    trades, _ = leaderboard.run_bot(leaderboard.BOTS[0], archive)
    out.append("<h3>Bot 1 on the first ten weeks</h3>")
    svg = chart_svg(archive, trades, "Brent crude price over the first ten weeks with Bot 1's trades marked, "
                    "and the news score below", score_lim=7, thresholds=(6, 0, -6), shade=False, numbered=True)
    scores = [to_float(r.get("score")) for r in archive]
    scores = [s for s in scores if s is not None]
    if svg:
        hit = sum(1 for s in scores if abs(s) >= 6)
        calm = sum(1 for s in scores if abs(s) <= 3)
        calm_pct = 100.0 * calm / len(scores) if scores else 0.0
        out.append('<figure>\n<div class="chartbox">\n%s\n</div>\n<figcaption>Brent in dollars a barrel, every hour '
                   'the logger ran. Solid colour marks the hours Bot 1 held a position, green for profit and red '
                   'for loss. A filled dot is an entry and a hollow one an exit. Below, the news score against '
                   'the +6 and −6 lines. It reached them on %d of the %d scored hours and spent %.0f%% of its '
                   'time between −3 and +3.</figcaption>\n</figure>'
                   % (svg, hit, len(scores), calm_pct))
    else:
        out.append('<p class="empty">There is not enough price data for a chart.</p>')

    if not trades:
        out.append('<p class="empty">Bot 1 would not have made a trade in this period.</p>')
    for i, (t, after) in enumerate(zip(trades, balances_after(trades)), 1):
        er, xr = _row_text(by_run, t["entry_utc"]), _row_text(by_run, t["exit_utc"])
        cls = "win" if t["pnl_usd"] > 0 else "loss"
        real = not str(er.get("score_source", "")).startswith("backfilled")
        es, xs = to_float(er.get("score")), to_float(xr.get("score"))
        hours = t.get("hours")
        held = ("%.0fh" % hours) if isinstance(hours, (int, float)) else "n/a"
        conf = str(er.get("confidence", "")).strip() or "not given"
        exit_note = "On exit · %s" % REASON_TEXT.get(t["reason"], t["reason"])
        if xs is not None:
            exit_note += " · score %s" % score_text(xs)
        out.append(
            '<article class="trade %s">\n<header>\n <span class="idx">%d</span>\n <span class="side %s">%s</span>\n'
            ' <span class="tag">%s</span>\n <span class="pnl">%s</span>\n</header>\n<div class="legs">\n'
            ' <div class="leg"><span class="lbl">In</span><span class="val">%s</span>'
            '<span class="sub">%s &middot; score %s</span></div>\n'
            ' <div class="arrow" aria-hidden="true">&rarr;</div>\n'
            ' <div class="leg"><span class="lbl">Out</span><span class="val">%s</span>'
            '<span class="sub">%s &middot; %s</span></div>\n'
            ' <div class="leg held"><span class="lbl">Held</span><span class="val">%s</span>'
            '<span class="sub">balance %s</span></div>\n</div>\n'
            '<div class="quote">\n <p class="q">“%s”</p>\n'
            ' <p class="src">The model, on entry · confidence %s</p>\n</div>\n'
            '<p class="heads">%s</p>\n'
            '<div class="quote out">\n <p class="q">“%s”</p>\n <p class="src">%s</p>\n</div>\n</article>'
            % (cls, i, esc(t["side"]), esc(t["side"].upper()),
               "Actually logged" if real else "Rescored after the fact", esc(pct(t["pnl_pct"])),
               money(t["entry_price"], signed=False), esc(when_text(t["entry_utc"])), esc(score_text(es)),
               money(t["exit_price"], signed=False), esc(when_text(t["exit_utc"])),
               esc(REASON_TEXT.get(t["reason"], t["reason"])), esc(held), esc(money(after, signed=False)),
               esc(trim(er.get("reasoning"))), esc(conf), esc(trim(er.get("key_headlines"), 260)),
               esc(trim(xr.get("reasoning"))), esc(exit_note)))

    rows_html = "".join('<tr><td>%s</td><td>%s</td><td>%s</td></tr>' % (esc(a), esc(b), esc(c))
                        for a, b, c in LEARNED)
    out.append('<div class="verdict">\n<h2>What we learned</h2>\n<div class="tablebox">\n<table>\n'
               '<thead><tr><th>Question</th><th>What we found</th><th>In plain words</th></tr></thead>\n'
               '<tbody>\n%s\n</tbody>\n</table>\n</div>\n'
               '<p style="margin-top:1rem">Checked on 22 November 2026 against every hour logged up to then.</p>\n'
               '</div>' % rows_html)
    out.append("</section>")
    return "\n".join(out)


def how_it_runs_html() -> str:
    items = "".join("<div><dt>%s</dt><dd>%s</dd></div>" % (esc(k), esc(v)) for k, v in HOW_IT_RUNS)
    return '<section id="how">\n<h2>How it runs</h2>\n<dl class="defs">%s</dl>\n</section>' % items


# ------------------------------------------------------------------ build --

def build(rows: List[dict], simulate_race: int = 0) -> str:
    rows = sorted((dict(r) for r in rows), key=lambda r: str(r.get("run_utc", "")))
    if simulate_race > 0:
        for row in rows[-simulate_race:]:
            row["rules_version"] = "v2"   # preview only, in memory
    race, table = _race_context(rows)
    archive = [r for r in rows if not str(r.get("rules_version", "")).strip().startswith("v2")]
    by_run = {str(r.get("run_utc", "")).strip(): r for r in rows}

    parts = [
        "<title>Oil Bot Race</title>",
        '<meta name="claude:viewport-fit-sides" content="cover">',
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600'
        '&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">',
        "<style>\n%s</style>" % CSS,
        '<div class="wrap">',
        header_html(rows, race, table, simulate_race),
        scoreboard_html(table, race),
        race_chart_html(race),
        bot_cards_html(race, table, by_run),
        archive_html(archive, by_run),
        how_it_runs_html(),
        "<footer>Simulated throughout. No broker is connected and no money is at risk.</footer>",
        "</div>",
    ]
    return "\n".join(parts) + "\n"


def summary(rows: List[dict], page: str, simulate_race: int = 0) -> str:
    rows = sorted(rows, key=lambda r: str(r.get("run_utc", "")))
    if simulate_race > 0:
        rows = [dict(r, rules_version="v2") if i >= len(rows) - simulate_race else r for i, r in enumerate(rows)]
    race = leaderboard.race_rows(rows)
    archive = [dict(r, rules_version="v2") for r in rows if not str(r.get("rules_version", "")).startswith("v2")]
    v1_trades = len(leaderboard.run_bot(leaderboard.BOTS[0], archive)[0]) if archive else 0
    return "%srace rows %d, bots %d, v1 trades %d, bytes %d" % (
        "SIMULATED " if simulate_race else "", len(race), len(leaderboard.BOTS), v1_trades,
        len(page.encode("utf-8")))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the Oil Bot Race page fragment.")
    parser.add_argument("--csv", default="backup/signals.csv")
    parser.add_argument("--out", default="backup/race_page.html")
    # Preview and testing only: relabels the last N rows as race rows, in memory.
    parser.add_argument("--simulate-race", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    with open(args.csv, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    page = build(rows, simulate_race=args.simulate_race)
    with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(page)
    print(summary(rows, page, args.simulate_race))
    return 0


if __name__ == "__main__":
    sys.exit(main())
