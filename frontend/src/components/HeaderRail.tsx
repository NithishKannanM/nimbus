import type { SchedulerState } from "../lib/api";

/**
 * A lamp on a legend plate. Filled when the condition holds, hollow when it
 * does not — readable without relying on colour alone, which matters because
 * the two lamps here mean different things and one of them is easy to
 * misread as the other.
 */
function Lamp({ on, children, title }: { on: boolean; children: string; title: string }) {
  return (
    <span className="flex items-center gap-1.5" title={title}>
      <span
        aria-hidden
        className={`inline-block h-2 w-2 border ${
          on ? "border-observed bg-observed" : "border-declared bg-transparent"
        }`}
      />
      <span className={`legend ${on ? "text-ink" : "text-declared"}`}>{children}</span>
    </span>
  );
}

export function HeaderRail({
  scheduler,
  readsTelemetry,
  stale,
}: {
  scheduler: SchedulerState;
  readsTelemetry: boolean;
  stale: boolean;
}) {
  return (
    <header className="relative z-10">
      <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-3">
        <div>
          <h1 className="font-legend text-[13px] font-700 uppercase tracking-legend text-ink">
            Nimbus
          </h1>
          {/* The strategy written as the composition it actually is. Ordering
              and placement are separate objects, and every implemented strategy
              holds ordering constant — so a difference between two of them is
              attributable to placement alone. Printing it as a sum keeps that
              visible instead of hiding it behind one name. */}
          <p className="mt-1.5 font-log text-[12px] text-ink">
            {scheduler.strategy}
            <span className="text-declared"> = </span>
            {scheduler.ordering_policy}
            <span className="text-declared"> + </span>
            {scheduler.placement_policy}
            <span className="text-declared"> · {scheduler.algorithm_version}</span>
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
          <Lamp
            on={scheduler.telemetry_enabled}
            title="Whether the collector is sampling the hosts at all."
          >
            {scheduler.telemetry_enabled ? "telemetry collecting" : "telemetry off"}
          </Lamp>
          <Lamp
            on={readsTelemetry}
            title={
              readsTelemetry
                ? "This policy receives a view carrying observed state, and its score depends on it."
                : "This policy receives a view with no telemetry fields on it at all. The red pen below is recorded, and cannot reach the placement decision."
            }
          >
            {readsTelemetry ? "placement reads it" : "placement cannot read it"}
          </Lamp>
          {stale && (
            <span className="border border-observed px-1.5 py-0.5 font-legend text-[10px] font-600 uppercase tracking-legend text-observed">
              api unreachable · last known
            </span>
          )}
        </div>
      </div>
    </header>
  );
}
