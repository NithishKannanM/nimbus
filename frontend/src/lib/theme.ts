/**
 * The four-pen plotter set.
 *
 * These are ink colours, not semantic UI colours, and the distinction is the
 * whole design. Nimbus's central claim is that a worker has three separate
 * descriptions — DECLARED, RESERVED, OBSERVED — that are allowed to disagree,
 * and that only one placement policy may read the third. A palette that gave
 * "good" one colour and "bad" another would flatten that back into a status
 * light and undo the argument on screen.
 *
 * Red for OBSERVED is load-bearing. In a bound lab record, red ink is what gets
 * written when the reading contradicts the register. Observed telemetry is
 * exactly that: not an alarm, a correction to a model that did not know.
 */
export const INK = {
  /** Plotter stock. Sage-tinted, deliberately not cream. */
  paper: "#EEF0EA",
  /** Grid ruling, printed before the pens ever touch it. */
  rule: "#D5D8CE",
  /** Primary pen. Near-black with a green cast. */
  ink: "#1C1F1A",
  /** Graphite: operator configuration, inert, never inferred. */
  declared: "#5E645C",
  /** Blue ballpoint: Nimbus's own bookkeeping. What it believes it committed. */
  reserved: "#1B4D8F",
  /** Red pen: what the host is actually doing. */
  observed: "#C2410C",
} as const;

/** Seconds of paper held on screen. One sample per second. */
export const WINDOW_S = 90;

/** Strip height per worker lane, in px. */
export const LANE_H = 66;
