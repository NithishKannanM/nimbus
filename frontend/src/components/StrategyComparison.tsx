import { usePoll } from "../hooks/usePoll";
import { api, type StrategyLive } from "../lib/api";
import { InfoTip } from "./InfoTip";
import { Card, StaleChip } from "./SectionHead";

function duration(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${Math.round(s % 60)}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

const secs = (v: number | null) => (v === null ? "-" : `${v.toFixed(2)}s`);

/** Share of placements onto a saturated host, as a bar. Coral, because a job
 *  sent to a worker the host reports as saturated is the divergence the whole
 *  experiment is about. */
function HotShare({ row }: { row: StrategyLive }) {
  const share = row.placed > 0 ? row.placed_hot / row.placed : 0;
  return (
    <div className="flex min-w-[110px] items-center gap-2">
      <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
        <div
          role="meter"
          aria-label={`Placements onto a saturated worker, ${row.strategy}`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(share * 100)}
          className="h-full rounded-full bg-observed"
          style={{ width: `${share * 100}%` }}
        />
      </div>
      <span
        className={`w-[78px] shrink-0 text-right text-[13px] ${
          row.placed_hot > 0 ? "font-semibold text-observedText" : "text-ink"
        }`}
      >
        {row.placed_hot}/{row.placed} · {Math.round(share * 100)}%
      </span>
    </div>
  );
}

/**
 * Each strategy's live figures since the control plane started, grouped by the
 * strategy that placed each job. Switch strategy and a new row appears.
 *
 * Mean execution time is the figure placement moves: a job sent to a host
 * that is already saturated runs slower. Completed per minute mostly tracks
 * the arrival rate unless the fleet is saturated, so it is shown but not
 * leaned on.
 */
export function StrategyComparison({ active }: { active: string }) {
  const comparison = usePoll(api.comparison, 3000);
  const data = comparison.data;
  const rows = data?.strategies ?? [];

  // Widest bar = slowest mean execution, so the shorter bar is the better one.
  const slowest = Math.max(0, ...rows.map((r) => r.mean_exec_s ?? 0));

  return (
    <Card
      id="comparison"
      title="Live strategy comparison"
      subtitle={data ? `Since ${new Date(data.since).toLocaleTimeString()}` : undefined}
      info={
        <>
          Each job counts under the strategy that placed it. Live and uncontrolled: strategies ran
          one after another under whatever load was present, so this shows the mechanism, not
          proof. Look at execution time and saturated placements; per-minute throughput mostly
          follows the arrival rate. The controlled comparison is the archived runs.
        </>
      }
      action={comparison.failures > 1 ? <StaleChip /> : undefined}
    >
      {comparison.loading && !data ? (
        <p role="status" className="text-[14px] text-declared">Reading the comparison…</p>
      ) : rows.length === 0 ? (
        <p className="text-[14px] text-declared">No strategy has placed a job yet.</p>
      ) : (
        <div className="overflow-x-auto" tabIndex={0} role="region" aria-label="Live strategy comparison">
          <table className="data-table min-w-[640px]">
            <caption className="sr-only">
              Live per-strategy placement and execution figures
            </caption>
            <thead>
              <tr>
                <th scope="col">Strategy</th>
                <th scope="col" className="text-right">Active</th>
                <th scope="col">
                  <span className="inline-flex items-center gap-1.5">
                    To a busy worker
                    <InfoTip label="About busy workers">
                      Jobs placed on a worker whose observed CPU was at least {data!.hot_cpu_percent}%
                      when the decision was made. Lower is better.
                    </InfoTip>
                  </span>
                </th>
                <th scope="col" className="text-right">Completed</th>
                <th scope="col" className="text-right">/min</th>
                <th scope="col">Mean exec</th>
                <th scope="col" className="text-right">p95</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.strategy}>
                  <th scope="row" className="font-medium text-ink">
                    <span className="flex flex-col items-start gap-1">
                      <span className="break-all font-mono text-[12px]">{row.strategy}</span>
                      <span className="flex flex-wrap gap-1">
                        {row.strategy === active && <span className="chip-reserved">active</span>}
                        {row.reads_telemetry && <span className="chip-observed">telemetry</span>}
                      </span>
                    </span>
                  </th>
                  <td className="text-right">{duration(row.active_s)}</td>
                  <td>
                    <HotShare row={row} />
                  </td>
                  <td className="text-right font-medium text-ink">{row.completed}</td>
                  <td className="text-right">{row.completed_per_min ?? "-"}</td>
                  <td>
                    <div className="flex min-w-[110px] items-center gap-2">
                      <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
                        <div
                          className="h-full rounded-full bg-reserved"
                          style={{
                            width: `${slowest > 0 ? ((row.mean_exec_s ?? 0) / slowest) * 100 : 0}%`,
                          }}
                        />
                      </div>
                      <span className="w-[48px] shrink-0 text-right font-medium text-ink">
                        {secs(row.mean_exec_s)}
                      </span>
                    </div>
                  </td>
                  <td className="text-right">{secs(row.p95_exec_s)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

    </Card>
  );
}
