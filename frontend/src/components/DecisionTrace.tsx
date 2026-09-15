import { useState } from "react";

import type { Candidate, Decision } from "../lib/api";

/** The pen-nib marker DivergenceStrip draws at "now" on each lane, reused here
 *  unchanged. The one deliberate visual echo between the two panels: the strip
 *  marks where the pen is right now, this marks which worker the pen landed
 *  on. */
function PenMark() {
  return (
    <svg width={7} height={8} viewBox="0 0 7 8" className="inline-block shrink-0" aria-hidden>
      <path d="M5,0.6L5,7.4L1.5,4Z" fill="currentColor" />
    </svg>
  );
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
    <tr className={`border-t border-ink/10 ${candidate.eligible ? "" : "opacity-45"}`}>
      <td className="py-1 pr-3 font-log text-[11px] text-ink">
        <span className="flex items-center gap-1.5">
          {selected && (
            <span className="text-observed">
              <PenMark />
            </span>
          )}
          {workerId}
        </span>
      </td>
      <td className="py-1 pr-3 font-log text-[11px]">
        {candidate.eligible ? (
          <span className="text-ink">yes</span>
        ) : (
          <span className="text-declared" title={candidate.ineligible_reason ?? undefined}>
            no
          </span>
        )}
      </td>
      <td className="stat py-1 pr-3 text-right font-log text-[11px] text-declared">
        {candidate.declared_cpu_units}u &middot; {candidate.declared_slots}s
      </td>
      <td className="stat py-1 pr-3 text-right font-log text-[11px] text-reserved">
        {candidate.reserved_cpu_units}u &middot; {candidate.active_jobs}/{candidate.declared_slots}
      </td>
      {/* Always rendered, whether or not this policy could read it. A static
          policy's candidates carry observed: null -- that column present but
          empty, next to a populated reserved column, is the visible evidence
          the policy genuinely could not see the host. Omitting the column
          would let a reader assume the data was never collected, rather than
          never consulted. */}
      <td
        className={`stat py-1 pr-3 text-right font-log text-[11px] ${
          observedCpu === null ? "text-declared" : "text-observed"
        }`}
      >
        {observedCpu === null ? "—" : `${observedCpu.toFixed(0)}%`}
      </td>
      <td className="stat py-1 text-right font-log text-[11px] text-ink">
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
export function DecisionTrace({ decisions }: { decisions: Decision[] }) {
  const [openId, setOpenId] = useState<string | null>(null);

  if (decisions.length === 0) {
    return (
      <section>
        <h2 className="legend mb-3">Decision ledger</h2>
        <p className="font-log text-[12px] text-declared">no placements yet</p>
      </section>
    );
  }

  return (
    <section>
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="legend">Decision ledger</h2>
        <p className="legend text-declared">
          observed is recorded even when placement cannot read it
        </p>
      </div>

      <div className="divide-y divide-ink/15 border-y border-ink/20">
        {decisions.map((decision) => {
          const open = openId === decision.decision_id;
          return (
            <div key={decision.decision_id}>
              <button
                type="button"
                onClick={() => setOpenId(open ? null : decision.decision_id)}
                className="flex w-full items-center justify-between gap-3 py-2 text-left hover:bg-ink/5"
              >
                <span className="flex min-w-0 items-baseline gap-3">
                  <span className="font-log text-[12px] text-ink">
                    {decision.job_id.replace("job-", "")}
                  </span>
                  <span className="legend truncate text-declared">
                    {decision.placement_policy}
                  </span>
                </span>
                <span className="flex shrink-0 items-baseline gap-4">
                  {decision.selected_worker ? (
                    <span className="font-log text-[12px] text-ink">
                      {decision.selected_worker}
                    </span>
                  ) : (
                    <span className="legend text-observed">no candidate</span>
                  )}
                  {decision.margin === 0 && (
                    <span className="legend text-observed">tie-break</span>
                  )}
                  <span className="stat font-log text-[11px] text-declared">
                    {decision.decision_latency_us.toFixed(0)}&micro;s
                  </span>
                </span>
              </button>

              {open && (
                <div className="border-t border-ink/10 pb-3 pl-1">
                  <div className="overflow-x-auto">
                    <table className="w-full">
                      <thead>
                        <tr className="legend text-left text-declared">
                          <th className="pb-1 pt-2 font-normal">worker</th>
                          <th className="pb-1 pt-2 font-normal">eligible</th>
                          <th className="pb-1 pt-2 text-right font-normal text-declared">
                            declared
                          </th>
                          <th className="pb-1 pt-2 text-right font-normal text-reserved">
                            reserved
                          </th>
                          <th className="pb-1 pt-2 text-right font-normal text-observed">
                            observed
                          </th>
                          <th className="pb-1 pt-2 text-right font-normal">score</th>
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

                  <p className="mt-2 font-log text-[12px] leading-relaxed text-ink">
                    {decision.reason}
                  </p>

                  {Object.keys(decision.weights).length > 0 && (
                    <p className="mt-1.5 font-log text-[10px] text-declared">
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
