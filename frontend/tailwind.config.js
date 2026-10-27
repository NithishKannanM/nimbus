/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      // Each colour is a CSS variable set in index.css (:root for light, .dark
      // for dark) and mirrored in src/lib/theme.ts for SVG. Channels, not hex,
      // so opacity modifiers like bg-canvas/95 keep working.
      colors: {
        paper: "rgb(var(--c-paper) / <alpha-value>)",
        canvas: "rgb(var(--c-canvas) / <alpha-value>)",
        ink: "rgb(var(--c-ink) / <alpha-value>)",
        hairline: "rgb(var(--c-hairline) / <alpha-value>)",
        declared: "rgb(var(--c-declared) / <alpha-value>)",
        reserved: "rgb(var(--c-reserved) / <alpha-value>)",
        reservedLight: "rgb(var(--c-reservedLight) / <alpha-value>)",
        observed: "rgb(var(--c-observed) / <alpha-value>)",
        observedText: "rgb(var(--c-observedText) / <alpha-value>)",
        observedLight: "rgb(var(--c-observedLight) / <alpha-value>)",
        muted: "rgb(var(--c-muted) / <alpha-value>)",
      },
      fontFamily: {
        sans: ['"Plus Jakarta Sans"', "system-ui", "sans-serif"],
        mono: ['"IBM Plex Mono"', "ui-monospace", "monospace"],
      },
      borderRadius: {
        card: "7px",
      },
      boxShadow: {
        card: "var(--shadow-card)",
      },
    },
  },
  plugins: [],
};
