import type { Lane } from "./DivergenceStrip";

/**
 * One sentence, generated from the strip above it.
 *
 * Derived rather than written, for the same reason the conclusions in
 * `docs/results.md` are derived from their own tables: a caption composed
 * separately from the figure is free to drift from it, and the drift is
 * invisible. This reads the same readings the pens drew.
 */
const THRESHOLD = 20;

function gapsOf(lanes: Lane[]) {
  return lanes
    .map((lane) => {
      const latest = lane.readings[lane.readings.length - 1];
      if (!latest || latest.observed === null) return null;
      return { workerId: lane.workerId, gap: latest.observed - latest.reserved, latest };
    })
    .filter((g): g is NonNullable<typeof g> => g !== null);
}

/**
 * Whether the Finding is reporting a divergence. The masthead's warning stripe
 * and the Finding's left rule both read this, so they cannot disagree with
 * each other or with the strip.
 */
export function isDiverged(lanes: Lane[], telemetryOn: boolean): boolean {
  if (!telemetryOn) return false;
  const gaps = gapsOf(lanes);
  if (gaps.length === 0) return false;
  return Math.max(...gaps.map((g) => Math.abs(g.gap))) >= THRESHOLD;
}

const BASE = "max-w-[62ch] border-l-[6px] py-1 pl-4 font-mono text-[15px] leading-relaxed sm:text-base";
const Reserved = ({ children }: { children: React.ReactNode }) => (
  <span className="font-semibold text-ink underline decoration-ink decoration-2 underline-offset-4">{children}</span>
);
const Observed = ({ children }: { children: React.ReactNode }) => (
  <span className="font-semibold text-observedText">{children}</span>
);

export function Finding({
  lanes,
  readsTelemetry,
  policy,
  telemetryOn,
}: {
  lanes: Lane[];
  readsTelemetry: boolean;
  policy: string;
  telemetryOn: boolean;
}) {
  if (!telemetryOn) {
    return (
      <p className={`${BASE} border-ink text-ink`}>
        Telemetry is off, so there is no observed trace and nothing to disagree with.
        The reserved trace is Nimbus's own bookkeeping, which is all it has —{" "}
        <span className="text-declared">
          and all any static policy would have had regardless.
        </span>
      </p>
    );
  }

  const gaps = gapsOf(lanes);
  const diverged = isDiverged(lanes, telemetryOn);
  const rule = diverged ? "border-observed" : "border-ink";

  if (gaps.length === 0) {
    return (
      <p className={`${BASE} ${rule} text-declared`}>
        Waiting for the first samples. The strip is blank until both traces have
        something to draw.
      </p>
    );
  }

  const worst = gaps.reduce((a, b) => (b.gap > a.gap ? b : a));
  const widest = gaps.reduce((a, b) => (Math.abs(b.gap) > Math.abs(a.gap) ? b : a));

  // Below this the two pens are effectively tracking each other and there is no
  // divergence worth narrating. Saying "a 3% gap" every second would train a
  // reader to ignore the sentence by the time it says something. The test is
  // on the absolute gap: "agree" must not be said while a worker is booked
  // full and idle, which is a divergence too, just in the other direction.
  if (Math.abs(widest.gap) < THRESHOLD) {
    return (
      <p className={`${BASE} ${rule} text-ink`}>
        Belief and reality agree across the fleet. The widest gap is{" "}
        <Observed>{Math.abs(widest.gap).toFixed(0)} points</Observed>{" "}
        on {widest.workerId} — within the range Nimbus's reservation model
        already accounts for.
      </p>
    );
  }

  // Unbooked load is the case the project is about, so it wins whenever it
  // clears the threshold; otherwise the gap is the opposite one.
  if (worst.gap < THRESHOLD) {
    return (
      <p className={`${BASE} ${rule} text-ink`}>
        Nimbus has{" "}
        <Reserved>{widest.latest.reserved.toFixed(0)}% reserved</Reserved>{" "}
        on {widest.workerId}. The host is at{" "}
        <Observed>{widest.latest.observed!.toFixed(0)}%</Observed>
        . The jobs holding those slots are not using the CPU they were booked
        for, so the declared/reserved model reports the worker full while the
        host has room —{" "}
        {readsTelemetry ? (
          <span className="text-ink">
            and <span className="font-semibold">{policy}</span> can see the difference.
          </span>
        ) : (
          <span className="text-ink">
            and <span className="font-semibold">{policy}</span> cannot see the
            difference.
          </span>
        )}
      </p>
    );
  }

  return (
    <p className={`${BASE} ${rule} text-ink`}>
      Nimbus has{" "}
      <Reserved>{worst.latest.reserved.toFixed(0)}% reserved</Reserved>{" "}
      on {worst.workerId}. The host is at{" "}
      <Observed>{worst.latest.observed!.toFixed(0)}%</Observed>
      . That load carries no lease, no slot and no reservation, so the
      declared/reserved model still reports the worker free —{" "}
      {readsTelemetry ? (
        <span className="text-ink">
          and <span className="font-semibold">{policy}</span> can see the difference.
        </span>
      ) : (
        <span className="text-ink">
          and <span className="font-semibold">{policy}</span> cannot see the
          difference.
        </span>
      )}
    </p>
  );
}
