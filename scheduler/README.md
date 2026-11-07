# Scheduler

GitHub's cron dropped roughly three scheduled runs in four. Over the first four
weeks that turned an expected 672 rows into 143, and every statistical problem
this experiment had traced back to sample size. This replaces it.

Cloudflare Workers cron triggers are free and fire on time. The worker does one
thing: calls GitHub's `workflow_dispatch` endpoint once an hour.

## Deploy, about ten minutes

**1. A GitHub token that can only do this one thing**

github.com → Settings → Developer settings → Personal access tokens → Fine
grained tokens → Generate new token.

- Repository access: only `beausiff/Oil-signal-logger`
- Permissions → Repository → **Actions: Read and write**. Nothing else.
- Expiry: set a reminder, because an expired token fails exactly the way the
  Anthropic key did.

**2. Deploy**

```sh
npm install -g wrangler
cd scheduler
wrangler login
wrangler secret put GITHUB_TOKEN     # paste the token at the prompt
wrangler deploy
```

The token goes in as a Cloudflare secret. It is never in this repository.

**3. Check it works without waiting an hour**

`wrangler deploy` prints a `*.workers.dev` URL. Open it. It dispatches a run
immediately and tells you how long ago the previous one was.

**4. Turn off GitHub's schedule**

Once a few hours of Cloudflare triggered runs have landed, comment out the
`schedule:` block in `.github/workflows/hourly.yml`. Leave
`workflow_dispatch:` in place, because that is what the worker calls.

Do not do this before step 3 passes, or nothing is running the job.

## What it costs

Nothing. The free plan covers 100,000 requests a day and this uses 24.

## If runs stop

The worker logs a warning when the previous run is more than three hours old
(`wrangler tail` to watch). The hourly job also fails loudly after three
consecutive runs that score nothing, which sends GitHub's own failure email.
Between them, silence is no longer indistinguishable from working, which is
what let a dead API key run unnoticed for nineteen days.
