import type { WorkerState } from "../lib/api";
import { INK, hatchUrl } from "../lib/theme";
import { SectionHead } from "./SectionHead";

type Kind = "declared" | "reserved" | "observed";

/** The bar track and fill for each description. Three different treatments,
 *  not three colours of one bar: declared is an outline with hatch (never a
 *  solid fill), reserved is solid carbon, observed is solid hazard. */
const TRACK: Record<Kind, string> = {
  declared: "border-2 border-declared",
  reserved: "border-2 border-ink",
  observed: "border-2 border-observed",
};
const LABEL_TONE: Record<Kind, string> = {
  declared: "text-declared",
  reserved: "text-ink",
  observed: "text-observedText",
};

/**
 * Legend plates: the three descriptions of a worker, printed separately.
 *
 * Deliberately three rows rather than one merged utilisation figure. DECLARED
 * is what the operator configured, RESERVED is what Nimbus has committed, and
 * OBSERVED is what the host is doing -- and the experiment turns entirely on
 * their being allowed to disagree. A single blended bar would be the one
 * rendering that makes the whole project unreadable.
 */
function Row({
  kind,
  value,
  detail,
  pct,
  hatched = false,
}: {
  kind: Kind;
  value: string;
  detail?: string;
  pct: number | null;
  hatched?: boolean;
}) {
  const clamped = pct === null ? null : Math.min(100, Math.max(0, pct));
  return (
    <div className="grid grid-cols-[72px_1fr_auto] items-center gap-3">
      <span className={`label ${LABEL_TONE[kind]}`}>{kind}</span>
      <span
        className={`block h-3 ${TRACK[kind]}`}
        style={hatched ? { backgroundImage: hatchUrl(INK.declared, 6, 1) } : undefined}
        role={pct !== null ? "meter" : undefined}
        aria-label={pct !== null ? `${kind} ${Math.round(pct)} percent` : undefined}
        aria-valuemin={pct !== null ? 0 : undefined}
        aria-valuemax={pct !== null ? 100 : undefined}
        aria-valuenow={pct !== null ? Math.round(pct) : undefined}
      >
        {clamped !== null && (
          <span
            className={`block h-full transition-[width] duration-150 motion-reduce:transition-none ${
              kind === "observed" ? "bg-observed" : "bg-ink"
            }`}
            style={{ width: `${clamped}%` }}
          />
        )}
      </span>
      <span className="font-mono text-[12px] text-ink" title={detail}>
        {value}
      </span>
    </div>
  );
}

export function FleetPlates({ workers }: { workers: WorkerState[] }) {
  return (
    <section aria-labelledby="fleet-h">
      <SectionHead n="02" id="fleet-h">Fleet</SectionHead>
      {/* Plate dividers are borders, not a black gap: with 3 workers on a
          2-column row, a gap-px background would show through the empty cell. */}
      <div className="grid border-l-2 border-t-2 border-ink sm:grid-cols-2 lg:grid-cols-3">
        {workers.map((worker, i) => {
          const observed = worker.observed;
          const reservedPct =
            worker.declared.cpu_units > 0
              ? (worker.reserved.cpu_units / worker.declared.cpu_units) * 100
              : 0;
          const healthy = worker.status === "HEALTHY";
          return (
            <article key={worker.worker_id} className="border-b-2 border-r-2 border-ink bg-paper p-4">
              <header className="mb-3 flex items-baseline justify-between gap-3 border-b border-ink pb-2">
                <span className="flex items-baseline gap-2">
                  <span aria-hidden className="display text-xl text-declared">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <span className="font-mono text-[13px] font-semibold text-ink">{worker.worker_id}</span>
                </span>
                <span
                  className={`label ${healthy ? "text-declared" : "font-semibold text-observedText"}`}
                >
                  [ {worker.status} ]
                </span>
              </header>

              <div className="mb-3 flex items-end justify-between gap-3">
                <span className="label text-declared">observed cpu</span>
                <span
                  className={`display text-5xl ${observed ? "text-observed" : "text-declared"}`}
                  aria-label={observed ? `observed ${observed.cpu_percent.toFixed(0)} percent` : "no observed sample"}
                >
                  {observed ? `${observed.cpu_percent.toFixed(0)}%` : "——"}
                </span>
              </div>

              <div className="space-y-2.5">
                <Row
                  kind="declared"
                  pct={null}
                  hatched
                  value={`${worker.declared.cpu_units} u · ${worker.declared.slots} slots`}
                  detail="Operator configuration. Static, and never inferred from telemetry."
                />
                <Row
                  kind="reserved"
                  pct={reservedPct}
                  value={`${worker.reserved.cpu_units} u · ${worker.reserved.active_jobs}/${worker.declared.slots}`}
                  detail="What Nimbus has committed. Its own bookkeeping, and the only capacity signal a static policy gets."
                />
                <Row
                  kind="observed"
                  pct={observed ? observed.cpu_percent : null}
                  value={observed ? `${observed.cpu_percent.toFixed(0)}% cpu` : "no sample"}
                  detail="What the host is actually doing, per psutil. Recorded in every decision trace whether or not the policy may read it."
                />
              </div>

              <footer className="label mt-3 border-t border-hairline pt-2 normal-case tracking-normal text-declared">
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
