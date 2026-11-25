"""The "Feed B" section of the Oil Bot Race page. Pure: rows in, an HTML string out.

Used by scripts.build_page. Styling reuses the page's own classes and tokens
(tablebox, note, lede, empty, pos, neg), so light and dark come for free.
"""

from __future__ import annotations

from typing import List

from scripts import leaderboard, leaderboard_b
from src.outcomes import parse_utc

SENTENCE = ("The pairs, not the overall ranking, are the comparison that counts: each pair is the same rule on the "
            "same prices and hours, and only the news feed differs.")


def section_html(main_rows: List[dict], b_rows: List[dict]) -> str:
    from scripts import build_page as bp
    esc = bp.esc
    out = ['<section id="feedb">\n<h2>Feed B: same six bots on NewsAPI.ai</h2>']
    table = leaderboard_b.build(main_rows, b_rows)
    if not table["started"]:
        out.append('<p class="empty">Feed B has not logged a row yet. Once it has, the six twin bots and their '
                   'comparison with Bots 1 to 6 appear here.</p>\n</section>')
        return "\n".join(out)

    start = parse_utc(table["feed_b_start"])
    started = ("%s %s UTC" % (bp.day_text(start), start.strftime("%H:%M"))) if start else "unknown"
    out.append('<p class="lede">Twin bots 1B to 6B follow exactly the same rules as Bots 1 to 6, but read headlines '
               'from NewsAPI.ai instead of Google News. Feed B started later, at %s. %s</p>' % (esc(started), esc(SENTENCE)))

    p = table["paired"]
    by_key = {b["key"]: b for b in table["bots"]}
    rows = []
    for number, bot in enumerate(leaderboard.BOTS, 1):
        b = by_key[bot["key"]]
        rows.append('<tr><td class="bot">%s</td><td class="n">%d</td>%s</tr>' % (
            esc(b["label"]), b["trades"], bp.balance_cells(b["balance_now"])))
    out.append('<div class="tablebox"><table>\n<thead><tr><th>Feed B bot</th><th class="n">Closed trades</th>'
               '<th class="n">$100 is now</th><th class="n">Return</th></tr></thead>\n<tbody>\n%s\n</tbody></table></div>'
               % "\n".join(rows))

    out.append('<h3>Pairs over the common period</h3>')
    if not p["pairs"]:
        out.append('<p class="empty">No hour has been logged by both feeds yet.</p>')
    else:
        pair_rows = []
        for pair in p["pairs"]:
            diff = pair["diff"]
            pair_rows.append(
                '<tr><td class="bot">%d vs %dB</td><td class="n">%s</td><td class="n">%s</td>'
                '<td class="n %s">%s</td><td class="n">%d / %d</td></tr>' % (
                    pair["number"], pair["number"], esc(bp.money(pair["main_balance"], signed=False)),
                    esc(bp.money(pair["b_balance"], signed=False)), bp.tone(diff),
                    esc(bp.money(diff)), pair["main_trades"], pair["b_trades"]))
        out.append('<div class="tablebox"><table>\n<thead><tr><th>Pair</th><th class="n">Main $100 is now</th>'
                   '<th class="n">B $100 is now</th><th class="n">B minus main</th>'
                   '<th class="n">Trades main / B</th></tr></thead>\n<tbody>\n%s\n</tbody></table></div>'
                   % "\n".join(pair_rows))
        out.append('<p class="note">Over %d hours logged by both feeds, from %s to %s UTC. Both sides start '
                   'flat at the first of those hours.</p>' % (
                       p["common_rows"], esc(bp.when_text(p["common_start"])), esc(bp.when_text(p["common_end"]))))
    out.append("</section>")
    return "\n".join(out)
