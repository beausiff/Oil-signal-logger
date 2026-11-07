// Fires the hourly run on a clock that actually keeps time.
//
// GitHub's own scheduler dropped roughly three runs in four, which cost this
// experiment most of its sample size. Cloudflare's cron triggers are on the
// free plan and fire reliably, so this worker does nothing but call GitHub's
// workflow_dispatch endpoint once an hour.
//
// It also checks that the run before it actually happened. If the last run is
// much older than an hour, something is wrong beyond a single missed tick, and
// it says so in the log where it can be seen.

const OWNER = "beausiff";
const REPO = "Oil-signal-logger";
const WORKFLOW = "hourly.yml";
const BRANCH = "main";

const API = `https://api.github.com/repos/${OWNER}/${REPO}/actions`;

function headers(token) {
  return {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    // GitHub rejects API calls with no User-Agent.
    "User-Agent": "oil-signal-scheduler",
  };
}

async function lastRunAgeMinutes(token) {
  const res = await fetch(`${API}/workflows/${WORKFLOW}/runs?per_page=1`, {
    headers: headers(token),
  });
  if (!res.ok) return null;
  const body = await res.json();
  const run = body.workflow_runs && body.workflow_runs[0];
  if (!run) return null;
  return (Date.now() - new Date(run.created_at).getTime()) / 60000;
}

async function dispatch(token) {
  const res = await fetch(`${API}/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    headers: { ...headers(token), "Content-Type": "application/json" },
    body: JSON.stringify({ ref: BRANCH }),
  });
  // 204 is success for this endpoint. Anything else is worth seeing.
  if (res.status !== 204) {
    throw new Error(`dispatch failed: ${res.status} ${await res.text()}`);
  }
}

export default {
  async scheduled(event, env, ctx) {
    const age = await lastRunAgeMinutes(env.GITHUB_TOKEN);
    if (age !== null && age > 180) {
      console.log(`WARNING: last run was ${Math.round(age)} minutes ago`);
    }
    await dispatch(env.GITHUB_TOKEN);
    console.log("dispatched hourly run");
  },

  // Lets you confirm it works without waiting for the hour.
  async fetch(request, env) {
    const age = await lastRunAgeMinutes(env.GITHUB_TOKEN);
    await dispatch(env.GITHUB_TOKEN);
    return new Response(
      `dispatched. previous run was ${age === null ? "unknown" : Math.round(age) + " minutes"} ago\n`
    );
  },
};
