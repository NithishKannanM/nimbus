import { useMemo } from "react";

import { usePoll } from "../hooks/usePoll";
import { api, type BenchmarkProfile, type BenchmarkRunRow } from "../lib/api";

type EvidenceClass = "primary" | "descriptive" | "inadmissible";

/**
 * A direct port of the same rule `scripts/generate_report.py` and
 * `benchmark/report.py`'s `low_load_dominates()` apply to the archive when
 * `docs/results.md` is generated. `GET /benchmarks/report` only exposes a
 * two-way `evidence_class` (primary/descriptive); the third case --
 * genuinely low load, no external pressure to explain it -- only exists
 * inside the report-generation script and isn't returned by any endpoint.
 * Reconstructing it here from fields `/benchmarks/runs` and
 * `/benchmarks/profiles` already expose means this badge cannot silently
 * drift from what the written report says about the same archive.
 */
function evidenceClass(runs: BenchmarkRunRow[], hasPressure: boolean): EvidenceClass {
  if (runs.length === 0) return "inadmissible";
  const admissible = runs.filter((r) => !r.truncated && r.load_regime_ok).length;
  const ratio = admissible / runs.length;
  if (hasPressure && ratio < 0.5) return "descriptive";
  if (ratio < 0.5) return "inadmissible";
  return "primary";
}

const BADGE_TONE: Record<EvidenceClass, string> = {
  primary: "text-ink",
  descriptive: "text-declared",
  inadmissible: "text-observed",
};

const BADGE_TITLE: Record<EvidenceClass, string> = {
  primary: "Passes the load-regime gate. Usable as evidence.",
  descriptive:
    "The load gate excludes most of these runs, but this profile applies external pressure -- a scheduler that successfully steers work away from a saturated worker leaves it looking idle, which is a known mis-fire, not a sign the runs were too light. Reported as boundary-condition evidence, not primary.",
  inadmissible:
    "Most runs fall below the load-regime gate and this profile applies no pressure to explain it -- genuinely low load. The static baseline degenerates into its tie-break here, so this run set cannot support a comparison.",
};

/**
 * The archive: real experimental runs already on disk, never fabricated.
 *
 * Deliberately does not surface p-values, effect sizes, or tie-break rates --
 * that is a paper's work (`docs/results.md`), not a glance panel for a live
 * demo. This panel's job is to prove the runs are real and to carry forward
 * the same evidence-class honesty the written report insists on, not to
 * re-derive its conclusions inline.
 */
export function BenchmarkTable() {
  const runs = usePoll(api.benchmarkRuns, 15000);
  const profiles = usePoll(api.profiles, 15000);

  const summary = useMemo(() => {
    const rows = runs.data ?? [];
    const profileList = profiles.data ?? [];
    const byProfile = new Map<string, BenchmarkRunRow[]>();
    for (const row of rows) {
      const bucket = byProfile.get(row.profile) ?? [];
      bucket.push(row);
      byProfile.set(row.profile, bucket);
    }
    return [...byProfile.entries()]
      .map(([name, profileRuns]) => {
        const profile = profileList.find((p: BenchmarkProfile) => p.name === name);
        const hasPressure = (profile?.pressure.length ?? 0) > 0;
        const admissible = profileRuns.filter((r) => !r.truncated && r.load_regime_ok).length;
        return {
          name,
          total: profileRuns.length,
          admissible,
          evidence: evidenceClass(profileRuns, hasPressure),
        };
      })
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [runs.data, profiles.data]);

  const recent = [...(runs.data ?? [])]
    .sort((a, b) => b.started_at.localeCompare(a.started_at))
    .slice(0, 15);

  return (
    <div className="plate p-4">
      <h2 className="legend mb-3">Archived runs</h2>

      {summary.length === 0 ? (
        <p className="font-log text-[11px] text-declared">no archived runs on disk</p>
      ) : (
        <table className="mb-4 w-full">
          <thead>
            <tr className="legend text-left text-declared">
              <th className="pb-1 font-normal">profile</th>
              <th className="pb-1 text-right font-normal">runs</th>
              <th className="pb-1 text-right font-normal">admissible</th>
              <th className="pb-1 text-right font-normal">evidence</th>
            </tr>
          </thead>
          <tbody>
            {summary.map((row) => (
              <tr key={row.name} className="border-t border-ink/10">
                <td className="py-1 pr-2 font-log text-[11px] text-ink">{row.name}</td>
                <td className="stat py-1 pr-2 text-right font-log text-[11px] text-declared">
                  {row.total}
                </td>
                <td className="stat py-1 pr-2 text-right font-log text-[11px] text-declared">
                  {row.admissible}
                </td>
                <td className="py-1 text-right">
                  <span
                    className={`legend ${BADGE_TONE[row.evidence]}`}
                    title={BADGE_TITLE[row.evidence]}
                  >
                    {row.evidence}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <table className="w-full">
        <thead>
          <tr className="legend text-left text-declared">
            <th className="pb-1 font-normal">run</th>
            <th className="pb-1 font-normal">profile</th>
            <th className="pb-1 font-normal">strategy</th>
            <th className="pb-1 text-right font-normal">jobs</th>
            <th className="pb-1 text-right font-normal">makespan</th>
            <th className="pb-1 text-right font-normal">flag</th>
          </tr>
        </thead>
        <tbody>
          {recent.map((run) => {
            const flag = run.truncated ? "truncated" : !run.load_regime_ok ? "low load" : null;
            return (
              <tr key={run.run_id} className="border-t border-ink/10">
                <td className="py-1 pr-2 font-log text-[11px] text-declared">
                  {run.run_id.replace("exp-", "")}
                </td>
                <td className="py-1 pr-2 font-log text-[11px] text-ink">{run.profile}</td>
                <td className="py-1 pr-2 font-log text-[11px] text-ink">{run.strategy}</td>
                <td className="stat py-1 pr-2 text-right font-log text-[11px] text-declared">
                  {run.jobs_completed}/{run.jobs_submitted}
                </td>
                <td className="stat py-1 pr-2 text-right font-log text-[11px] text-declared">
                  {run.makespan_s === null ? "—" : `${run.makespan_s.toFixed(1)}s`}
                </td>
                <td
                  className={`py-1 text-right font-log text-[11px] ${
                    flag ? "text-observed" : "text-declared"
                  }`}
                >
                  {flag ?? "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {recent.length === 0 && (
        <p className="py-4 text-center font-log text-[11px] text-declared">no archived runs</p>
      )}

      <p className="mt-3 font-log text-[10px] text-declared">
        full statistical comparisons: docs/results.md
      </p>
    </div>
  );
}
