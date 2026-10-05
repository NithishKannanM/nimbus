import type { SchedulerState } from "../lib/api";
import { INK, stripeUrl } from "../lib/theme";

/** A bracketed metadata cell: [ LABEL value ]. */
function Cell({
  label,
  children,
  title,
}: {
  label: string;
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <p className="label flex items-baseline gap-2 text-ink" title={title}>
      <span aria-hidden>[</span>
      <span className="text-declared">{label}</span>
      <span className="normal-case tracking-normal">{children}</span>
      <span aria-hidden>]</span>
    </p>
  );
}

/**
 * A square lamp: filled when the condition holds, hollow when it does not,
 * so it reads without relying on colour alone.
 */
function Lamp({ on, children, title }: { on: boolean; children: string; title: string }) {
  return (
    <span className="label flex items-center gap-2" title={title}>
      <span
        aria-hidden
        className={`inline-block h-2.5 w-2.5 border-2 ${
          on ? "border-observed bg-observed" : "border-declared bg-transparent"
        }`}
      />
      <span className={on ? "text-ink" : "text-declared"}>{children}</span>
    </span>
  );
}

export function HeaderRail({
  scheduler,
  readsTelemetry,
  stale,
  workerCount,
  diverged,
}: {
  scheduler: SchedulerState;
  readsTelemetry: boolean;
  stale: boolean;
  workerCount: number;
  /** Derived from the same lanes the Finding reads. */
  diverged: boolean;
}) {
  return (
    <header>
      <div className="flex flex-wrap items-end justify-between gap-x-10 gap-y-4 pb-4">
        <h1 className="display masthead-title -ml-[0.04em]">Nimbus</h1>

        <div className="flex min-w-0 flex-col gap-1.5 sm:max-w-[34rem]">
          {/* The strategy written as the composition it actually is. Ordering
              and placement are separate objects, and every implemented strategy
              holds ordering constant, so a difference between two of them is
              attributable to placement alone. */}
          <Cell label="strategy">
            {scheduler.strategy}
            <span className="text-declared"> = </span>
            {scheduler.ordering_policy}
            <span className="text-declared"> + </span>
            {scheduler.placement_policy}
          </Cell>
          <Cell label="policy version">{scheduler.algorithm_version}</Cell>
          <Cell label="workers">{workerCount}</Cell>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-1.5 pt-1">
            <Lamp
              on={scheduler.telemetry_enabled}
              title="Whether the collector is sampling the hosts at all."
            >
              {scheduler.telemetry_enabled ? "[ telemetry collecting ]" : "[ telemetry off ]"}
            </Lamp>
            <Lamp
              on={readsTelemetry}
              title={
                readsTelemetry
                  ? "This policy receives a view carrying observed state, and its score depends on it."
                  : "This policy receives a view with no telemetry fields on it at all. The observed trace below is recorded, and cannot reach the placement decision."
              }
            >
              {readsTelemetry ? "[ placement reads it ]" : "[ placement cannot read it ]"}
            </Lamp>
          </div>
          {stale && (
            <span
              role="status"
              className="label w-fit border-2 border-observed px-1.5 py-0.5 font-semibold text-observedText"
            >
              [ api unreachable · last known ]
            </span>
          )}
        </div>
      </div>

      {diverged && (
        <div
          aria-hidden
          className="h-4 border-y-2 border-ink"
          style={{ backgroundImage: stripeUrl(INK.observed) }}
        />
      )}
    </header>
  );
}
