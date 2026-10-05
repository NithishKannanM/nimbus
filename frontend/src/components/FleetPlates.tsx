import type { WorkerState } from "../lib/api";
import { INK } from "../lib/theme";

/**
 * Legend plates: the three descriptions of a worker, engraved separately.
 *
 * Deliberately three rows rather than one merged utilisation figure. DECLARED
 * is what the operator configured, RESERVED is what Nimbus has committed, and
 * OBSERVED is what the host is doing — and the experiment turns entirely on
 * their being allowed to disagree. A single blended bar would be the one
 * rendering that makes the whole project unreadable.
 */
function Row({
  label,
  value,
  detail,
  colour,
  pct,
}: {
  label: string;
  value: string;
  detail?: string;
  colour: string;
  pct: number | null;
}) {
  return (
    <div className="grid grid-cols-[64px_1fr_auto] items-center gap-3">
      <span className="legend" style={{ color: colour }}>
        {label}
      </span>
      <span
        className="h-[3px] bg-ink/10"
        role={pct !== null ? "meter" : undefined}
        aria-label={pct !== null ? `${label} ${Math.round(pct)} percent` : undefined}
        aria-valuemin={pct !== null ? 0 : undefined}
        aria-valuemax={pct !== null ? 100 : undefined}
        aria-valuenow={pct !== null ? Math.round(pct) : undefined}
      >
        {pct !== null && (
          <span
            className="block h-full transition-[width] duration-700"
            style={{ width: `${Math.min(100, Math.max(0, pct))}%`, background: colour }}
          />
        )}
      </span>
      <span className="font-log text-[11px] text-ink" title={detail}>
        {value}
      </span>
    </div>
  );
}

export function FleetPlates({ workers }: { workers: WorkerState[] }) {
  return (
    <section aria-labelledby="fleet-h">
      <h2 id="fleet-h" className="legend mb-3 text-ink">Fleet</h2>
      {/* Hairlines are borders, not a tinted gap: with 3 workers on a 2-column
          row, a gap-px background would show through the empty cell. */}
      <div className="grid border-l border-t border-ink/15 sm:grid-cols-2 lg:grid-cols-3">
        {workers.map((worker) => {
          const observed = worker.observed;
          const reservedPct =
            worker.declared.cpu_units > 0
              ? (worker.reserved.cpu_units / worker.declared.cpu_units) * 100
              : 0;
          return (
            <article key={worker.worker_id} className="border-b border-r border-ink/15 bg-paper p-4">
              <header className="mb-3 flex items-baseline justify-between border-b border-ink/15 pb-2">
                <span className="font-log text-[13px] text-ink">{worker.worker_id}</span>
                <span
                  className={`legend ${
                    worker.status === "HEALTHY" ? "text-declared" : "text-observed"
                  }`}
                >
                  {worker.status}
                </span>
              </header>

              <div className="space-y-2">
                <Row
                  label="declared"
                  colour={INK.declared}
                  pct={null}
                  value={`${worker.declared.cpu_units} u · ${worker.declared.slots} slots`}
                  detail="Operator configuration. Static, and never inferred from telemetry."
                />
                <Row
                  label="reserved"
                  colour={INK.reserved}
                  pct={reservedPct}
                  value={`${worker.reserved.cpu_units} u · ${worker.reserved.active_jobs}/${worker.declared.slots}`}
                  detail="What Nimbus has committed. Its own bookkeeping, and the only capacity signal a static policy gets."
                />
                <Row
                  label="observed"
                  colour={INK.observed}
                  pct={observed ? observed.cpu_percent : null}
                  value={observed ? `${observed.cpu_percent.toFixed(0)}% cpu` : "no sample"}
                  detail="What the host is actually doing, per psutil. Recorded in every decision trace whether or not the policy may read it."
                />
              </div>

              <footer className="mt-3 border-t border-ink/10 pt-2 font-log text-[11px] text-declared">
                heartbeat {worker.heartbeat_age_s.toFixed(1)}s
                {worker.telemetry_age_s !== null &&
                  ` · sample ${worker.telemetry_age_s.toFixed(1)}s`}
              </footer>
            </article>
          );
        })}
      </div>
    </section>
  );
}
