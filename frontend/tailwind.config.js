/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      // Mirrors src/lib/theme.ts. Kept as literals because Tailwind reads this
      // file through its own loader, and a .ts import here is a fragile way to
      // save six lines.
      colors: {
        paper: "#EEF0EA",
        rule: "#D5D8CE",
        ink: "#1C1F1A",
        declared: "#5E645C",
        reserved: "#1B4D8F",
        observed: "#C2410C",
      },
      fontFamily: {
        // Legend plates. Condensed-feeling grotesque, set in caps with wide
        // tracking wherever it labels something.
        legend: ['"Instrument Sans"', "system-ui", "sans-serif"],
        // The readout. Wide and deliberately slow to read, which is right for
        // a number you are meant to look at rather than skim past.
        meter: ['"Martian Mono"', "ui-monospace", "monospace"],
        // Logbook. Dense, tabular, unglamorous.
        log: ['"IBM Plex Mono"', "ui-monospace", "monospace"],
      },
      letterSpacing: {
        legend: "0.14em",
      },
    },
  },
  plugins: [],
};
