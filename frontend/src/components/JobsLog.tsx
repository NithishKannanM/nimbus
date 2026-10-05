import { useCallback } from "react";

import { usePoll } from "../hooks/usePoll";
import { api, type JobStatus } from "../lib/api";

/**
 * Which of the three roles a job status plays, in the same terms as
 * declared/reserved/observed: is Nimbus currently holding this normally
 * (reserved ink), is it the unremarkable happy path (plain ink), or has it
 * diverged from what was expected (observed/red)?
 */
function statusTone(status: JobStatus): string {
  switch (status) {
    case "QUEUED":
    case "SCHEDULED":
    case "RUNNING":
      return "text-reserved";
    case "COMPLETED":
      return "text-ink";
    case "RETRYING":
    case "TIMED_OUT":
    case "FAILED":
      return "text-observed";
    default:
      return "text-declared";
  }
}

/** Recent job activity, log-book style: one row per job, newest first. */
export function JobsLog() {
  const jobs = usePoll(useCallback((signal?: AbortSignal) => api.jobs(30, signal), []), 3000);
  const rows = jobs.data ?? [];

  return (
    <div className="plate p-4">
      <h2 className="legend mb-3">
        Jobs
        {jobs.failures > 1 && (
          <span role="status" className="ml-3 text-observed">api unreachable · last known</span>
        )}
      </h2>
      <div className="max-h-72 overflow-auto pr-2 [scrollbar-gutter:stable]" tabIndex={0} role="region" aria-label="Recent jobs">
        <table className="w-full min-w-[420px]">
          <caption className="sr-only">Recent jobs, newest first</caption>
          <thead className="sticky top-0 bg-paper">
            <tr className="legend text-left text-declared">
              <th scope="col" className="pb-1 font-normal">job</th>
              <th scope="col" className="pb-1 font-normal">type</th>
              <th scope="col" className="pb-1 font-normal">status</th>
              <th scope="col" className="pb-1 font-normal">worker</th>
              <th scope="col" className="pb-1 text-right font-normal">attempts</th>
              <th scope="col" className="pb-1 text-right font-normal">exec</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((job) => (
              <tr key={job.job_id} className="border-t border-ink/10">
                <td className="py-1 pr-2 font-log text-[11px] text-declared">
                  {job.job_id.replace("job-", "")}
                </td>
                <td className="py-1 pr-2 font-log text-[11px] text-ink">{job.job_type}</td>
                <td
                  className={`py-1 pr-2 font-log text-[11px] ${statusTone(job.status)}`}
                  title={job.error ?? undefined}
                >
                  {job.status}
                </td>
                <td className="py-1 pr-2 font-log text-[11px] text-declared">
                  {job.assigned_worker ?? "—"}
                </td>
                <td
                  className={`stat py-1 pr-2 text-right font-log text-[11px] ${
                    job.attempts > 1 ? "text-observed" : "text-declared"
                  }`}
                >
                  {job.attempts}/{job.max_attempts}
                </td>
                <td className="stat py-1 text-right font-log text-[11px] text-declared">
                  {job.execution_time_ms === null
                    ? "—"
                    : `${(job.execution_time_ms / 1000).toFixed(2)}s`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && (
          <p className="py-4 text-center font-log text-[11px] text-declared">
            {jobs.loading ? "reading the log…" : jobs.error ? "api unreachable, no jobs loaded" : "no jobs yet"}
          </p>
        )}
      </div>
    </div>
  );
}
