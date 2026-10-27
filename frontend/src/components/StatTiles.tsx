import type { ReactNode } from "react";

import type { SchedulerState, WorkerState } from "../lib/api";
import type { Lane } from "./DivergenceStrip";
import { IconActivity, IconCheck, IconServer, IconStack } from "./Icons";

function Tile({
  icon,
  label,
  value,
  foot,
  accent = false,
}: {
  icon: ReactNode;
  label: string;
  value: ReactNode;
  foot: ReactNode;
  accent?: boolean;
}) {
  return (
    <div className="card flex items-start gap-4 p-6">
      <span
        className={`grid h-12 w-12 shrink-0 place-items-center rounded-full ${
          accent ? "bg-observedLight text-observed" : "bg-reservedLight text-reserved"
        }`}
      >
        {icon}
      </span>
      <div className="min-w-0">
        <p className="text-[14px] text-declared">{label}</p>
        <p
          className={`mt-1 text-[24px] font-semibold leading-tight ${
            accent ? "text-observedText" : "text-ink"
          }`}
        >
          {value}
        </p>
        <p className="mt-1 text-[12px] text-declared">{foot}</p>
      </div>
    </div>
  );
}

/**
 * Summary row. Every figure is read from the same polls the panels below use;
 * the divergence tile reads the strip's own lanes, and takes the accent only
 * when the Finding would call it a divergence.
 */
export function StatTiles({
  workers,
  scheduler,
  lanes,
  diverged,
}: {
  workers: WorkerState[];
  scheduler: SchedulerState;
  lanes: Lane[];
  diverged: boolean;
}) {
  const healthy = workers.filter((w) => w.status === "HEALTHY").length;

  let widest: { workerId: string; gap: number } | null = null;
  for (const lane of lanes) {
    const latest = lane.readings[lane.readings.length - 1];
    if (!latest || latest.observed === null) continue;
    const gap = latest.observed - latest.reserved;
    if (!widest || Math.abs(gap) > Math.abs(widest.gap)) widest = { workerId: lane.workerId, gap };
  }

  const friction =
    scheduler.retried + scheduler.failed + scheduler.dead_letters + scheduler.no_candidate;

  return (
    <div className="grid gap-6 sm:grid-cols-2 xl:grid-cols-4">
      <Tile
        icon={<IconServer className="h-6 w-6" />}
        label="Healthy workers"
        value={`${healthy} / ${workers.length}`}
        foot={healthy === workers.length ? "All heartbeats current" : `${workers.length - healthy} not healthy`}
        accent={healthy < workers.length}
      />
      <Tile
        icon={<IconActivity className="h-6 w-6" />}
        label="Widest divergence"
        value={
          !scheduler.telemetry_enabled ? "-" : widest ? `${Math.abs(widest.gap).toFixed(0)} pts` : "…"
        }
        foot={
          !scheduler.telemetry_enabled
            ? "Telemetry off"
            : widest
              ? `${widest.workerId}, observed ${widest.gap >= 0 ? "above" : "below"} reserved`
              : "Waiting for samples"
        }
        accent={diverged}
      />
      <Tile
        icon={<IconStack className="h-6 w-6" />}
        label="Queue"
        value={scheduler.queue_visible + scheduler.queue_in_flight}
        foot={`${scheduler.queue_visible} visible · ${scheduler.queue_in_flight} in flight`}
      />
      <Tile
        icon={<IconCheck className="h-6 w-6" />}
        label="Completed jobs"
        value={scheduler.completed}
        foot={friction === 0 ? "No friction events" : `${friction} friction events`}
        accent={friction > 0}
      />
    </div>
  );
}
