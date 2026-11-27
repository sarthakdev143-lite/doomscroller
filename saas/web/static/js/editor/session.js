// Following a re-render, and picking one back up after a reload. Pure (the API and the clock are passed in),
// so tests/saas/test_web_editor.py runs it under node.

export const FINAL = ["done", "failed", "canceled"];

/** The re-render already running for this clip, from GET /api/clips/{id}/edit (`active_job_id`), or null. */
export function activeJob(raw) {
  const id = raw && raw.active_job_id;
  return typeof id === "string" && id ? id : null;
}

/**
 * Poll a job until it is final. `getJob(id)` is GET /api/jobs/{id}; `onProgress(job)` sees every reading.
 * A 404 ends it at once; other failures are retried, up to `maxMisses` in a row.
 */
export async function followJob(id, { getJob, wait, onProgress = () => {}, hidden = () => false, maxMisses = 5 }) {
  let misses = 0;
  for (;;) {
    let job;
    try {
      job = await getJob(id);
      misses = 0;
    } catch (err) {
      if ((err && err.status === 404) || misses >= maxMisses) throw err;
      misses += 1;
      await wait(4000);
      continue;
    }
    onProgress(job);
    if (FINAL.includes(job.status)) return job;
    await wait(hidden() ? 5000 : 1500);
  }
}

/** Why a finished job did not produce a version, in words for the customer; "" when it did. */
export function failureText(job) {
  if (!job || job.status === "done") return "";
  const why = job.error && job.error.message ? job.error.message
    : job.status === "canceled" ? "The render was canceled." : "The render didn’t finish.";
  return `${why.replace(/\.?$/, ".")} Your edits are still here; render again when you’re ready.`;
}
