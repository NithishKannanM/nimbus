import type { WorkerState } from "../lib/api";
import { hatchUrl } from "../lib/theme";
import { useInk } from "../lib/themeMode";
import { IconServer } from "./Icons";
import { Card } from "./SectionHead";

type Kind = "declared" | "reserved" | "observed";

/** The bar track and fill for each description. Three different treatments,
 *  not three colours of one bar: declared is an outline with hatch (never a
 *  solid fill), reserved is solid blue, observed is solid coral. */
const TRACK: Record<Kind, string> = {
  declared: "border border-declared",
  reserved: "bg-muted",
  observed: "bg-muted",
};
const LABEL: Record<Kind, string> = {
  declared: "Declared",
  reserved: "Reserved",
  observed: "Observed",
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
  const INK = useInk();
  const clamped = pct === null ? null : Math.min(100, Math.max(0, pct));
  return (
    <div className="grid grid-cols-[72px_1fr_auto] items-center gap-3">
      <span className={`text-[13px] font-medium ${LABEL_TONE[kind]}`}>{LABEL[kind]}</span>
      <span
        className={`block h-2 overflow-hidden rounded-full ${TRACK[kind]}`}
        style={hatched ? { backgroundImage: hatchUrl(INK.declared, 6, 1) } : undefined}
        role={pct !== null ? "meter" : undefined}
        aria-label={pct !== null ? `${kind} ${Math.round(pct)} percent` : undefined}
        aria-valuemin={pct !== null ? 0 : undefined}
        aria-valuemax={pct !== null ? 100 : undefined}
        aria-valuenow={pct !== null ? Math.round(pct) : undefined}
      >
        {clamped !== null && (
          <span
            className={`block h-full rounded-full transition-[width] duration-150 motion-reduce:transition-none ${
              kind === "observed" ? "bg-observed" : "bg-reserved"
            }`}
            style={{ width: `${clamped}%` }}
          />
        )}
      </span>
      <span className="text-right font-mono text-[12px] text-declared" title={detail}>
        {value}
      </span>
    </div>
  );
}

export function FleetPlates({ workers }: { workers: WorkerState[] }) {
  return (
    <Card
      id="fleet"
      title="Fleet"
      subtitle="Declared, reserved and observed"
      info="Each worker described three ways: what the operator declared (hatched outline), what Nimbus has reserved (blue), and what the host is actually doing (coral)."
    >
      <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
        {workers.map((worker) => {
          const observed = worker.observed;
          const reservedPct =
            worker.declared.cpu_units > 0
              ? (worker.reserved.cpu_units / worker.declared.cpu_units) * 100
              : 0;
          const healthy = worker.status === "HEALTHY";
          return (
            <article key={worker.worker_id} className="rounded-card border border-hairline p-5">
              <header className="mb-4 flex items-center justify-between gap-3">
                <span className="flex items-center gap-3">
                  <span className="grid h-10 w-10 place-items-center rounded-full bg-reservedLight text-reserved">
                    <IconServer />
                  </span>
                  <span className="text-[15px] font-semibold text-ink">{worker.worker_id}</span>
                </span>
                <span className={healthy ? "chip-neutral" : "chip-observed"}>
                  {healthy ? "Healthy" : worker.status.toLowerCase()}
                </span>
              </header>

              <div className="mb-4">
                <span
                  className={`text-[28px] font-bold leading-none ${observed ? "text-observed" : "text-declared"}`}
                  aria-label={observed ? `observed ${observed.cpu_percent.toFixed(0)} percent` : "no observed sample"}
                >
                  {observed ? `${observed.cpu_percent.toFixed(0)}%` : "-"}
                </span>
                <span className="ml-2 text-[13px] text-declared">observed CPU</span>
              </div>

              <div className="space-y-3">
                <Row
                  kind="declared"
                  pct={null}
                  hatched
                  value={`${worker.declared.cpu_units}u · ${worker.declared.slots} slots`}
                  detail="Operator configuration. Static, and never inferred from telemetry."
                />
                <Row
                  kind="reserved"
                  pct={reservedPct}
                  value={`${worker.reserved.cpu_units}u · ${worker.reserved.active_jobs}/${worker.declared.slots}`}
                  detail="What Nimbus has committed. Its own bookkeeping, and the only capacity signal a static policy gets."
                />
                <Row
                  kind="observed"
                  pct={observed ? observed.cpu_percent : null}
                  value={observed ? `${observed.cpu_percent.toFixed(0)}% cpu` : "no sample"}
                  detail="What the host is actually doing, per psutil. Recorded in every decision trace whether or not the policy may read it."
                />
              </div>

              <footer className="mt-4 border-t border-hairline pt-3 text-[12px] text-declared">
                Heartbeat {worker.heartbeat_age_s.toFixed(1)}s
                {worker.telemetry_age_s !== null &&
                  ` · sample ${worker.telemetry_age_s.toFixed(1)}s`}
              </footer>
            </article>
          );
        })}
      </div>
    </Card>
  );
}
