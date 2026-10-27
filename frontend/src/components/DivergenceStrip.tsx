import { useEffect, useId, useRef } from "react";

import { LANE_H, WINDOW_S } from "../lib/theme";
import { useInk } from "../lib/themeMode";

/** One second of paper, for one worker. */
export interface Reading {
  /** Nimbus's belief: reserved CPU units as a percentage of declared. */
  reserved: number;
  /** The host: observed CPU percent. Null when telemetry is off. */
  observed: number | null;
}

export interface Lane {
  workerId: string;
  readings: Reading[];
  healthy: boolean;
}

const PAD_L = 104;
const PAD_R = 104;

/**
 * Blank stock between one worker's trace and the next.
 *
 * Not cosmetic. Without it the lanes butt together, one lane's 0 rule sits on
 * the next lane's 100 rule, and their scale labels overprint -- so three
 * separate recordings read as a single continuous chart, which is exactly the
 * wrong thing to say about three workers that are supposed to be independent.
 */
const GUTTER = 20;

/**
 * Horizontal geometry, fixed to the window rather than to the data.
 *
 * The step is `usable / (WINDOW_S - 1)` and the newest sample is pinned to the
 * right edge, so thirty seconds of recording occupies the rightmost third of
 * the paper and the rest stays blank. Scaling the samples to fill the width
 * instead — the obvious way to write this — would draw thirty seconds of
 * history as though it were ninety, and a strip whose time axis silently
 * rescales is not an instrument.
 */
function geometry(count: number, width: number) {
  const usable = width - PAD_L - PAD_R;
  const step = usable / (WINDOW_S - 1);
  const x = (i: number) => width - PAD_R - (count - 1 - i) * step;
  return { x, step, usable };
}

const y = (v: number, height: number) =>
  height - (Math.min(100, Math.max(0, v)) / 100) * height;

function pathFor(values: (number | null)[], width: number, height: number): string {
  const { x } = geometry(values.length, width);
  let d = "";
  let pen = false;
  values.forEach((value, i) => {
    if (value === null) {
      pen = false;
      return;
    }
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(value, height).toFixed(1)}`;
    pen = true;
  });
  return d;
}

/**
 * The band between belief and reality, for one worker, as hatching.
 *
 * Split by sign, because the two directions of disagreement mean opposite
 * things and drawing them alike would throw that away:
 *
 *   observed above reserved -- the host is busier than Nimbus knows. This is
 *   the case the whole project is about: external pressure carries no lease, no
 *   slot and no reservation, so the declared/reserved model reports the worker
 *   free. Hazard hatch.
 *
 *   reserved above observed -- Nimbus has committed more than the host is
 *   using. Usually a job that has not spun up yet. Grey hatch, the colour of
 *   the operator's own expectations.
 */
function Band({
  lane,
  width,
  height,
  uid,
}: {
  lane: Lane;
  width: number;
  height: number;
  uid: string;
}) {
  const INK = useInk();
  const { x } = geometry(lane.readings.length, width);
  const filled = lane.readings
    .map((r, i) => ({ i, r }))
    .filter(({ r }) => r.observed !== null);
  if (filled.length < 2) return null;

  const forward = filled
    .map(({ i, r }, n) => `${n === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(r.observed!, height).toFixed(1)}`)
    .join("");
  const back = [...filled]
    .reverse()
    .map(({ i, r }) => `L${x(i).toFixed(1)},${y(r.reserved, height).toFixed(1)}`)
    .join("");
  const area = `${forward}${back}Z`;

  const reservedLine = filled
    .map(({ i, r }, n) => `${n === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(r.reserved, height).toFixed(1)}`)
    .join("");
  const left = x(filled[0]!.i);
  const right = x(filled[filled.length - 1]!.i);

  return (
    <g className="band">
      <defs>
        <clipPath id={`${uid}-above`}>
          <path d={`${reservedLine}L${right.toFixed(1)},0L${left.toFixed(1)},0Z`} />
        </clipPath>
        <clipPath id={`${uid}-below`}>
          <path d={`${reservedLine}L${right.toFixed(1)},${height}L${left.toFixed(1)},${height}Z`} />
        </clipPath>
        <pattern id={`${uid}-hz`} width={6} height={6} patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1={0} y1={0} x2={0} y2={6} stroke={INK.observed} strokeWidth={1.6} />
        </pattern>
        <pattern id={`${uid}-gr`} width={6} height={6} patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">
          <line x1={0} y1={0} x2={0} y2={6} stroke={INK.declared} strokeWidth={1} />
        </pattern>
      </defs>
      <path d={area} fill={`url(#${uid}-hz)`} clipPath={`url(#${uid}-above)`} />
      <path d={area} fill={`url(#${uid}-gr)`} clipPath={`url(#${uid}-below)`} />
    </g>
  );
}

/** The pen head: a dot where the line is being laid down right now. */
function PenHead({ value, x, height, colour }: {
  value: number | null;
  x: number;
  height: number;
  colour: string;
}) {
  const INK = useInk();
  if (value === null) return null;
  const cy = y(value, height);
  return <circle cx={x} cy={cy} r={4.5} fill={colour} stroke={INK.paper} strokeWidth={2} />;
}

function WorkerLane({
  lane,
  width,
  uid,
}: {
  lane: Lane;
  width: number;
  uid: string;
}) {
  const INK = useInk();
  const height = LANE_H - GUTTER;
  const latest = lane.readings[lane.readings.length - 1];
  const { x } = geometry(lane.readings.length, width);
  const now = width - PAD_R;

  return (
    <g>
      {/* Scale, drawn and labelled. Every lane is 0-100%, so the lanes are
          directly comparable -- but only if the reader can see that they share
          a scale. */}
      {[0, 50, 100].map((v) => (
        <g key={v}>
          <line
            x1={PAD_L}
            x2={now}
            y1={y(v, height)}
            y2={y(v, height)}
            stroke={v === 0 ? INK.declared : INK.hairline}
            strokeWidth={1}
            strokeDasharray={v === 0 ? undefined : "4 4"}
          />
          {v !== 50 && (
            <text
              x={PAD_L - 14}
              y={y(v, height) + (v === 100 ? 8 : 0)}
              textAnchor="end"
              fontSize={11}
              fill={INK.declared}
            >
              {v}%
            </text>
          )}
        </g>
      ))}

      <Band lane={lane} width={width} height={height} uid={uid} />

      <path
        className="pen"
        style={{ "--len": 4000 } as React.CSSProperties}
        d={pathFor(lane.readings.map((r) => r.reserved), width, height)}
        fill="none"
        stroke={INK.reserved}
        strokeWidth={2.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <path
        className="pen"
        style={{ "--len": 4000 } as React.CSSProperties}
        d={pathFor(lane.readings.map((r) => r.observed), width, height)}
        fill="none"
        stroke={INK.observed}
        strokeWidth={2.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />

      {lane.readings.length > 0 && (
        <>
          <PenHead value={latest?.reserved ?? null} x={x(lane.readings.length - 1)} height={height} colour={INK.reserved} />
          <PenHead value={latest?.observed ?? null} x={x(lane.readings.length - 1)} height={height} colour={INK.observed} />
        </>
      )}

      <text x={0} y={height / 2 - 2} fontSize={13} fontWeight={600} fill={INK.ink}>
        {lane.workerId}
      </text>
      <text x={0} y={height / 2 + 16} fontSize={11} fontWeight={lane.healthy ? 400 : 600} fill={lane.healthy ? INK.declared : INK.observedText}>
        {lane.healthy ? "Healthy" : "Not healthy"}
      </text>

      {/* Readout: reality first, then what Nimbus believes -- the order the
          argument runs in. The big numeral is large bold type, so the hazard
          fill is allowed at this size. */}
      <text x={now + 16} y={height / 2 + 4} fontSize={24} fontWeight={700} fill={INK.observed}>
        {latest?.observed === null || latest?.observed === undefined ? "-" : `${latest.observed.toFixed(0)}%`}
      </text>
      <text x={now + 16} y={height / 2 + 22} fontSize={11} fontWeight={600} fill={INK.declared}>
        {latest ? `Reserved ${latest.reserved.toFixed(0)}%` : "Reserved -"}
      </text>

      {/* Lane separator, in the gutter. */}
      <line x1={0} x2={width} y1={height + GUTTER / 2 + 4} y2={height + GUTTER / 2 + 4} stroke={INK.hairline} strokeWidth={1} />
    </g>
  );
}

/**
 * The recorder strip: belief against reality, one lane per worker, paper
 * feeding right to left.
 *
 * This is the page's thesis rather than a chart on it. Everything else Nimbus
 * measures is downstream of the question this shape answers: when the host and
 * the bookkeeping disagree, how large is the gap, and is the scheduler allowed
 * to know?
 */
export function DivergenceStrip({
  lanes,
  width = 1080,
  recordingSince,
}: {
  lanes: Lane[];
  width?: number;
  recordingSince: string;
}) {
  const INK = useInk();
  const uid = useId().replace(/:/g, "");
  const height = lanes.length * LANE_H;
  const now = width - PAD_R;

  // On a narrow screen the strip scrolls, and the live edge is on the right.
  // Open there once; after that the reader's own scroll position is theirs.
  const scroller = useRef<HTMLElement>(null);
  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollLeft = el.scrollWidth;
  }, []);

  return (
    <figure
      ref={scroller}
      className="relative overflow-x-auto"
      tabIndex={0}
      aria-label="Divergence strip, scrolls horizontally on narrow screens"
    >
      <svg
        width="100%"
        style={{ minWidth: 720 }}
        viewBox={`0 0 ${width} ${height + 14}`}
        role="img"
        aria-label={`Reserved capacity against observed host CPU for ${lanes.length} workers over the last ${WINDOW_S} seconds`}
        className="block"
      >
        <line x1={PAD_L} x2={PAD_L} y1={0} y2={height - GUTTER} stroke={INK.hairline} strokeWidth={1} />

        {lanes.map((lane, i) => (
          <g key={lane.workerId} transform={`translate(0,${i * LANE_H})`}>
            <WorkerLane lane={lane} width={width} uid={`${uid}-${i}`} />
          </g>
        ))}

        {/* The live edge. Paper feeds from the right, so now is the right edge
            and history scrolls away to the left. */}
        <line x1={now} x2={now} y1={-2} y2={height - GUTTER + 2} stroke={INK.declared} strokeWidth={1} strokeDasharray="3 3" />
        <text x={now} y={height + 6} textAnchor="middle" fontSize={11} fontWeight={600} fill={INK.ink}>
          now
        </text>
        <text x={PAD_L} y={height + 6} fontSize={11} fill={INK.declared}>
          {WINDOW_S}s window · recording since {recordingSince}
        </text>
      </svg>
    </figure>
  );
}
