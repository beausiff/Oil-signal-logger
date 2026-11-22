# Scheduler

GitHub only delivers about 3 to 4 scheduled runs a day for this repository,
out of the 24 the hourly cron asks for. A missed run is a row that never
exists. The Cloudflare worker in this folder is the only real fix. It needs a
terminal, so it is not a phone job.

## What was tried

The workflow was changed from hourly to every 10 minutes, with `src/main.py`
keeping the first attempt in each hour and stopping the rest. Measured over two
weeks on each side, it made no measurable difference: GitHub still delivered
3 to 4 runs a day, which is roughly 15% of hours. More attempts do not make
GitHub deliver more. The cron is hourly again at minute 23.

The guard is still there, in `config.MIN_MINUTES_BETWEEN_RUNS`, set to 55
rather than 60 so a run at :03 does not block the one at :58. It is harmless,
and it is what stops the worker and a scheduled run firing twice in the same
hour. The worker dispatches with `force=false` so the guard applies to it. A
hand started run (`workflow_dispatch`) from the Actions tab defaults to
`--force` and ignores the guard, so manual testing always works.

## The fix: an external scheduler

Cloudflare Workers cron triggers fire reliably. `worker.js` calls GitHub's
`workflow_dispatch` endpoint once an hour and logs a warning when the previous
run is more than three hours old. Any external scheduler that can call that
endpoint would do the same job.

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

**4. Optionally, drop the GitHub schedule**

Once Cloudflare triggered runs are landing, the `cron:` line in
`.github/workflows/hourly.yml` can be removed. Keep `workflow_dispatch:`,
because that is what the worker calls. Do not do this before step 3 passes, or
nothing is running the job.

## Free either way

Cloudflare's free plan covers 100,000 requests a day. This uses 24.

## If runs stop

The worker logs a warning when the last run is over three hours old
(`wrangler tail`). Separately, the hourly job exits non zero after three
consecutive runs that tried to score and failed, which triggers GitHub's own
failure email. Between them, silence is no longer indistinguishable from
working, which is what let a dead API key go unnoticed for nineteen days.
