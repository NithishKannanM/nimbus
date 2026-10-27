import type { ReactNode } from "react";

import type { Decision, SchedulerState } from "../lib/api";
import { placementReadsTelemetry } from "../lib/api";
import { InfoTip } from "./InfoTip";
import { Card } from "./SectionHead";

/**
 * Plain-language descriptions of each placement policy. These restate the
 * code in scheduler/placement/, they do not replace it: the weights shown under
 * "Last decision" come from the trace, so they cannot drift from what ran.
 */
const PLACEMENT: Record<string, { short: string; summary: string; formula: string; picks: string }> = {
  round_robin: {
    short: "Next in turn",
    summary:
      "Hands each job to the next eligible worker in ID order. It looks at nothing but whose turn it is.",
    formula: "next = eligible[cursor mod n]",
    picks: "the next worker in turn",
  },
  static_resource_aware: {
    short: "Least reserved",
    summary:
      "Scores each eligible worker by how full it would be after taking this job, from Nimbus's reservations only. Spreads load across the fleet.",
    formula: "score = 0.40 × cpu_reserved + 0.30 × mem_reserved + 0.30 × slots_used   (projected, incl. this job)",
    picks: "the lowest score",
  },
  static_resource_aware_binpack: {
    short: "Most packed",
    summary:
      "Same reservation-only score, opposite objective: packs jobs onto the fewest workers (the ECS binpack analogue).",
    formula: "score = 0.40 × cpu_reserved + 0.30 × mem_reserved + 0.30 × slots_used   (projected, incl. this job)",
    picks: "the highest score",
  },
  adaptive: {
    short: "Least loaded",
    summary:
      "Reservation terms plus what the host actually reports. Weights depend on the job's workload class. If a worker's telemetry is stale it falls back to reservations, so an unmeasured worker never looks idle.",
    formula:
      "score = w_cpu × observed_cpu + w_mem × observed_mem + w_res × reservation + w_queue × slots_used + w_trend × cpu_trend",
    picks: "the lowest score",
  },
};

function Step({ n, title, value, children }: { n: number; title: string; value: string; children: ReactNode }) {
  return (
    <li className="flex items-center gap-3 rounded-card border border-hairline px-4 py-3">
      <span
        aria-hidden
        className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-reservedLight text-[12px] font-semibold text-reserved"
      >
        {n}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-[12px] text-declared">{title}</span>
        <span className="block text-[14px] font-semibold text-ink">{value}</span>
      </span>
      <InfoTip label={`About step ${n}: ${title}`}>{children}</InfoTip>
    </li>
  );
}

/**
 * Which algorithm the scheduler is running, what it does, and its most recent
 * decision. Answers "what is it actually doing" without opening the code.
 */
export function ActiveAlgorithm({
  scheduler,
  latest,
}: {
  scheduler: SchedulerState;
  latest: Decision | undefined;
}) {
  const policy = scheduler.placement_policy;
  const reads = placementReadsTelemetry(policy);
  const info = PLACEMENT[policy];

  const chosen = latest?.selected_worker ? latest.candidates[latest.selected_worker] : undefined;

  return (
    <Card
      id="algorithm"
      title="Active algorithm"
      info="What the scheduler runs for every job it places. Hover the i on each step for the detail."
      action={
        <span className={reads ? "chip-observed" : "chip-neutral"}>
          {reads ? "Reads observed telemetry" : "Reservations only"}
        </span>
      }
    >
      <div className="grid gap-6 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div className="min-w-0">
          <div className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-1">
            <p className="break-all font-mono text-[18px] font-semibold text-ink">{scheduler.strategy}</p>
            <span className="chip-neutral font-mono">{scheduler.algorithm_version}</span>
          </div>

          <ol className="grid gap-3 sm:grid-cols-3">
            <Step n={1} title="Order" value={scheduler.ordering_policy === "fifo" ? "Oldest first" : scheduler.ordering_policy}>
              Strict arrival order. Every strategy shares it, so differences between strategies come
              from placement alone.
            </Step>
            <Step n={2} title="Filter" value="Fits & healthy">
              Drops workers that are unhealthy, have no free slot, or lack the declared CPU or memory
              the job asks for. Same rules for every strategy; telemetry never changes eligibility.
            </Step>
            <Step n={3} title="Place" value={info?.short ?? policy}>
              {info ? (
                <>
                  {info.summary} Picks {info.picks}.
                  <span className="mt-2 block font-mono text-[11px] opacity-80">{info.formula}</span>
                </>
              ) : (
                policy
              )}
            </Step>
          </ol>
        </div>

        <div className="flex flex-col justify-center rounded-card bg-reservedLight px-5 py-4">
          <div className="flex items-center justify-between gap-2">
            <p className="eyebrow text-ink">Last decision</p>
            {latest && (
              <InfoTip label="Why this worker">
                {latest.reason}
                {latest.weights && Object.keys(latest.weights).length > 0 && (
                  <span className="mt-2 block font-mono text-[11px] opacity-80">
                    {Object.entries(latest.weights)
                      .map(([k, v]) => `${k}=${v}`)
                      .join("  ")}
                  </span>
                )}
              </InfoTip>
            )}
          </div>
          {latest ? (
            <>
              <p className="mt-1 text-[26px] font-bold leading-tight text-ink">
                {latest.selected_worker ?? "No candidate"}
              </p>
              <p className="mt-1 text-[13px] text-ink">
                <span className="font-mono font-semibold">{latest.job_id.replace("job-", "")}</span>
                <span className="text-declared"> ({latest.workload_class.toLowerCase().replace("_", " ")})</span>
                {chosen?.score != null && (
                  <span className="text-declared">, score {chosen.score.toFixed(3)}</span>
                )}
              </p>
            </>
          ) : (
            <p className="mt-1 text-[14px] font-semibold text-ink">No job placed yet</p>
          )}
        </div>
      </div>
    </Card>
  );
}
