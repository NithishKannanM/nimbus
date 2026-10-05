/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    // Square corners are a rule, so every radius step resolves to zero.
    borderRadius: {
      none: "0",
      sm: "0",
      DEFAULT: "0",
      md: "0",
      lg: "0",
      xl: "0",
      "2xl": "0",
      "3xl": "0",
      full: "0",
    },
    extend: {
      // Mirrors src/lib/theme.ts. Kept as literals because Tailwind reads this
      // file through its own loader, and a .ts import here is a fragile way to
      // save a few lines.
      colors: {
        paper: "#F2F0EB",
        ink: "#0A0A0A",
        rule: "#0A0A0A",
        hairline: "#C9C5BB",
        declared: "#5F5C56",
        reserved: "#0A0A0A",
        observed: "#F54F00",
        observedText: "#B33A00",
      },
      fontFamily: {
        // Macro type: structural headers and the big numerals.
        display: ['"Archivo Black"', "Impact", "sans-serif"],
        // Micro type: every label, datum and table.
        mono: ['"IBM Plex Mono"', "ui-monospace", "monospace"],
      },
      letterSpacing: {
        label: "0.08em",
      },
    },
  },
  plugins: [],
};
