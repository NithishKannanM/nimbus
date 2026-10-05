/**
 * Swiss industrial print: matte paper, carbon ink, one hazard accent.
 *
 * These are print inks, not semantic UI colours, and the distinction is the
 * whole design. Nimbus's central claim is that a worker has three separate
 * descriptions -- DECLARED, RESERVED, OBSERVED -- that are allowed to disagree,
 * and that only one placement policy may read the third. A palette that gave
 * "good" one colour and "bad" another would flatten that back into a status
 * light and undo the argument on screen. So the three descriptions differ in
 * TREATMENT as well as hue:
 *
 *   declared  grey, drawn as outline or hatch, never a solid fill
 *   reserved  carbon, solid
 *   observed  hazard orange, and the only accent on the page
 *
 * Hazard means one thing: the reading contradicts the register. Observed load,
 * retried, failed, dead-lettered, tie-break, stale poll, api unreachable.
 *
 * Contrast on paper (see DESIGN.md): ink 17.4, declared 5.9, observedText 5.2,
 * observed 3.07 (marks, fills, strokes and >=24px bold type only).
 */
export const INK = {
  /** Matte, unbleached substrate. */
  paper: "#F2F0EB",
  /** Carbon. Text, structural rules, and the RESERVED mark. */
  ink: "#0A0A0A",
  /** Structural lines are solid ink; kept as a name so call sites say why. */
  rule: "#0A0A0A",
  /** Light inner rules between table rows. */
  hairline: "#C9C5BB",
  /** DECLARED: operator configuration. Outline or hatch only. */
  declared: "#5F5C56",
  /** RESERVED: Nimbus bookkeeping. Carbon, solid. */
  reserved: "#0A0A0A",
  /** OBSERVED / divergence: hazard. Marks, fills, strokes, large type. */
  observed: "#F54F00",
  /** The accent for small text: darker so it clears 4.5:1. */
  observedText: "#B33A00",
} as const;

/** Seconds of paper held on screen. One sample per second. */
export const WINDOW_S = 90;

/** Strip height per worker lane, in px. */
export const LANE_H = 84;

function svgUrl(body: string, w: number, h: number, extra = ""): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" ${extra}>${body}</svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(svg)}")`;
}

/** 45 degree hatch tile as a CSS background-image. */
export function hatchUrl(colour: string, pitch = 6, stroke = 1): string {
  const p = pitch;
  return svgUrl(
    `<path d="M0 ${p}L${p} 0M-1 1L1 -1M${p - 1} ${p + 1}L${p + 1} ${p - 1}" stroke="${colour}" stroke-width="${stroke}" fill="none" shape-rendering="crispEdges"/>`,
    p,
    p,
  );
}

/** Thick 45 degree warning stripes: colour bands on a transparent ground. */
export function stripeUrl(colour: string): string {
  return svgUrl(
    `<polygon points="0,10 10,0 20,0 0,20" fill="${colour}"/><polygon points="10,20 20,10 20,20" fill="${colour}"/>`,
    20,
    20,
  );
}

/** Greyscale fractal noise, a tile of paper grain. Used at opacity <= 0.06. */
export const GRAIN_URL = svgUrl(
  `<filter id="n"><feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" stitchTiles="stitch"/><feColorMatrix values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 1.4 -0.2"/></filter><rect width="100%" height="100%" filter="url(#n)"/>`,
  160,
  160,
);
