# The bots

This file lists every bot in the race, the two cohorts, and how missing data is handled.

## The 16 bots

All bots read the same logged news scores and Brent prices. They are replayed over the logged rows, so none of them trades live. Bot 1 is the only bot that matches the live paper account.

Bot 1 enters long at a score of +6 or more and short at a score of −6 or less. It exits on a reversal, on a 3% trailing stop checked once per run, or at the Friday close. It is flat over the weekend.

| Bot | Key | What it does | Cohort |
| --- | --- | --- | --- |
| 1 | original | The base rules. | 1 |
| 2 | hold_longer | Bot 1, but it ignores a reversal. | 1 |
| 3 | lower_bar | Bot 1, but it enters at +4 and −4. | 1 |
| 4 | lower_bar_hold | Enters at +4 and −4 and ignores a reversal. | 1 |
| 5 | calendar | Reads no news. Buys at the start of each week and leaves at the Friday close. | 1 |
| 6 | opposite | Bot 1 with every score flipped. | 1 |
| 7 | wide_stop | Bot 1 with an 8% trailing stop. | 2 |
| 8 | vol_stop | Bot 1 with a stop of 3 times the 20 day volatility at entry. The width is at least 3% and at most 15%, and it is fixed for the trade. | 2 |
| 9 | short_hold | Bot 1, plus it closes at the first row at or after 24 hours from entry. | 2 |
| 10 | fade_24h | Bot 6 (opposite), plus the same 24 hour exit. | 2 |
| 11 | trend_filter | Bot 1, but it goes long only when price is above the 200 day average and short only when price is below it. | 2 |
| 12 | event_standaside | Bot 1, but it opens no new trade on a row flagged eia_window or opec_day. Exits still happen. | 2 |
| 13 | vol_size | Bot 1 trades, sized at 2 divided by the 20 day volatility at entry, kept between 0.25 and 2. | 2 |
| 14 | safeguards | Bots 8, 11, 12 and 13 together. | 2 |
| 15 | trend_bar | Bot 1, but the entry bar drops to +4 or −4 when the trend agrees. That means a long with price above the 200 day average, or a short with price below it. Otherwise the normal bar of +6 or −6 applies. | 2 |
| 16 | trend_only | Reads no news. Goes long above the 200 day average and short below it. Leaves when price crosses to the other side, or at the Friday close. | 2 |

Bots 1 to 6 were registered on 2026-11-22. Bots 7 to 14 were registered on 2026-11-25. Bots 15 and 16 were added on the same day, before the cohort 2 race started. None of them may be edited during the race.

There is also a reference line, "Bot 1 over the same window". It is not a bot. It is Bot 1's rules run over the same rows as cohort 2, so each new bot can be compared like for like.

## The two cohorts

Cohort 1 is bots 1 to 6. They race over every row logged under rules version v2, exactly as before.

Cohort 2 is bots 7 to 16 and the reference. They need the context columns, which only exist from the first row logged with a context_version. Judging them on older rows would be cheating, because they were registered after those rows were seen. So cohort 2 races only over v2 rows from the first row that has a context_version. Each bot starts flat there with $100.

Until that first row exists, cohort 2 shows "not started" with 0 trades.

Compare a cohort 2 bot with the reference, not with the cohort 1 table. The two cohorts cover different rows.

Bot 15 tests whether the trend should make the bot quicker to act on news. Bot 16 is a yardstick, so if Bot 11 beats Bot 1 you can see whether the news or the trend did the work.

Bot 16 gets no luck p, because it reads no scores. Equal to the average counts as no trend, so Bot 16 does not trade and Bot 15 uses the normal bar.

## How time exits, stops and sizes work

* Stops are checked at logged rows only, and fills are at the logged price.
* The 24 hour exit fires on the first row at or after 24 hours that has a price and is inside market hours. The Friday close, the weekend close, the stop and a reversal all come first.
* A sized trade records its size. Its profit and balance use the return times the size.
* A trade's best and worst moves are measured at logged rows between entry and exit, in percent of the entry price, before the spread.

## The missing input rule

This rule is the same for every bot.

* If a value is blank on a row, use the most recent non blank value from earlier rows in the same race.
* If there has never been one, skip that one feature for that decision. The bot then acts like Bot 1 for that feature.
* Each bot counts these skips in `missing_input_decisions`. Only entry decisions count. For Bot 15 a skip counts only when the score is between the two bars with enough confidence, so the average could have changed the outcome. For Bot 16 every row where it could have entered counts.
* A blank `event_flags` on a row that has a context_version means no event. It is not missing.
* A volatility of 0 or less is treated as missing.

## Luck

With 16 bots, one will lead by luck, and Bot 1 will often be that one. A lead means little until it repeats in a second period.

Each bot with at least 10 trades and 60 rows gets a luck p. It is the share of score shifts that match or beat the bot's real total. The shifts slide the scores round the clock, one offset at a time. Prices, the 200 day average, volatility and event flags stay where they are. The test is deterministic, so the same rows always give the same p.

## Where the numbers go

* `backup/leaderboard.json` and the `leaderboard` tab. Bots 1 to 6 keep their shape. Cohort 2 sits in a `cohort2` section, and the tab shows it as a block below the first table.
* `backup/bot_trades.json` and the `bot_trades` tab list every closed trade and open position for all 16 bots and the reference.
