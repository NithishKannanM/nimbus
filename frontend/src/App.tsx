import { useCallback, useEffect, useRef, useState } from "react";

import { BenchmarkTable } from "./components/BenchmarkTable";
import { DecisionTrace } from "./components/DecisionTrace";
import { DivergenceStrip, type Lane, type Reading } from "./components/DivergenceStrip";
import { Finding } from "./components/Finding";
import { FleetPlates } from "./components/FleetPlates";
import { HeaderRail } from "./components/HeaderRail";
import { JobsLog } from "./components/JobsLog";
import { QueueLedger } from "./components/QueueLedger";
import { StrategySwitch } from "./components/StrategySwitch";
import { usePoll } from "./hooks/usePoll";
import { WINDOW_S } from "./lib/theme";
import { api, placementReadsTelemetry } from "./lib/api";

/** One sample per second. A recorder's paper speed, not a poll interval chosen
 *  to be polite — the strip is the page, so it gets the fast loop. */
const PAPER_MS = 1000;

export default function App() {
  const workers = usePoll(api.workers, PAPER_MS);
  const scheduler = usePoll(api.scheduler, 2000);
  const decisions = usePoll(
    useCallback((signal?: AbortSignal) => api.decisions(25, signal), []),
    3000,
  );

  const [lanes, setLanes] = useState<Record<string, Reading[]>>({});
  const since = useRef(
    new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
  );

  // Both pens accumulate from the same poll, so a reading in one series always
  // has a counterpart in the other at the same instant. Seeding the red pen
  // from /workers/{id}/telemetry would give a longer trace on first paint, but
  // the blue pen has no history endpoint to match it — and drawing a gap that
  // is really "we were not watching yet" as though it were a measured
  // divergence is exactly the error this whole strip exists to expose.
  useEffect(() => {
    if (!workers.data) return;
    setLanes((previous) => {
      const next: Record<string, Reading[]> = {};
      for (const worker of workers.data!) {
        const reserved =
          worker.declared.cpu_units > 0
            ? (worker.reserved.cpu_units / worker.declared.cpu_units) * 100
            : 0;
        const reading: Reading = {
          reserved,
          observed: worker.observed ? worker.observed.cpu_percent : null,
        };
        next[worker.worker_id] = [...(previous[worker.worker_id] ?? []), reading].slice(-WINDOW_S);
      }
      return next;
    });
  }, [workers.data]);

  if (workers.loading && scheduler.loading) {
    return (
      <main className="relative z-10 grid min-h-screen place-items-center">
        <p className="legend text-declared">connecting to the control plane</p>
      </main>
    );
  }

  if (!scheduler.data || !workers.data) {
    return (
      <main className="relative z-10 grid min-h-screen place-items-center px-6">
        <div className="max-w-md">
          <p className="legend mb-3 text-observed">no control plane</p>
          <p className="font-log text-[13px] leading-relaxed text-ink">
            Start it with{" "}
            <span className="text-reserved">uvicorn backend.api.app:app</span>, or
            point the dev proxy elsewhere with{" "}
            <span className="text-reserved">NIMBUS_API</span>.
          </p>
        </div>
      </main>
    );
  }

  const policy = scheduler.data.placement_policy;
  const readsTelemetry = placementReadsTelemetry(policy);
  const stale = scheduler.failures > 1 || workers.failures > 1;

  const strip: Lane[] = workers.data.map((worker) => ({
    workerId: worker.worker_id,
    readings: lanes[worker.worker_id] ?? [],
    healthy: worker.status === "HEALTHY",
  }));

  return (
    <main className="relative z-10 mx-auto max-w-[1180px] px-6 py-7">
      <div className="border-b border-ink/20 pb-4">
        <HeaderRail
          scheduler={scheduler.data}
          readsTelemetry={readsTelemetry}
          stale={stale}
        />
        <StrategySwitch
          available={scheduler.data.available}
          active={scheduler.data.strategy}
          onSwitched={scheduler.refresh}
        />
      </div>

      <section className="mt-8">
        <div className="mb-4 flex flex-wrap items-baseline justify-between gap-4">
          <h2 className="font-legend text-[11px] font-700 uppercase tracking-legend text-ink">
            The divergence
          </h2>
          {/* The key is the argument in six words, so it sits with the figure
              rather than in a corner of the page. */}
          <div className="flex items-center gap-5">
            <span className="flex items-center gap-2">
              <span aria-hidden className="inline-block h-[2px] w-5 bg-reserved" />
              <span className="legend text-reserved">reserved · what Nimbus believes</span>
            </span>
            <span className="flex items-center gap-2">
              <span aria-hidden className="inline-block h-[2px] w-5 bg-observed" />
              <span className="legend text-observed">observed · what the host is doing</span>
            </span>
          </div>
        </div>

        <DivergenceStrip lanes={strip} recordingSince={since.current} />

        <div className="mt-5 border-t border-ink/20 pt-4">
          <Finding
            lanes={strip}
            readsTelemetry={readsTelemetry}
            policy={policy}
            telemetryOn={scheduler.data.telemetry_enabled}
          />
        </div>
      </section>

      <section className="mt-10">
        <FleetPlates workers={workers.data} />
      </section>

      <section className="mt-10">
        <DecisionTrace decisions={decisions.data ?? []} />
      </section>

      <section className="mt-10 grid gap-6 lg:grid-cols-2">
        <QueueLedger scheduler={scheduler.data} />
        <JobsLog />
      </section>

      <section className="mt-10">
        <BenchmarkTable />
      </section>

      <footer className="mt-10 border-t border-ink/20 pt-4 font-log text-[10px] leading-relaxed text-declared">
        Strategy switching is the one write this dashboard makes
        (PUT /scheduler, unauthenticated by design). Destructive controls
        still live on the admin router, not mounted unless
        NIMBUS_ENABLE_ADMIN is set.
      </footer>
    </main>
  );
}
