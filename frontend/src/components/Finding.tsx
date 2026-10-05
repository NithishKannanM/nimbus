import type { Lane } from "./DivergenceStrip";

/**
 * One sentence, generated from the strip above it.
 *
 * Derived rather than written, for the same reason the conclusions in
 * `docs/results.md` are derived from their own tables: a caption composed
 * separately from the figure is free to drift from it, and the drift is
 * invisible. This reads the same readings the pens drew.
 */
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
      <p className="max-w-[62ch] font-log text-[13px] leading-relaxed text-ink">
        Telemetry is off, so there is no red pen and nothing to disagree with.
        The blue trace is Nimbus's own bookkeeping, which is all it has —{" "}
        <span className="text-declared">
          and all any static policy would have had regardless.
        </span>
      </p>
    );
  }

  const gaps = lanes
    .map((lane) => {
      const latest = lane.readings[lane.readings.length - 1];
      if (!latest || latest.observed === null) return null;
      return { workerId: lane.workerId, gap: latest.observed - latest.reserved, latest };
    })
    .filter((g): g is NonNullable<typeof g> => g !== null);

  if (gaps.length === 0) {
    return (
      <p className="max-w-[62ch] font-log text-[13px] leading-relaxed text-declared">
        Waiting for the first samples. The paper is blank until the pens have
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
  if (Math.abs(widest.gap) < 20) {
    return (
      <p className="max-w-[62ch] font-log text-[13px] leading-relaxed text-ink">
        Belief and reality agree across the fleet. The widest gap is{" "}
        <span className="text-observed">{Math.abs(widest.gap).toFixed(0)} points</span>{" "}
        on {widest.workerId} — within the range Nimbus's reservation model
        already accounts for.
      </p>
    );
  }

  // Unbooked load is the case the project is about, so it wins whenever it
  // clears the threshold; otherwise the gap is the opposite one.
  if (worst.gap < 20) {
    return (
      <p className="max-w-[62ch] font-log text-[13px] leading-relaxed text-ink">
        Nimbus has{" "}
        <span className="text-reserved">
          {widest.latest.reserved.toFixed(0)}% reserved
        </span>{" "}
        on {widest.workerId}. The host is at{" "}
        <span className="text-observed">
          {widest.latest.observed!.toFixed(0)}%
        </span>
        . The jobs holding those slots are not using the CPU they were booked
        for, so the declared/reserved model reports the worker full while the
        host has room —{" "}
        {readsTelemetry ? (
          <span className="text-ink">
            and <span className="font-600">{policy}</span> can see the difference.
          </span>
        ) : (
          <span className="text-ink">
            and <span className="font-600">{policy}</span> cannot see the
            difference.
          </span>
        )}
      </p>
    );
  }

  return (
    <p className="max-w-[62ch] font-log text-[13px] leading-relaxed text-ink">
      Nimbus has{" "}
      <span className="text-reserved">
        {worst.latest.reserved.toFixed(0)}% reserved
      </span>{" "}
      on {worst.workerId}. The host is at{" "}
      <span className="text-observed">
        {worst.latest.observed!.toFixed(0)}%
      </span>
      . That load carries no lease, no slot and no reservation, so the
      declared/reserved model still reports the worker free —{" "}
      {readsTelemetry ? (
        <span className="text-ink">
          and <span className="font-600">{policy}</span> can see the difference.
        </span>
      ) : (
        <span className="text-ink">
          and <span className="font-600">{policy}</span> cannot see the
          difference.
        </span>
      )}
    </p>
  );
}
