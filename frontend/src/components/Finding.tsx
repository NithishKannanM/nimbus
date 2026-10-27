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
 * Whether the Finding is reporting a divergence. The overview's divergence
 * tile and the Finding's alert tone both read this, so they cannot disagree
 * with each other or with the strip.
 */
export function isDiverged(lanes: Lane[], telemetryOn: boolean): boolean {
  if (!telemetryOn) return false;
  const gaps = gapsOf(lanes);
  if (gaps.length === 0) return false;
  return Math.max(...gaps.map((g) => Math.abs(g.gap))) >= THRESHOLD;
}

const BASE = "rounded-card px-5 py-4 text-[14px] leading-relaxed sm:text-[15px]";
const Reserved = ({ children }: { children: React.ReactNode }) => (
  <span className="font-semibold text-ink underline decoration-reserved decoration-2 underline-offset-4">{children}</span>
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
      <p className={`${BASE} bg-muted text-ink`}>
        Telemetry is off. Only reservations are visible,{" "}
        <span className="text-declared">which is all a static policy ever has.</span>
      </p>
    );
  }

  const gaps = gapsOf(lanes);
  const diverged = isDiverged(lanes, telemetryOn);
  const rule = diverged ? "bg-observedLight" : "bg-muted";

  if (gaps.length === 0) {
    return <p className={`${BASE} ${rule} text-declared`}>Waiting for the first samples.</p>;
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
        Reservations match reality. Widest gap:{" "}
        <Observed>{Math.abs(widest.gap).toFixed(0)} points</Observed> on {widest.workerId}.
      </p>
    );
  }

  const verdict = (
    <>
      <span className="font-semibold">{policy}</span>{" "}
      {readsTelemetry ? "can see this." : "can't see this."}
    </>
  );

  // Unbooked load is the case the project is about, so it wins whenever it
  // clears the threshold; otherwise the gap is the opposite one.
  if (worst.gap < THRESHOLD) {
    return (
      <p className={`${BASE} ${rule} text-ink`}>
        <span className="font-mono">{widest.workerId}</span>:{" "}
        <Reserved>{widest.latest.reserved.toFixed(0)}% reserved</Reserved>, host at{" "}
        <Observed>{widest.latest.observed!.toFixed(0)}%</Observed>. Booked jobs are idling, so it
        looks full while it has room. {verdict}
      </p>
    );
  }

  return (
    <p className={`${BASE} ${rule} text-ink`}>
      <span className="font-mono">{worst.workerId}</span>:{" "}
      <Reserved>{worst.latest.reserved.toFixed(0)}% reserved</Reserved>, host at{" "}
      <Observed>{worst.latest.observed!.toFixed(0)}%</Observed>. Load from outside Nimbus makes it
      look free. {verdict}
    </p>
  );
}
