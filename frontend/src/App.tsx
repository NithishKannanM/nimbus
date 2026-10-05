import { useCallback, useEffect, useRef, useState } from "react";

import { BenchmarkTable } from "./components/BenchmarkTable";
import { DecisionTrace } from "./components/DecisionTrace";
import { DivergenceStrip, type Lane, type Reading } from "./components/DivergenceStrip";
import { Finding, isDiverged } from "./components/Finding";
import { FleetPlates } from "./components/FleetPlates";
import { HeaderRail } from "./components/HeaderRail";
import { JobsLog } from "./components/JobsLog";
import { QueueLedger } from "./components/QueueLedger";
import { StrategySwitch } from "./components/StrategySwitch";
import { usePoll } from "./hooks/usePoll";
import { SectionHead } from "./components/SectionHead";
import { GRAIN_URL, WINDOW_S } from "./lib/theme";
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
      <main className="relative z-10 grid min-h-[100dvh] place-items-center">
        <p role="status" className="label text-declared">[ connecting to the control plane ]</p>
      </main>
    );
  }

  if (!scheduler.data || !workers.data) {
    return (
      <main className="relative z-10 grid min-h-[100dvh] place-items-center px-6">
        <div role="alert" className="max-w-md">
          <p className="label mb-3 font-semibold text-observedText">[ no control plane ]</p>
          <p className="font-mono text-[13px] leading-relaxed text-ink">
            Start it with{" "}
            <span className="font-semibold">uvicorn backend.api.app:app</span>, or
            point the dev proxy elsewhere with{" "}
            <span className="font-semibold">NIMBUS_API</span>.
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

  const diverged = isDiverged(strip, scheduler.data.telemetry_enabled);

  return (
    <>
      <div
        aria-hidden
        className="grain"
        style={{ backgroundImage: GRAIN_URL, backgroundSize: "160px 160px" }}
      />
      <main className="relative z-10 mx-auto max-w-[1280px] px-4 pb-10 pt-5 sm:px-6">
        <div className="border-b-2 border-ink">
          <HeaderRail
            scheduler={scheduler.data}
            readsTelemetry={readsTelemetry}
            stale={stale}
            workerCount={workers.data.length}
            diverged={diverged}
          />
          <StrategySwitch
            available={scheduler.data.available}
            active={scheduler.data.strategy}
            onSwitched={scheduler.refresh}
          />
        </div>

        <section className="mt-6" aria-labelledby="divergence-h">
          <SectionHead
            n="01"
            id="divergence-h"
            aside={
              /* The key is the argument in a line, so it sits with the figure
                 rather than in a corner of the page. */
              <div className="flex flex-wrap items-center gap-x-5 gap-y-1">
                <span className="flex items-center gap-2">
                  <span aria-hidden className="inline-block h-[3px] w-6 bg-ink" />
                  <span className="label text-ink">reserved · what Nimbus believes</span>
                </span>
                <span className="flex items-center gap-2">
                  <span aria-hidden className="inline-block h-[3px] w-6 bg-observed" />
                  <span className="label text-observedText">observed · what the host is doing</span>
                </span>
              </div>
            }
          >
            The divergence
          </SectionHead>

          <div className="border-y-2 border-ink py-2">
            <DivergenceStrip lanes={strip} recordingSince={since.current} />
          </div>

          <div className="mt-5">
            <Finding
              lanes={strip}
              readsTelemetry={readsTelemetry}
              policy={policy}
              telemetryOn={scheduler.data.telemetry_enabled}
            />
          </div>
        </section>

        <div className="mt-10 border-t-2 border-ink pt-5">
          <FleetPlates workers={workers.data} />
        </div>

        <div className="mt-10 border-t-2 border-ink pt-5">
          <DecisionTrace
            decisions={decisions.data ?? []}
            loading={decisions.loading}
            error={decisions.failures > 1}
          />
        </div>

        <div className="mt-10 grid grid-cols-1 gap-x-8 gap-y-10 border-t-2 border-ink pt-5 lg:grid-cols-2">
          <QueueLedger scheduler={scheduler.data} />
          <JobsLog />
        </div>

        <div className="mt-10 border-t-2 border-ink pt-5">
          <BenchmarkTable />
        </div>

        <footer className="mt-10 border-t-2 border-ink pt-4" aria-labelledby="notes-h">
          <SectionHead n="07" id="notes-h">Notes</SectionHead>
          <p className="max-w-[80ch] font-mono text-[12px] leading-relaxed text-declared">
            Strategy switching is the one write this dashboard makes
            (PUT /scheduler, unauthenticated by design). Destructive controls
            still live on the admin router, not mounted unless
            NIMBUS_ENABLE_ADMIN is set.
          </p>
        </footer>
      </main>
    </>
  );
}
