import { useId } from "react";

import { INK, LANE_H, WINDOW_S } from "../lib/theme";

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
const PAD_R = 78;

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
 * The band between belief and reality, for one worker.
 *
 * Split by sign, because the two directions of disagreement mean opposite
 * things and colouring them alike would throw that away:
 *
 *   observed above reserved -- the host is busier than Nimbus knows. This is
 *   the case the whole project is about: external pressure carries no lease, no
 *   slot and no reservation, so the declared/reserved model reports the worker
 *   free. Inked red.
 *
 *   reserved above observed -- Nimbus has committed more than the host is
 *   using. Benign, usually a job that has not spun up yet. Inked in the blue of
 *   the bookkeeping that produced it.
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
      </defs>
      <path d={area} fill={INK.observed} opacity={0.18} clipPath={`url(#${uid}-above)`} />
      <path d={area} fill={INK.reserved} opacity={0.12} clipPath={`url(#${uid}-below)`} />
    </g>
  );
}

/** The pen head: where the ink is being laid down right now. */
function PenHead({ value, x, height, colour }: {
  value: number | null;
  x: number;
  height: number;
  colour: string;
}) {
  if (value === null) return null;
  const cy = y(value, height);
  return (
    <>
      <circle cx={x} cy={cy} r={2.6} fill={colour} />
      <path d={`M${x + 5},${cy - 3.4}L${x + 5},${cy + 3.4}L${x + 1.5},${cy}Z`} fill={colour} />
    </>
  );
}

function WorkerLane({ lane, width, uid }: { lane: Lane; width: number; uid: string }) {
  const height = LANE_H - GUTTER;
  const latest = lane.readings[lane.readings.length - 1];
  const { x } = geometry(lane.readings.length, width);
  const now = width - PAD_R;

  return (
    <g>
      {/* Scale, drawn and labelled. Every lane is 0-100%, so the lanes are
          directly comparable -- but only if the reader can see that they share
          a scale. An unlabelled lane invites reading each one as though it were
          normalised to its own maximum. */}
      {[0, 50, 100].map((v) => (
        <g key={v}>
          <line
            x1={PAD_L}
            x2={now}
            y1={y(v, height)}
            y2={y(v, height)}
            stroke={INK.ink}
            strokeWidth={v === 0 ? 0.9 : 0.5}
            strokeDasharray={v === 0 ? undefined : "2 4"}
            opacity={v === 0 ? 0.4 : 0.22}
          />
          {v !== 50 && (
            <text
              x={PAD_L - 8}
              y={y(v, height) + (v === 100 ? 7 : -1)}
              textAnchor="end"
              className="font-log"
              fontSize={8}
              fill={INK.declared}
            >
              {v}
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
        strokeWidth={1.5}
        strokeLinejoin="round"
      />
      <path
        className="pen"
        style={{ "--len": 4000 } as React.CSSProperties}
        d={pathFor(lane.readings.map((r) => r.observed), width, height)}
        fill="none"
        stroke={INK.observed}
        strokeWidth={1.5}
        strokeLinejoin="round"
      />

      {lane.readings.length > 0 && (
        <>
          <PenHead value={latest?.reserved ?? null} x={x(lane.readings.length - 1)} height={height} colour={INK.reserved} />
          <PenHead value={latest?.observed ?? null} x={x(lane.readings.length - 1)} height={height} colour={INK.observed} />
        </>
      )}

      <text x={0} y={height / 2 - 4} className="font-log" fontSize={12} fill={INK.ink} opacity={lane.healthy ? 1 : 0.45}>
        {lane.workerId}
      </text>
      <text x={0} y={height / 2 + 10} className="font-legend" fontSize={8.5} fill={INK.declared} letterSpacing="0.12em">
        {lane.healthy ? "HEALTHY" : "NOT HEALTHY"}
      </text>

      {/* Readout: reality first, then what Nimbus believes — the order the
          argument runs in. */}
      <text x={now + 16} y={height / 2 - 3} className="font-meter" fontSize={13} fontWeight={700} fill={INK.observed}>
        {latest?.observed === null || latest?.observed === undefined ? "——" : `${latest.observed.toFixed(0)}%`}
      </text>
      <text x={now + 16} y={height / 2 + 13} className="font-meter" fontSize={11} fill={INK.reserved}>
        {latest ? `${latest.reserved.toFixed(0)}%` : "——"}
      </text>
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
  const uid = useId().replace(/:/g, "");
  const height = lanes.length * LANE_H;
  const now = width - PAD_R;

  return (
    <figure className="relative">
      <svg
        width="100%"
        viewBox={`0 0 ${width} ${height + 10}`}
        role="img"
        aria-label={`Reserved capacity against observed host CPU for ${lanes.length} workers over the last ${WINDOW_S} seconds`}
        className="block"
      >
        {/* Left and right margins of the paper. */}
        <line
          x1={PAD_L}
          x2={PAD_L}
          y1={0}
          y2={height - GUTTER}
          stroke={INK.ink}
          strokeWidth={0.5}
          opacity={0.25}
        />

        {lanes.map((lane, i) => (
          <g key={lane.workerId} transform={`translate(0,${i * LANE_H})`}>
            <WorkerLane lane={lane} width={width} uid={`${uid}-${i}`} />
          </g>
        ))}

        {/* The pen carriage. Paper feeds from the right, so now is the right
            edge and history scrolls away to the left. */}
        <line
          x1={now}
          x2={now}
          y1={-2}
          y2={height - GUTTER + 2}
          stroke={INK.ink}
          strokeWidth={1}
          opacity={0.6}
        />
        <text x={now} y={height + 2} textAnchor="middle" className="font-legend" fontSize={8.5} fill={INK.ink} letterSpacing="0.14em">
          NOW
        </text>
        <text x={PAD_L} y={height + 2} className="font-legend" fontSize={8.5} fill={INK.declared} letterSpacing="0.12em">
          {WINDOW_S}s OF PAPER · RECORDING SINCE {recordingSince}
        </text>
      </svg>
    </figure>
  );
}
