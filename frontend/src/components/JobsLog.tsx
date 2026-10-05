import { useCallback } from "react";

import { usePoll } from "../hooks/usePoll";
import { api, type JobStatus } from "../lib/api";
import { SectionHead } from "./SectionHead";

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
      return "font-bold text-ink";
    case "COMPLETED":
      return "text-ink";
    case "RETRYING":
    case "TIMED_OUT":
    case "FAILED":
      return "font-semibold text-observedText";
    default:
      return "text-declared";
  }
}

/** Recent job activity, log-book style: one row per job, newest first. */
export function JobsLog() {
  const jobs = usePoll(useCallback((signal?: AbortSignal) => api.jobs(30, signal), []), 3000);
  const rows = jobs.data ?? [];

  return (
    <section aria-labelledby="jobs-h">
      <SectionHead
        n="05"
        id="jobs-h"
        aside={
          jobs.failures > 1 ? (
            <span role="status" className="label font-semibold text-observedText">[ api unreachable · last known ]</span>
          ) : undefined
        }
      >
        Jobs
      </SectionHead>
      <div className="max-h-72 overflow-auto border-b-2 border-ink pr-2 [scrollbar-gutter:stable]" tabIndex={0} role="region" aria-label="Recent jobs">
        <table className="w-full min-w-[420px]">
          <caption className="sr-only">Recent jobs, newest first</caption>
          <thead className="sticky top-0 bg-paper">
            <tr className="thead-rule">
              <th scope="col">job</th>
              <th scope="col">type</th>
              <th scope="col">status</th>
              <th scope="col">worker</th>
              <th scope="col" className="text-right">attempts</th>
              <th scope="col" className="text-right">exec</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((job) => (
              <tr key={job.job_id} className="row-rule">
                <td className="py-1 pr-2 font-mono text-[12px] text-declared">
                  {job.job_id.replace("job-", "")}
                </td>
                <td className="py-1 pr-2 font-mono text-[12px] text-ink">{job.job_type}</td>
                <td
                  className={`py-1 pr-2 font-mono text-[12px] ${statusTone(job.status)}`}
                  title={job.error ?? undefined}
                >
                  {job.status}
                </td>
                <td className="py-1 pr-2 font-mono text-[12px] text-declared">
                  {job.assigned_worker ?? "—"}
                </td>
                <td
                  className={`stat py-1 pr-2 text-right font-mono text-[12px] ${
                    job.attempts > 1 ? "font-semibold text-observedText" : "text-declared"
                  }`}
                >
                  {job.attempts}/{job.max_attempts}
                </td>
                <td className="stat py-1 text-right font-mono text-[12px] text-declared">
                  {job.execution_time_ms === null
                    ? "—"
                    : `${(job.execution_time_ms / 1000).toFixed(2)}s`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && (
          <p className="py-4 text-center font-mono text-[12px] text-declared">
            {jobs.loading ? "reading the log…" : jobs.error ? "api unreachable, no jobs loaded" : "no jobs yet"}
          </p>
        )}
      </div>
    </section>
  );
}
