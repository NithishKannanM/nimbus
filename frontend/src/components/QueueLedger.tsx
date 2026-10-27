import type { SchedulerState } from "../lib/api";
import { InfoTip } from "./InfoTip";
import { Card } from "./SectionHead";

function Row({
  label,
  value,
  hint,
  divergent = false,
}: {
  label: string;
  value: number;
  hint?: string;
  divergent?: boolean;
}) {
  return (
    <li className="flex items-center justify-between gap-3 py-3">
      <span className="flex items-center gap-2 text-[14px] text-ink">
        {label}
        {hint && <InfoTip label={`About ${label}`}>{hint}</InfoTip>}
      </span>
      <span
        className={`text-[16px] font-semibold ${divergent ? "text-observedText" : "text-ink"}`}
      >
        {value}
      </span>
    </li>
  );
}

/**
 * The queue as a snapshot, not a chart.
 *
 * Friction counters (retried, failed, dead-lettered, no-candidate) take the
 * accent exactly like every other divergence from the expected path on this
 * page, at the same size as the happy path, never smaller or hidden. Three
 * Phase 3 runs once silently completed 59 of 60 jobs and looked completely
 * ordinary; a friction counter that is easy to overlook is worse than useless.
 */
export function QueueLedger({ scheduler }: { scheduler: SchedulerState }) {
  const problems = scheduler.failed + scheduler.worker_lost + scheduler.dead_letters;

  return (
    <Card id="queue" title="Queue" subtitle="Point-in-time counters" className="h-full">
      <p className="eyebrow text-declared">In the system</p>
      <ul className="mb-3 divide-y divide-hairline">
        <Row label="Visible" value={scheduler.queue_visible} hint="Waiting to be leased." />
        <Row
          label="In flight"
          value={scheduler.queue_in_flight}
          hint="Leased or delayed. Owned but not runnable right now."
        />
        <Row label="Completed" value={scheduler.completed} />
      </ul>

      <p className="eyebrow text-declared">Friction</p>
      <ul className="divide-y divide-hairline">
        <Row
          label="Retried"
          value={scheduler.retried}
          divergent={scheduler.retried > 0}
          hint="Attempts that failed and were re-placed. Not an error on its own."
        />
        <Row
          label="Failed"
          value={scheduler.failed}
          divergent={scheduler.failed > 0}
          hint="Terminal: the retry budget was exhausted, or the message was dead-lettered."
        />
        <Row
          label="Dead-lettered"
          value={scheduler.dead_letters}
          divergent={scheduler.dead_letters > 0}
          hint="The queue gave up delivering. Any measurement taken during this window is suspect."
        />
        <Row
          label="No candidate"
          value={scheduler.no_candidate}
          divergent={scheduler.no_candidate > 0}
          hint="No eligible worker at decision time."
        />
      </ul>

      {problems > 0 && (
        <p className="mt-4 rounded-card bg-observedLight px-4 py-3 text-[13px] leading-relaxed text-observedText">
          {scheduler.failed} failed &middot; {scheduler.worker_lost} reclaimed from lost
          workers &middot; {scheduler.dead_letters} dead-lettered. A benchmark run
          overlapping this window is not clean evidence.
        </p>
      )}
    </Card>
  );
}
