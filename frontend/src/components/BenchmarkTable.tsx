import { useMemo } from "react";

import { usePoll } from "../hooks/usePoll";
import { api, type BenchmarkProfile, type BenchmarkRunRow } from "../lib/api";
import { Card, StaleChip } from "./SectionHead";

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
  primary: "chip-reserved",
  descriptive: "chip-neutral",
  inadmissible: "chip-observed",
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

  const stale = runs.failures > 1 || profiles.failures > 1;

  return (
    <Card
      id="runs"
      title="Archived runs"
      subtitle="Benchmark runs on disk"
      info="The controlled comparison: same seeded workload and pressure, repeated runs. Full statistics live in docs/results.md."
      action={stale ? <StaleChip /> : undefined}
    >
      <div className="grid gap-8 2xl:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <div>
          <h3 className="mb-2 text-[15px] font-semibold text-ink">Evidence by profile</h3>
          {summary.length === 0 ? (
            <p className="text-[13px] text-declared">
              {runs.loading ? "Reading the archive…" : runs.error ? "API unreachable, archive not loaded." : "No archived runs on disk."}
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="data-table min-w-[360px]">
                <caption className="sr-only">Evidence class per benchmark profile</caption>
                <thead>
                  <tr>
                    <th scope="col">Profile</th>
                    <th scope="col" className="text-right">Runs</th>
                    <th scope="col" className="text-right">Admissible</th>
                    <th scope="col" className="text-right">Evidence</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.map((row) => (
                    <tr key={row.name}>
                      <td className="font-medium text-ink">{row.name}</td>
                      <td className="text-right">{row.total}</td>
                      <td className="text-right">{row.admissible}</td>
                      <td className="text-right">
                        <span className={BADGE_TONE[row.evidence]} title={BADGE_TITLE[row.evidence]}>
                          {row.evidence}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div>
          <h3 className="mb-2 text-[15px] font-semibold text-ink">Most recent runs</h3>
          <div className="overflow-x-auto">
            <table className="data-table min-w-[520px]">
              <caption className="sr-only">Most recent archived runs</caption>
              <thead>
                <tr>
                  <th scope="col">Run</th>
                  <th scope="col">Profile</th>
                  <th scope="col">Strategy</th>
                  <th scope="col" className="text-right">Jobs</th>
                  <th scope="col" className="text-right">Makespan</th>
                  <th scope="col" className="text-right">Flag</th>
                </tr>
              </thead>
              <tbody>
                {recent.map((run) => {
                  const flag = run.truncated ? "truncated" : !run.load_regime_ok ? "low load" : null;
                  return (
                    <tr key={run.run_id}>
                      <td className="font-mono text-[12px]">{run.run_id.replace("exp-", "")}</td>
                      <td className="text-ink">{run.profile}</td>
                      <td className="text-ink">{run.strategy}</td>
                      <td className="text-right">
                        {run.jobs_completed}/{run.jobs_submitted}
                      </td>
                      <td className="text-right">
                        {run.makespan_s === null ? "-" : `${run.makespan_s.toFixed(1)}s`}
                      </td>
                      <td className="text-right">
                        {flag ? <span className="chip-observed">{flag}</span> : "-"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {recent.length === 0 && (
            <p className="py-4 text-center text-[13px] text-declared">
              {runs.loading ? "Reading the archive…" : "No archived runs."}
            </p>
          )}
        </div>
      </div>
    </Card>
  );
}
