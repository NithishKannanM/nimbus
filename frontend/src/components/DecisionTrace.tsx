import { useState } from "react";

import type { Candidate, Decision } from "../lib/api";
import { IconChevron } from "./Icons";
import { Card, StaleChip } from "./SectionHead";

/** Marks which worker the placement landed on. */
function PenMark() {
  return <span aria-hidden className="inline-block h-2 w-2 shrink-0 rounded-full bg-reserved" />;
}

function CandidateRow({
  workerId,
  candidate,
  selected,
}: {
  workerId: string;
  candidate: Candidate;
  selected: boolean;
}) {
  const observedCpu = candidate.observed?.cpu_percent ?? null;
  return (
    <tr className={candidate.eligible ? "" : "opacity-60"}>
      <td className="font-medium text-ink">
        <span className="flex items-center gap-2">
          {selected ? <PenMark /> : <span aria-hidden className="inline-block w-2" />}
          {workerId}
        </span>
      </td>
      <td className="font-mono text-[12px]">
        {candidate.eligible ? (
          <span className="text-ink">yes</span>
        ) : (
          <span className="text-declared" title={candidate.ineligible_reason ?? undefined}>
            no
          </span>
        )}
      </td>
      <td className="text-right font-mono text-[12px] text-declared">
        {candidate.declared_cpu_units}u &middot; {candidate.declared_slots}s
      </td>
      <td className="text-right font-mono text-[12px] text-ink font-semibold">
        {candidate.reserved_cpu_units}u &middot; {candidate.active_jobs}/{candidate.declared_slots}
      </td>
      {/* Always rendered, whether or not this policy could read it. A static
          policy's candidates carry observed: null -- that column present but
          empty, next to a populated reserved column, is the visible evidence
          the policy genuinely could not see the host. Omitting the column
          would let a reader assume the data was never collected, rather than
          never consulted. */}
      <td
        className={`text-right font-mono text-[12px] ${
          observedCpu === null ? "text-declared" : "font-semibold text-observedText"
        }`}
      >
        {observedCpu === null ? "-" : `${observedCpu.toFixed(0)}%`}
      </td>
      <td className="text-right font-mono text-[12px] text-ink">
        {candidate.score === null ? "-" : candidate.score.toFixed(4)}
      </td>
    </tr>
  );
}

/**
 * The decision ledger: every candidate a placement considered, what it knew
 * about each, and why one was chosen.
 *
 * The observed column is the whole point of this panel. It is recorded in
 * every decision trace regardless of which policy made the choice, because the
 * trace is assembled from the full fleet snapshot after the policy has already
 * answered -- recording and consuming are different code paths. Seeing a
 * worker at 100% observed CPU sitting next to a score computed purely from
 * reservations is the evidence, at decision granularity, that the boundary the
 * whole project depends on actually holds.
 */
export function DecisionTrace({
  decisions,
  loading = false,
  error = false,
}: {
  decisions: Decision[];
  loading?: boolean;
  error?: boolean;
}) {
  const [openId, setOpenId] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const PREVIEW = 8;

  if (decisions.length === 0) {
    return (
      <Card id="decisions" title="Decision ledger" className="h-full">
        <p className="text-[13px] text-declared">
          {error ? "API unreachable, no placements loaded yet." : loading ? "Reading the ledger…" : "No placements yet."}
        </p>
      </Card>
    );
  }

  const shown = showAll ? decisions : decisions.slice(0, PREVIEW);

  return (
    <Card
      id="decisions"
      title="Decision ledger"
      subtitle="Select a row for its candidates"
      info="Every placement with the full state of each worker at that moment. Observed load is recorded even when the active placement cannot read it, which is how you can see a static policy pick a worker at 100% CPU."
      action={error ? <StaleChip /> : undefined}
      className="h-full"
    >
      <div className="-mx-2 divide-y divide-hairline">
        {shown.map((decision) => {
          const open = openId === decision.decision_id;
          return (
            <div key={decision.decision_id}>
              <button
                type="button"
                onClick={() => setOpenId(open ? null : decision.decision_id)}
                aria-expanded={open}
                className="flex min-h-[48px] w-full items-center justify-between gap-3 rounded-card px-2 py-2 text-left transition-colors hover:bg-muted"
              >
                <span className="flex min-w-0 items-center gap-3">
                  <IconChevron
                    className={`h-4 w-4 shrink-0 text-declared transition-transform ${open ? "rotate-180" : "-rotate-90"}`}
                  />
                  <span className="font-mono text-[13px] text-ink">
                    {decision.job_id.replace("job-", "")}
                  </span>
                  <span className="chip-neutral hidden sm:inline-flex">
                    {decision.placement_policy}
                  </span>
                </span>
                <span className="flex shrink-0 items-center gap-3">
                  {decision.margin === 0 && <span className="chip-observed">tie-break</span>}
                  {decision.selected_worker ? (
                    <span className="text-[13px] font-medium text-ink">
                      {decision.selected_worker}
                    </span>
                  ) : (
                    <span className="chip-observed">no candidate</span>
                  )}
                  <span className="w-12 text-right font-mono text-[12px] text-declared">
                    {decision.decision_latency_us.toFixed(0)}&micro;s
                  </span>
                </span>
              </button>

              {open && (
                <div className="mb-3 ml-9 mr-2 rounded-card bg-muted/60 p-4">
                  <div className="overflow-x-auto">
                    <table className="data-table min-w-[460px]">
                      <caption className="sr-only">Candidates considered for {decision.job_id}</caption>
                      <thead>
                        <tr>
                          <th scope="col">Worker</th>
                          <th scope="col">Eligible</th>
                          <th scope="col" className="text-right">Declared</th>
                          <th scope="col" className="text-right">Reserved</th>
                          <th scope="col" className="text-right">Observed</th>
                          <th scope="col" className="text-right">Score</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(decision.candidates)
                          .sort(([a], [b]) => a.localeCompare(b))
                          .map(([workerId, candidate]) => (
                            <CandidateRow
                              key={workerId}
                              workerId={workerId}
                              candidate={candidate}
                              selected={decision.selected_worker === workerId}
                            />
                          ))}
                      </tbody>
                    </table>
                  </div>

                  <p className="mt-3 text-[13px] leading-relaxed text-ink">{decision.reason}</p>

                  {decision.weights && Object.keys(decision.weights).length > 0 && (
                    <p className="mt-1.5 font-mono text-[12px] text-declared">
                      weights:{" "}
                      {Object.entries(decision.weights)
                        .map(([k, v]) => `${k}=${v}`)
                        .join("  ")}
                    </p>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {decisions.length > PREVIEW && (
        <button
          type="button"
          onClick={() => setShowAll((v) => !v)}
          className="mt-4 rounded-card px-3 py-2 text-[13px] font-semibold text-reserved hover:bg-reservedLight"
        >
          {showAll ? "Show fewer" : `View all ${decisions.length}`}
        </button>
      )}
    </Card>
  );
}
