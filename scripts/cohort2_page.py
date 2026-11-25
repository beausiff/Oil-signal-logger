"""The "Ten more bots" section of the Oil Bot Race page. Pure: rows in, an HTML string out.

Used by scripts.build_page. Styling reuses the page's own classes and tokens
(tablebox, note, lede, empty, trade cards), so light and dark come for free.
Bots 7 to 16 race only over the rows from the first one with a context_version,
and their balances come from leaderboard.standings(), so they already account
for position size.
"""

from __future__ import annotations

from typing import List

from scripts import leaderboard
from scripts.trade_story import trim
from src.outcomes import parse_utc

FIRST_NUMBER = 7
HEADING = "Ten more bots: news plus one extra rule"

# "{minus}" is swapped for the page's own MINUS constant when the text is used.
RULE_TEXT = {
    "wide_stop": "Same as Bot 1 but the stop loss is 8% instead of 3%.",
    "vol_stop": "Same as Bot 1 but the stop loss widens when oil is jumpy: 3 times the usual daily move, "
                "never under 3% or over 15%.",
    "short_hold": "Same as Bot 1 but gets out after 24 hours at the latest.",
    "fade_24h": "Does the reverse of Bot 1 and gets out after 24 hours. It bets that news spikes fade.",
    "trend_filter": "Same as Bot 1 but only buys when oil is above its 200 day average, and only sells short "
                    "when it is below.",
    "event_standaside": "Same as Bot 1 but opens no new trade around the weekly US oil stocks report or an "
                        "OPEC+ meeting.",
    "vol_size": "Same as Bot 1 but bets less when oil is jumpy and more when it is calm.",
    "safeguards": "Bots 8, 11, 12 and 13 together: wider stop, trend filter, event pause and smaller bets "
                  "when oil is jumpy.",
    "trend_bar": "Acts at +4 and {minus}4 when the 200 day trend agrees with the news, and needs +6 and "
                 "{minus}6 when it does not.",
    "trend_only": "Reads no news. Holds oil when it is above its 200 day average and sells short when it is "
                  "below. A yardstick for the trend bots.",
}
# Bots that read no news get no model quote on entry.
NO_NEWS = ("trend_only", "calendar")


def rule_text(key: str, minus: str) -> str:
    """Rule text for a bot key, or an empty line for a key that has none."""
    return RULE_TEXT.get(key, "").replace("{minus}", minus)


def _running(trades: List[dict]) -> List[float]:
    """Balance after each closed trade, compounding at the trade's size."""
    out, balance = [], leaderboard.STARTING_BALANCE
    for t in trades:
        balance *= 1.0 + leaderboard.sized_pct(t) / 100.0
        out.append(balance)
    return out


def _table(bp, c2: dict) -> str:
    esc = bp.esc
    started = c2["started"]
    if started:
        bots = leaderboard.ranked_cohort2(c2)
    else:
        bots = [{"label": b["label"], "trades": 0, "winners": 0,
                 "balance_now": leaderboard.STARTING_BALANCE, "open": None} for b in leaderboard.NEW_BOTS]
    head = ('<tr><th class="n">Rank</th><th>Bot</th><th class="n">Closed trades</th>'
            '<th class="n">Winners</th><th class="n">$100 is now</th><th class="n">Return</th>'
            '<th>Open position</th></tr>')
    body = []
    for rank, bot in enumerate(bots, 1):
        body.append('<tr%s><td class="n">%s</td><td class="bot">%s</td><td class="n">%d</td><td class="n">%d</td>'
                    '%s<td>%s</td></tr>'
                    % (' class="lead"' if started and rank == 1 and (bot["trades"] or bot["open"]) else "",
                       str(rank) if started else "", esc(bot["label"]), bot["trades"], bot["winners"],
                       bp.balance_cells(bot["balance_now"]), esc(bp.open_text(bot))))
    ref = c2["reference"]
    body.append('<tr class="bench"><td class="n"></td><td class="bot">Bot 1 over the same hours</td>'
                '<td class="n">%d</td><td class="n">%d</td>%s<td>%s</td></tr>'
                % (ref["trades"], ref["winners"], bp.balance_cells(ref["balance_now"]), esc(bp.open_text(ref))))
    bench = c2.get("benchmark")
    if bench or not started:
        cells = bp.balance_cells(bench.get("balance_now") if (started and bench) else None)
        body.append('<tr class="bench"><td class="n"></td><td class="bot">Just holding oil</td><td class="n"></td>'
                    '<td class="n"></td>%s<td>held the whole time</td></tr>' % cells)
    return ('<div class="tablebox"><table>\n<thead>%s</thead>\n<tbody>\n%s\n</tbody></table></div>'
            % (head, "\n".join(body)))


def _note(bp, c2: dict) -> str:
    text = ("With 16 bots racing, 1 will be ahead by luck, so a winner has to win a second 4 weeks "
            "before it counts.")
    missing = sum(b.get("missing_input_decisions", 0) for b in c2["bots"]) if c2["started"] else 0
    if missing > 0:
        text += (" %s made without trend or volatility data, and the bot used Bot 1's rule for those."
                 % ("1 decision was" if missing == 1 else "%d decisions in total were" % missing))
    return '<p class="note">%s</p>' % bp.esc(text)


def _cards(bp, c2: dict, rows: List[dict]) -> str:
    esc = bp.esc
    race = leaderboard.cohort2_rows(rows)
    by_run = {str(r.get("run_utc", "")).strip(): r for r in race}
    by_key = {b["key"]: b for b in c2["bots"]}
    out = ["<h3>Bot by bot</h3>"]
    for number, bot in enumerate(leaderboard.NEW_BOTS, FIRST_NUMBER):
        trades, _ = leaderboard.run_bot(bot, race)
        stat = by_key.get(bot["key"]) or {}
        balance_now = stat.get("balance_now", leaderboard.STARTING_BALANCE)
        winners = sum(1 for t in trades if t["pnl_usd"] > 0)
        record = "%s, %s, $100 is now %s" % (bp.plural(len(trades), "trade", "trades"),
                                                bp.plural(winners, "winner", "winners"),
                                                bp.money(balance_now, signed=False))
        if stat.get("open"):
            record += ", open: %s" % bp.open_text(stat)
        card = ['<article class="trade">\n<header><span class="idx">%d</span><h3 style="margin:0">%s</h3></header>'
                % (number, esc(bot["label"])),
                '<p class="rule">%s</p>' % esc(rule_text(bot["key"], bp.MINUS)),
                '<p class="rec">%s</p>' % esc(record)]
        if not trades:
            card.append('<p class="empty" style="margin-top:.8rem">No trades yet.</p>')
        else:
            card.append('<ol class="tl">')
            for t, after in zip(trades, _running(trades)):
                result = leaderboard.sized_pct(t)
                extra = ""
                if "size" in t:
                    extra += "<span>size %.2f</span>" % t["size"]
                if "stop_pct" in t:
                    extra += "<span>stop %.1f%%</span>" % t["stop_pct"]
                card.append(
                    '<li><div class="tr"><span class="side %s">%s</span>'
                    '<span>In %s on %s</span><span>Out %s on %s</span><span>%s</span>%s'
                    '<span class="res %s">%s</span><span>balance %s</span></div>'
                    % (esc(t["side"]), esc(t["side"].upper()),
                       bp.money(t["entry_price"], signed=False), esc(bp.when_text(t["entry_utc"])),
                       bp.money(t["exit_price"], signed=False), esc(bp.when_text(t["exit_utc"])),
                       esc(bp.REASON_TEXT.get(t["reason"], t["reason"])), extra,
                       bp.tone(result), esc(bp.pct(result)), esc(bp.money(after, signed=False))))
                if bot["key"] not in NO_NEWS:
                    entry_row = by_run.get(str(t["entry_utc"]).strip(), {})
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
    return "\n".join(out)


def section_html(rows: List[dict]) -> str:
    from scripts import build_page as bp
    c2 = leaderboard.standings(rows, with_luck=False)["cohort2"]
    out = ['<section id="newbots">\n<h2>%s</h2>' % bp.esc(HEADING)]
    if not c2["started"]:
        out.append('<p class="lede">These bots have not started. They will start at the first hour logged with '
                   'the new trend and volatility data. Every bot sits at $100 until then.</p>')
        out.append(_table(bp, c2))
        out.append(_note(bp, c2))
        out.append("</section>")
        return "\n".join(out)

    start = parse_utc(c2["start_utc"])
    started = ("%s %s UTC" % (bp.day_text(start), start.strftime("%H:%M"))) if start else "an unknown time"
    out.append('<p class="lede">Bots 7 to 16 were added on 25 Nov 2026. Each takes Bot 1 (or its opposite) and '
               'changes 1 thing, using the trend, volatility or the calendar. Bot 16 reads no news and is a '
               'yardstick. They started later than Bots 1 to 6, at %s, so compare them with "Bot 1 over the '
               'same hours" in this table, not with the scoreboard above.</p>' % bp.esc(started))
    out.append(_table(bp, c2))
    out.append(_note(bp, c2))
    out.append(_cards(bp, c2, rows))
    out.append("</section>")
    return "\n".join(out)
