import { useState } from "react";

import type { Candidate, Decision } from "../lib/api";
import { SectionHead } from "./SectionHead";

/** The square mark DivergenceStrip draws at "now" on each lane, reused here:
 *  the strip marks where the trace is right now, this marks which worker the
 *  placement landed on. */
function PenMark() {
  return <span aria-hidden className="inline-block h-2 w-2 shrink-0 bg-current" />;
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
    <tr className={`row-rule ${candidate.eligible ? "" : "opacity-60"}`}>
      <td className="py-1 pr-3 font-mono text-[12px] text-ink">
        <span className="flex items-center gap-1.5">
          {selected && (
            <span className="text-observed">
              <PenMark />
            </span>
          )}
          {workerId}
        </span>
      </td>
      <td className="py-1 pr-3 font-mono text-[12px]">
        {candidate.eligible ? (
          <span className="text-ink">yes</span>
        ) : (
          <span className="text-declared" title={candidate.ineligible_reason ?? undefined}>
            no
          </span>
        )}
      </td>
      <td className="stat py-1 pr-3 text-right font-mono text-[12px] text-declared">
        {candidate.declared_cpu_units}u &middot; {candidate.declared_slots}s
      </td>
      <td className="stat py-1 pr-3 text-right font-mono text-[12px] text-ink font-semibold">
        {candidate.reserved_cpu_units}u &middot; {candidate.active_jobs}/{candidate.declared_slots}
      </td>
      {/* Always rendered, whether or not this policy could read it. A static
          policy's candidates carry observed: null -- that column present but
          empty, next to a populated reserved column, is the visible evidence
          the policy genuinely could not see the host. Omitting the column
          would let a reader assume the data was never collected, rather than
          never consulted. */}
      <td
        className={`stat py-1 pr-3 text-right font-mono text-[12px] ${
          observedCpu === null ? "text-declared" : "font-semibold text-observedText"
        }`}
      >
        {observedCpu === null ? "—" : `${observedCpu.toFixed(0)}%`}
      </td>
      <td className="stat py-1 text-right font-mono text-[12px] text-ink">
        {candidate.score === null ? "—" : candidate.score.toFixed(4)}
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

  if (decisions.length === 0) {
    return (
      <section aria-labelledby="ledger-h">
        <SectionHead n="03" id="ledger-h">Decision ledger</SectionHead>
        <p className="font-mono text-[12px] text-declared">
          {error ? "api unreachable, no placements loaded yet" : loading ? "reading the ledger…" : "no placements yet"}
        </p>
      </section>
    );
  }

  return (
    <section aria-labelledby="ledger-h">
      <SectionHead
        n="03"
        id="ledger-h"
        aside={
          <p className="label text-declared">
            {error ? <span className="font-semibold text-observedText">[ api unreachable · last known ] </span> : null}
            observed is recorded even when placement cannot read it
          </p>
        }
      >
        Decision ledger
      </SectionHead>

      <div className="divide-y divide-hairline border-y-2 border-ink">
        {decisions.map((decision) => {
          const open = openId === decision.decision_id;
          return (
            <div key={decision.decision_id}>
              <button
                type="button"
                onClick={() => setOpenId(open ? null : decision.decision_id)}
                aria-expanded={open}
                className="flex min-h-[40px] w-full items-center justify-between gap-3 py-2 text-left hover:underline decoration-[3px] decoration-observed underline-offset-4"
              >
                <span className="flex min-w-0 items-baseline gap-3">
                  <span className="font-mono text-[12px] text-ink">
                    {decision.job_id.replace("job-", "")}
                  </span>
                  <span className="label truncate text-declared">
                    {decision.placement_policy}
                  </span>
                </span>
                <span className="flex shrink-0 items-baseline gap-4">
                  {decision.selected_worker ? (
                    <span className="font-mono text-[12px] text-ink">
                      {decision.selected_worker}
                    </span>
                  ) : (
                    <span className="label font-semibold text-observedText">[ no candidate ]</span>
                  )}
                  {decision.margin === 0 && (
                    <span className="label font-semibold text-observedText">[ tie-break ]</span>
                  )}
                  <span className="stat font-mono text-[12px] text-declared">
                    {decision.decision_latency_us.toFixed(0)}&micro;s
                  </span>
                </span>
              </button>

              {open && (
                <div className="border-t-2 border-ink pb-3 pl-1">
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[460px]">
                      <caption className="sr-only">Candidates considered for {decision.job_id}</caption>
                      <thead>
                        <tr className="thead-rule">
                          <th className="font-normal">worker</th>
                          <th className="font-normal">eligible</th>
                          <th className="text-right text-declared">
                            declared
                          </th>
                          <th className="text-right">
                            reserved
                          </th>
                          <th className="text-right text-observedText">
                            observed
                          </th>
                          <th className="text-right">score</th>
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

                  <p className="mt-2 border-l-4 border-ink pl-3 font-mono text-[12px] leading-relaxed text-ink">
                    {decision.reason}
                  </p>

                  {Object.keys(decision.weights).length > 0 && (
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
    </section>
  );
}
