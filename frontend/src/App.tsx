import { useCallback, useEffect, useRef, useState } from "react";

import { ActiveAlgorithm } from "./components/ActiveAlgorithm";
import { BenchmarkTable } from "./components/BenchmarkTable";
import { DecisionTrace } from "./components/DecisionTrace";
import { DivergenceStrip, type Lane, type Reading } from "./components/DivergenceStrip";
import { Finding, isDiverged } from "./components/Finding";
import { FleetPlates } from "./components/FleetPlates";
import { HeaderRail } from "./components/HeaderRail";
import { JobsLog } from "./components/JobsLog";
import { QueueLedger } from "./components/QueueLedger";
import { StrategyComparison } from "./components/StrategyComparison";
import { StrategySwitch } from "./components/StrategySwitch";
import { usePoll } from "./hooks/usePoll";
import { InfoTip } from "./components/InfoTip";
import { Card } from "./components/SectionHead";
import { Sidebar } from "./components/Sidebar";
import { StatTiles } from "./components/StatTiles";
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

  const [menuOpen, setMenuOpen] = useState(false);
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
        <p role="status" className="text-[14px] text-declared">Connecting to the control plane…</p>
      </main>
    );
  }

  if (!scheduler.data || !workers.data) {
    return (
      <main className="relative z-10 grid min-h-[100dvh] place-items-center px-6">
        <div role="alert" className="card max-w-md p-8">
          <p className="mb-2 text-[18px] font-semibold text-ink">No control plane</p>
          <p className="text-[14px] leading-relaxed text-declared">
            Start it with{" "}
            <code className="font-mono text-[13px] text-ink">uvicorn backend.api.app:app</code>, or
            point the dev proxy elsewhere with{" "}
            <code className="font-mono text-[13px] text-ink">NIMBUS_API</code>.
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
    <div className="min-h-[100dvh]">
      <Sidebar scheduler={scheduler.data} open={menuOpen} onClose={() => setMenuOpen(false)} />

      <div className="lg:pl-[270px]">
        <div className="mx-auto max-w-[1400px] px-4 sm:px-6 lg:px-8">
          <HeaderRail
            telemetryOn={scheduler.data.telemetry_enabled}
            readsTelemetry={readsTelemetry}
            stale={stale}
            onMenu={() => setMenuOpen(true)}
          />

          <main className="space-y-6 pb-10">
            <div id="overview" className="flex scroll-mt-24 flex-wrap items-end justify-between gap-4 pt-2">
              <div>
                <h1 className="text-[24px] font-semibold text-ink">Dashboard</h1>
                <p className="mt-1 text-[14px] text-declared">
                  {workers.data.length} workers · policy {scheduler.data.algorithm_version}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <StrategySwitch
                  available={scheduler.data.available}
                  active={scheduler.data.strategy}
                  onSwitched={scheduler.refresh}
                />
                <InfoTip label="About switching strategy">
                  Switching strategy is the one write this dashboard makes (PUT /scheduler). It only
                  changes how the next jobs are placed. Destructive controls stay on the admin
                  router, which is off unless NIMBUS_ENABLE_ADMIN is set.
                </InfoTip>
              </div>
            </div>

            <StatTiles
              workers={workers.data}
              scheduler={scheduler.data}
              lanes={strip}
              diverged={diverged}
            />

            <ActiveAlgorithm scheduler={scheduler.data} latest={decisions.data?.[0]} />

            <Card
              id="divergence"
              title="Reserved vs observed CPU"
              subtitle="Last 90 seconds, per worker"
              info="Blue is what Nimbus has reserved on each worker (its own bookkeeping). Coral is what the host actually reports. The hatched gap between them is load the scheduler did not place."
              action={
                /* The key is the argument in a line, so it sits with the figure. */
                <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-[13px]">
                  <span className="flex items-center gap-2 text-ink">
                    <span aria-hidden className="h-2.5 w-2.5 rounded-full bg-reserved" />
                    Reserved
                  </span>
                  <span className="flex items-center gap-2 text-ink">
                    <span aria-hidden className="h-2.5 w-2.5 rounded-full bg-observed" />
                    Observed
                  </span>
                </div>
              }
            >
              <DivergenceStrip lanes={strip} recordingSince={since.current} />
              <div className="mt-5">
                <Finding
                  lanes={strip}
                  readsTelemetry={readsTelemetry}
                  policy={policy}
                  telemetryOn={scheduler.data.telemetry_enabled}
                />
              </div>
            </Card>

            <StrategyComparison active={scheduler.data.strategy} />

            <FleetPlates workers={workers.data} />

            <div className="grid gap-6 lg:grid-cols-3">
              <div className="lg:col-span-2">
                <DecisionTrace
                  decisions={decisions.data ?? []}
                  loading={decisions.loading}
                  error={decisions.failures > 1}
                />
              </div>
              <QueueLedger scheduler={scheduler.data} />
            </div>

            <JobsLog />

            <BenchmarkTable />

          </main>
        </div>
      </div>
    </div>
  );
}
