/**
 * Modernize-style admin palette: white cards on a white canvas, slate text,
 * one blue, one coral.
 *
 * The colours still carry meaning, not status. Nimbus's central claim is that
 * a worker has three separate descriptions -- DECLARED, RESERVED, OBSERVED --
 * that are allowed to disagree, and only one placement policy may read the
 * third. So the three differ in TREATMENT as well as hue:
 *
 *   declared  slate, drawn as outline or hatch, never a solid fill
 *   reserved  primary blue, solid
 *   observed  coral, and the only accent on the page
 *
 * Coral means one thing: the reading contradicts the register. There is no
 * green "ok" and no red "bad".
 *
 * Contrast (see DESIGN.md). Light, on white: ink 12.4, declared 5.5,
 * observedText 5.6, reserved 3.3 and observed 3.5 (marks, fills, strokes and
 * large type only). Dark, on #2A3447: ink 10.8, declared 5.0, observed 5.3,
 * reserved 3.8.
 */
export interface Palette {
  /** Card surface. */
  paper: string;
  /** Body text and strong labels. */
  ink: string;
  /** Card borders and table row rules. */
  hairline: string;
  /** DECLARED: operator configuration. Outline or hatch only. */
  declared: string;
  /** RESERVED: Nimbus bookkeeping. Primary blue, solid. */
  reserved: string;
  /** OBSERVED / divergence: coral. Marks, fills, strokes, large type. */
  observed: string;
  /** The accent for small text: clears 4.5:1 on paper. */
  observedText: string;
}

/** Mirrored as CSS variables in index.css (:root and .dark); change both. */
export const LIGHT: Palette = {
  paper: "#FFFFFF",
  ink: "#2A3547",
  hairline: "#E5EAEF",
  declared: "#5A6A85",
  reserved: "#5D87FF",
  observed: "#E85D37",
  observedText: "#B93D18",
};

/** Dark: Modernize's dark surfaces. Coral is lighter here, so the same hue
 *  clears 4.5:1 for text on its own, and observedText can equal observed. */
export const DARK: Palette = {
  paper: "#2A3447",
  ink: "#EAEFF4",
  hairline: "#333F55",
  declared: "#98A4B8",
  reserved: "#5D87FF",
  observed: "#FA896B",
  observedText: "#FA896B",
};

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
