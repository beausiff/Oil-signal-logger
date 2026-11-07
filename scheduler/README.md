# Scheduler

**You probably do not need what is in this folder.** The cheap fix below is
already live and gets most of the way for nothing. The Cloudflare worker is
the belt and braces version and it needs a terminal, so it is not a phone job.

## The cheap fix, already live

GitHub drops most scheduled runs, and a missed hour is a row that never
exists. Asking once an hour loses the hour.

So the workflow now fires every 10 minutes instead, and `src/main.py` keeps
the first attempt in each hour and stops the rest. A skipped attempt reads two
cells and exits, before any news fetch, price call or model call, so the extra
attempts cost nothing.

Six attempts an hour at roughly a 1 in 4 hit rate lands about 82% of hours,
against 25% before. This repository is public, so the extra Actions minutes
are free too.

The guard lives in `config.MIN_MINUTES_BETWEEN_RUNS`, set to 55 rather than 60
on purpose: at 60 a run landing at :03 would block the attempt at :57 and the
hour would be lost anyway.

A hand started run (`workflow_dispatch`) passes `--force` and ignores the
guard, so manual testing always works.

## The belt and braces version

Worth doing only if 82% is not enough. Cloudflare Workers cron triggers fire
reliably. `worker.js` calls GitHub's `workflow_dispatch` endpoint once an hour
and logs a warning when the previous run is more than three hours old.

Needs a laptop.

**1. A GitHub token that can only do this one thing**

Settings → Developer settings → Personal access tokens → Fine grained.

- Repository access: only `beausiff/Oil-signal-logger`
- Permissions → Repository → **Actions: Read and write**. Nothing else.
- Set a calendar reminder for the expiry. An expired token fails exactly the
  way the Anthropic key did.

**2. Deploy**

```sh
npm install -g wrangler
cd scheduler
wrangler login
wrangler secret put GITHUB_TOKEN     # paste at the prompt, never in a file
wrangler deploy
```

**3. Test it without waiting an hour**

`wrangler deploy` prints a `*.workers.dev` URL. Open it. It dispatches a run
and reports how long ago the previous one was.

**4. Only then, thin out the GitHub schedule**

Once Cloudflare triggered runs are landing, reduce the `cron:` line in
`.github/workflows/hourly.yml` back to hourly, or drop it. Keep
`workflow_dispatch:`, because that is what the worker calls.

Do not touch step 4 before step 3 passes, or nothing is running the job.

## Free either way

Cloudflare's free plan covers 100,000 requests a day. This uses 24.

## If runs stop

The worker logs a warning when the last run is over three hours old
(`wrangler tail`). Separately, the hourly job exits non zero after three
consecutive runs that score nothing, which triggers GitHub's own failure
email. Between them, silence is no longer indistinguishable from working,
which is what let a dead API key go unnoticed for nineteen days.
