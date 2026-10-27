import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

import { DARK, LIGHT, type Palette } from "./theme";

export type Mode = "light" | "dark";
const KEY = "nimbus-theme";

/** The mode index.html already applied before first paint, so React starts in
 *  agreement with the page and there is no flash. */
function initialMode(): Mode {
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

const Ctx = createContext<{ mode: Mode; toggle: () => void }>({ mode: "light", toggle: () => {} });

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<Mode>(initialMode);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", mode === "dark");
    document.documentElement.style.colorScheme = mode;
  }, [mode]);

  const toggle = useCallback(() => {
    setMode((m) => {
      const next = m === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(KEY, next);
      } catch {
        // Storage blocked: the toggle still works for this visit.
      }
      return next;
    });
  }, []);

  return <Ctx.Provider value={{ mode, toggle }}>{children}</Ctx.Provider>;
}

export const useThemeMode = () => useContext(Ctx);

/** The palette for SVG and inline styles, which cannot read Tailwind classes. */
export function useInk(): Palette {
  return useContext(Ctx).mode === "dark" ? DARK : LIGHT;
}
