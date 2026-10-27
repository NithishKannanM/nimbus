import { useCallback } from "react";

import { usePoll } from "../hooks/usePoll";
import { api, type JobStatus } from "../lib/api";
import { Card, StaleChip } from "./SectionHead";

/**
 * Which of the three roles a job status plays, in the same terms as
 * declared/reserved/observed: is Nimbus currently holding this (reserved
 * blue), is it the unremarkable happy path (neutral), or has it diverged from
 * what was expected (observed coral)?
 */
function statusChip(status: JobStatus): string {
  switch (status) {
    case "QUEUED":
    case "SCHEDULED":
    case "RUNNING":
      return "chip-reserved";
    case "RETRYING":
    case "TIMED_OUT":
    case "FAILED":
      return "chip-observed";
    default:
      return "chip-neutral";
  }
}

/** Recent job activity, newest first. */
export function JobsLog() {
  const jobs = usePoll(useCallback((signal?: AbortSignal) => api.jobs(30, signal), []), 3000);
  const rows = jobs.data ?? [];

  return (
    <Card
      id="jobs"
      title="Recent jobs"
      subtitle="The last 30 submissions, newest first"
      action={jobs.failures > 1 ? <StaleChip /> : undefined}
    >
      <div
        className="max-h-[420px] overflow-auto [scrollbar-gutter:stable]"
        tabIndex={0}
        role="region"
        aria-label="Recent jobs"
      >
        <table className="data-table min-w-[560px]">
          <caption className="sr-only">Recent jobs, newest first</caption>
          <thead className="sticky top-0 bg-paper">
            <tr>
              <th scope="col">Job</th>
              <th scope="col">Type</th>
              <th scope="col">Worker</th>
              <th scope="col">Status</th>
              <th scope="col" className="text-right">Attempts</th>
              <th scope="col" className="text-right">Exec time</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((job) => (
              <tr key={job.job_id}>
                <td className="font-mono text-[12px]">{job.job_id.replace("job-", "")}</td>
                <td className="font-medium text-ink">{job.job_type}</td>
                <td>{job.assigned_worker ?? "-"}</td>
                <td title={job.error ?? undefined}>
                  <span className={statusChip(job.status)}>{job.status.toLowerCase().replace("_", " ")}</span>
                </td>
                <td
                  className={`text-right ${
                    job.attempts > 1 ? "font-semibold text-observedText" : ""
                  }`}
                >
                  {job.attempts}/{job.max_attempts}
                </td>
                <td className="text-right font-medium text-ink">
                  {job.execution_time_ms === null
                    ? "-"
                    : `${(job.execution_time_ms / 1000).toFixed(2)}s`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && (
          <p className="py-6 text-center text-[13px] text-declared">
            {jobs.loading ? "Reading the log…" : jobs.error ? "API unreachable, no jobs loaded." : "No jobs yet."}
          </p>
        )}
      </div>
    </Card>
  );
}
