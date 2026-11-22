# Oil News Signal Logger

A four week **log only** experiment. On each run it reads oil related news, scores
it with Claude, records a buy / sell / hold / flat decision against locked rules,
and logs the Brent price. Everything lands in a Google Sheet.

**There is no real money trading.** Positions are paper rows in a spreadsheet.
They can optionally be mirrored onto an IG demo account with fake money (see
[IG demo mirror](#ig-demo-mirror)), off by default. The point is to find out, after four weeks, whether the signals
had any predictive value against what the price actually did.

---

## What it does each run

The job is scheduled hourly, but GitHub only delivers roughly 3 to 4 scheduled
runs a day for this repository, so expect a few rows a day, not 24.

1. Fetches oil news published since the previous run, plus a 10 minute margin
   (at least 90 minutes, at most 12 hours; Google News RSS, free).
2. Cleans and dedupes it against the last 1,000 logged headlines. Caps at 40.
3. Fetches the Brent price. A closed market or a failed call logs a blank and carries on.
4. Loads the last 24 scores and the current paper position.
5. Scores the news with Claude and validates the JSON strictly. One retry, then logs `error`.
6. Applies the locked rules and updates the paper position.
7. Writes to `headlines`, `signals`, and `trades` when a trade closes.
8. Backfills forward price moves onto older rows.
9. Aggregates weekend news into a `weekend_gaps` row.

---

## Setup, step by step

You do not need to be a developer. Work through this in order.

### 1. Create the Google Sheet

1. Go to [sheets.new](https://sheets.new) and name the sheet `Oil Signal Log`.
2. Look at the address bar. The long string between `/d/` and `/edit` is the
   **Sheet ID**. Copy it somewhere. It looks like `1AbC...xYz`.

### 2. Create a Google service account

This is a robot Google account that writes to your sheet.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a
   new project called `oil-signal-logger`.
2. In the search bar, find **Google Sheets API** and click **Enable**.
3. Search for **Google Drive API** and click **Enable** as well.
4. Go to **APIs and Services → Credentials → Create Credentials → Service account**.
   Name it `oil-logger`. Click through the optional steps and create it.
5. Click the new service account, open the **Keys** tab, then
   **Add Key → Create new key → JSON**. A `.json` file downloads. Keep it safe.
   This file is a password. Never commit it.
6. Open that file in a text editor. Find the `client_email` line. It looks like
   `oil-logger@oil-signal-logger.iam.gserviceaccount.com`.

### 3. Share the sheet with the robot

Open your Google Sheet, click **Share**, paste the `client_email` from the last
step, set it to **Editor**, and send. Without this the robot cannot write.

### 4. Get the API keys

- **Anthropic:** [console.anthropic.com](https://console.anthropic.com) → API Keys → Create Key.
- **OilPriceAPI:** [oilpriceapi.com](https://www.oilpriceapi.com) → sign up → copy your key.
- **GNews (optional second news source):** [gnews.io](https://gnews.io) → free key.

### 5. Add the secrets to GitHub

In this repository: **Settings → Secrets and variables → Actions → New repository secret**.
Add each of these:

| Secret | Value |
| --- | --- |
| `ANTHROPIC` | your Anthropic key |
| `OILTRACKER` | your OilPriceAPI key |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the **entire contents** of the JSON file from step 2, pasted in whole |
| `SHEET_ID` | the Sheet ID from step 1 |
| `ANTHROPIC_WORKSPACE_ID` | only if the Anthropic key is organisation scoped, see below |
| `GNEWS_API_KEY` | optional, leave out if you skipped it |

**Workspace scoping.** An Anthropic key created at organisation level must name
a workspace on every request, and the API rejects it otherwise. Two ways round
it: create the key inside a workspace in the Console, which needs no extra
secret, or keep the organisation key and add `ANTHROPIC_WORKSPACE_ID`. A
workspace scoped key is preferable because the Console then reports this
experiment's spend on its own line.

The first two secrets are mapped to the `ANTHROPIC_API_KEY` and
`OILPRICE_API_KEY` environment variables in `.github/workflows/hourly.yml`.
The code only ever reads the environment variable names.

Nothing is hardcoded. Nothing is printed to the logs.

### 6. Create the sheet tabs

Go to the **Actions** tab, pick **setup sheet**, and click **Run workflow**.

This creates `signals`, `trades`, `headlines`, `state` and `weekend_gaps` with the
right headers. It is safe to run again. No terminal needed.

### 7. Switch it on

In the **Actions** tab, pick **hourly** and **Run workflow** with *dry run* ticked.
Read the log: it prints every row it would write and writes nothing. Then run it
again with dry run unticked and check the `signals` tab.

Once a correct row appears, the schedule is already live and needs nothing
further from you.

**About the schedule.** The cron is hourly, at 23 minutes past rather than on
the hour. GitHub delays or drops scheduled runs, and in practice delivers only
about 3 to 4 a day for this repository. Firing every 10 minutes was tried and
made no measurable difference. Each `signals` row stands on its own, and the
outcome columns are filled from the price API when no logged price falls within
20 minutes of the target time. Gaps cost data, not correctness. An external
scheduler is the only real fix, see `scheduler/README.md`.

Because the schedule is unreliable, the forward price columns do **not** depend
on it. Each run fills them from the prices it logged itself where it can, and
looks up anything still missing from the price API in narrow windows around
each outstanding target. A dropped run therefore costs one scored run, not the
outcome data around it.

The windows are narrow on purpose: the range endpoint caps a page at 100 points
and ignores a larger `per_page`, so one wide sweep comes back truncated without
saying so. The log flags any response that arrives at the cap.

The weekend row is anchored the same way. `friday_last_price` and
`friday_last_score` come from the last signals row before the Friday close, not
from whichever run happened to land in the final hour, because that run is
often dropped. Without the anchor, `gap_pct` and `weekend_signal_pnl_pct`
cannot be computed at all.

`sunday_reopen_price` and `monday_12utc_price` are pinned to their timestamps
for the same reason. A column named after a moment should hold the price at
that moment, not the price the first surviving run happened to see, which can
be hours later. Both come from the price API, falling back to the run's own
price only if the API has nothing.

Completion is not tied to the day either. Every run sweeps for weekend rows
whose reopen or Monday noon has passed and are still blank, whatever day it
is. The earlier version required the completing run to land on the Monday
itself, so a weekend whose Monday runs were all dropped would have stayed
incomplete permanently rather than merely late. A run's own price stands in
only when it is within two hours of the target; beyond that the cell waits.

To work on it locally instead:

```bash
pip install -r requirements-dev.txt
python -m pytest -q            # the rule tests
python -m src.main --dry-run   # needs no keys
```

---

## Version 2: the race

From `v2` the news window covers everything published since the previous run
(plus 10 minutes, at least 90 minutes, at most 12 hours), instead of only the
last 90 minutes, so rows are not comparable with `v1.x` on input. Six bots race
on the same logged scores and prices and differ only in their rules:

1. **Bot 1: Original**: the locked rules.
2. **Bot 2: Hold longer**: the locked rules without the reversal exit.
3. **Bot 3: Lower bar**: enters at a score of +/-4 instead of +/-6.
4. **Bot 4: Lower bar + hold longer**: both changes.
5. **Bot 5: Calendar only**: long each week, out by Friday, reads no news.
6. **Bot 6: Opposite**: the locked rules on negated scores, so it shorts where the original buys.

The sheet's `state` and `trades` tabs are bot 1. The others are replays of the
same logged scores (`scripts/leaderboard.py`), so there is no extra live state.
Every bot starts the race with a pretend $100 and stakes the whole balance on each trade (no borrowing), so the balance compounds, and the page and leaderboard rank by what that $100 is now, counting any open position at the last logged price.
The standings live in the `leaderboard` sheet tab, in `backup/leaderboard.json`
(committed weekly), and in the `analysis` workflow's log.

With six bots one will lead by luck. A winner has to repeat in a second period
before it means anything. The `luck p` column shows how often the same bot,
with its scores slid round the clock, matches or beats its real total; it only
appears after 10 closed trades and 60 race rows.

---

## The locked rules

These do not change during the test. If one must change, bump `RULES_VERSION` in
`src/config.py` so the rows stay comparable.

| Rule | Setting |
| --- | --- |
| Market hours | CME crude, `America/Chicago`. Sunday 17:00 open, Friday 16:00 close, daily break 16:00 to 17:00 |
| Enter long | score >= +6 and confidence medium or high, flat |
| Enter short | score <= -6 and confidence medium or high, flat |
| Exit on reversal | long closes at score <= -3, short closes at score >= +3 |
| Trailing stop | 3% from the best price since entry |
| Friday | no new entries after 10:00 Chicago, any open position closed before the 16:00 close, or at the first run after it if that one was dropped |
| Size | one position at a time, 1 unit |
| Rules version | `v2`, stamped on every row. Trading thresholds unchanged from v1 |
| Spread | $0.05 per barrel deducted per round trip |

**The trailing stop is checked once per run, not tick by tick.** A real fill would be
worse, sometimes much worse on a fast move. Read every stop exit in the log as a
best case.

**Weekends stay flat.** eToro offers oil around the clock now, but stop losses do
not trigger at weekends and spreads widen, so the test closes on Friday. Weekend
news is still fetched, scored and logged, and the `weekend_gaps` tab records what
holding through the weekend would have done. That is how we find out whether
staying flat costs us anything.

---

## The sheet

| Tab | What it holds |
| --- | --- |
| `signals` | one row per run (about 3 to 4 a day): price, score, action, and the forward price moves at 1h, 4h, 24h and 72h |
| `trades` | one row per closed paper trade, with P&L in dollars per barrel and percent |
| `headlines` | every headline used, hashed for dedupe. The audit trail |
| `state` | a single row holding the current paper position |
| `weekend_gaps` | one row per weekend: the Friday close, the weekend scores, the Sunday gap, and what holding through would have made |

`state` carries one column beyond the original spec, `entry_score`, so a closed
trade can report the score it opened on.

---

## Reading the results

After four weeks, the question is simple: did a high score precede a rise?

In `signals`, compare `score` against `move_24h_pct` and `move_72h_pct`. Split the
rows by `confidence`. If `high` confidence scores above +6 do not beat the base
rate, the signal is noise and the experiment has done its job for the price of a
few dollars of tokens.

`trades` gives the headline number, but it is a small sample. `signals` is the
real dataset because it scores every run, traded or not.

---

## Cost

One Claude call per run, roughly 1,500 tokens in and 100 out. Well under a dollar
a day. Actual tokens and estimated cost are written into the `notes` column of
every row.

## Out of scope

No broker connection. No eToro. No real orders. No rule changes mid test.


---

## IG demo mirror

Optional. Copies each paper trade onto an IG **demo** account (fake money, real
prices) so you can see real fills. There is no live mode in the code.

How it behaves:

* Opens only on the run where the paper rules enter. A missed entry is not chased.
* Closes whatever is open on the market whenever the paper position is flat or flips.
* Reads IG's open positions first every run, so a rerun never doubles up and a
  failed close is retried next run.
* Puts a stop at IG 5% from entry (`BROKER_STOP_PCT`), a backstop for the hours
  between runs. The paper 3% trailing stop normally exits first.
* Stake is capped at `BROKER_MAX_SIZE` per point.
* Any IG error is written to the `notes` column. It never fails the run.

Setup:

1. Open an IG demo account at ig.com, then in My IG go to **Settings → API keys**
   and create a **demo** key.
2. In GitHub: **Settings → Secrets and variables → Actions → Secrets**, add
   `IG_API_KEY`, `IG_USERNAME`, `IG_PASSWORD`. Add `IG_ACCOUNT_ID` only if the
   demo has more than one account (spread bet and CFD).
3. Run the **ig_check** workflow from the Actions tab. It logs in and lists Brent
   markets with their epics. It places no orders.
4. Under **Variables**, add `IG_EPIC` (from step 3), `IG_EXPIRY` (`DFB` for a
   spread bet, `-` for a CFD, as ig_check shows), and `IG_SIZE` (stake per point,
   start at `1`).
5. Add the variable `BROKER_MODE` = `demo`. That switches it on.

To switch it off, delete `BROKER_MODE` or set it to `off`.
