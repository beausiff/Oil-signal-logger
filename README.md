# Oil News Signal Logger

A four week **log only** experiment. Every hour it reads oil related news, scores
it with Claude, records a buy / sell / hold / flat decision against locked rules,
and logs the Brent price. Everything lands in a Google Sheet.

**There is no broker connection and no real trading.** Positions are paper rows
in a spreadsheet. The point is to find out, after four weeks, whether the signals
had any predictive value against what the price actually did.

---

## What it does each hour

1. Fetches oil news published in the last 90 minutes (Google News RSS, free).
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
| `GNEWS_API_KEY` | optional, leave out if you skipped it |

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

Once a correct row appears, the hourly schedule is already live and needs nothing
further from you.

To work on it locally instead:

```bash
pip install -r requirements-dev.txt
python -m pytest -q            # the rule tests
python -m src.main --dry-run   # needs no keys
```

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
| Friday | no new entries after 10:00 Chicago, any open position closed on the last run before 16:00 |
| Size | one position at a time, 1 unit |
| Spread | $0.05 per barrel deducted per round trip |

**The trailing stop is checked hourly, not tick by tick.** A real fill would be
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
| `signals` | one row per hour: price, score, action, and the forward price moves at 1h, 4h, 24h and 72h |
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
real dataset because it scores every hour, traded or not.

---

## Cost

One Claude call an hour, roughly 1,500 tokens in and 100 out. Well under a dollar
a day. Actual tokens and estimated cost are written into the `notes` column of
every row.

## Out of scope

No broker connection. No eToro. No real orders. No rule changes mid test.
