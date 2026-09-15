import type { SchedulerState } from "../lib/api";

function Stat({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: number;
  tone: string;
  hint?: string;
}) {
  return (
    <span className="flex items-baseline gap-1.5" title={hint}>
      <span className={`stat font-meter text-[15px] ${tone}`}>{value}</span>
      <span className="legend text-declared">{label}</span>
    </span>
  );
}

/**
 * The queue as a snapshot, not a chart.
 *
 * SchedulerState is a point-in-time set of counters, and a snapshot reads best
 * as a snapshot -- the same legend-plate family FleetPlates already uses,
 * rather than a client-accumulated time series competing with the divergence
 * strip for the page's one "this changes over time" story.
 *
 * Friction counters (retried, failed, dead-lettered, no-candidate,
 * dispatch-rejected) are ink'd exactly like every other divergence from the
 * expected path elsewhere on this page: red, at the same size as the happy
 * path, never smaller or hidden. Three Phase 3 runs once silently completed 59
 * of 60 jobs and looked completely ordinary; a friction counter that is easy
 * to overlook is worse than useless.
 */
export function QueueLedger({ scheduler }: { scheduler: SchedulerState }) {
  const problems = scheduler.failed + scheduler.worker_lost + scheduler.dead_letters;

  return (
    <div className="plate p-4">
      <h2 className="legend mb-3">Queue</h2>

      <div className="space-y-2.5">
        <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
          <span className="legend w-24 shrink-0 text-declared">in the system</span>
          <Stat
            label="visible"
            value={scheduler.queue_visible}
            tone="text-reserved"
            hint="Waiting to be leased."
          />
          <Stat
            label="in flight"
            value={scheduler.queue_in_flight}
            tone="text-reserved"
            hint="Leased or delayed. Owned but not runnable right now."
          />
        </div>

        <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
          <span className="legend w-24 shrink-0 text-declared">resolved</span>
          <Stat label="completed" value={scheduler.completed} tone="text-ink" />
        </div>

        <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
          <span className="legend w-24 shrink-0 text-declared">friction</span>
          <Stat
            label="retried"
            value={scheduler.retried}
            tone={scheduler.retried ? "text-observed" : "text-ink"}
            hint="Attempts that failed and were re-placed. Not an error on its own."
          />
          <Stat
            label="failed"
            value={scheduler.failed}
            tone={scheduler.failed ? "text-observed" : "text-ink"}
            hint="Terminal: the retry budget was exhausted, or the message was dead-lettered."
          />
          <Stat
            label="dead-lettered"
            value={scheduler.dead_letters}
            tone={scheduler.dead_letters ? "text-observed" : "text-ink"}
            hint="The queue gave up delivering. Any measurement taken during this window is suspect."
          />
          <Stat
            label="no candidate"
            value={scheduler.no_candidate}
            tone={scheduler.no_candidate ? "text-observed" : "text-ink"}
            hint="No eligible worker at decision time."
          />
        </div>
      </div>

      {problems > 0 && (
        <p className="mt-3 border border-observed px-2 py-1 font-log text-[11px] leading-relaxed text-observed">
          {scheduler.failed} failed &middot; {scheduler.worker_lost} reclaimed from lost
          workers &middot; {scheduler.dead_letters} dead-lettered &mdash; a benchmark run
          overlapping this window is not clean evidence.
        </p>
      )}
    </div>
  );
}
