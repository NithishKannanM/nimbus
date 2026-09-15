import type { WorkerState } from "../lib/api";

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
      <span className="h-[3px] bg-ink/8">
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
    <section>
      <h2 className="legend mb-3 text-ink">Fleet</h2>
      <div className="grid gap-px bg-ink/15 sm:grid-cols-2 lg:grid-cols-3">
        {workers.map((worker) => {
          const observed = worker.observed;
          const reservedPct =
            worker.declared.cpu_units > 0
              ? (worker.reserved.cpu_units / worker.declared.cpu_units) * 100
              : 0;
          return (
            <article key={worker.worker_id} className="bg-paper p-4">
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
                  colour="#6B7169"
                  pct={null}
                  value={`${worker.declared.cpu_units} u · ${worker.declared.slots} slots`}
                  detail="Operator configuration. Static, and never inferred from telemetry."
                />
                <Row
                  label="reserved"
                  colour="#1B4D8F"
                  pct={reservedPct}
                  value={`${worker.reserved.cpu_units} u · ${worker.reserved.active_jobs}/${worker.declared.slots}`}
                  detail="What Nimbus has committed. Its own bookkeeping, and the only capacity signal a static policy gets."
                />
                <Row
                  label="observed"
                  colour="#C2410C"
                  pct={observed ? observed.cpu_percent : null}
                  value={observed ? `${observed.cpu_percent.toFixed(0)}% cpu` : "no sample"}
                  detail="What the host is actually doing, per psutil. Recorded in every decision trace whether or not the policy may read it."
                />
              </div>

              <footer className="mt-3 border-t border-ink/10 pt-2 font-log text-[10px] text-declared">
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
