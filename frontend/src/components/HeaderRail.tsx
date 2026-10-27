import { useThemeMode } from "../lib/themeMode";
import { IconMenu, IconMoon, IconSun } from "./Icons";

/** A status pill with a dot, so on/off reads without relying on colour. */
function Status({ on, children, title }: { on: boolean; children: string; title: string }) {
  return (
    <span className={on ? "chip-reserved" : "chip-neutral"} title={title}>
      <span
        aria-hidden
        className={`h-2 w-2 rounded-full ${on ? "bg-reserved" : "border-2 border-declared"}`}
      />
      {children}
    </span>
  );
}

/** Sticky top bar: menu toggle on small screens, live status on the right. */
export function HeaderRail({
  telemetryOn,
  readsTelemetry,
  stale,
  onMenu,
}: {
  telemetryOn: boolean;
  readsTelemetry: boolean;
  stale: boolean;
  onMenu: () => void;
}) {
  const { mode, toggle } = useThemeMode();
  const dark = mode === "dark";
  return (
    <header className="sticky top-0 z-20 bg-canvas/95 backdrop-blur">
      <div className="flex h-[70px] items-center justify-between gap-4">
        <button
          type="button"
          onClick={onMenu}
          aria-label="Open menu"
          className="rounded-full p-2 text-ink hover:bg-reservedLight lg:hidden"
        >
          <IconMenu />
        </button>
        <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
          {stale && (
            <span role="status" className="chip-observed">
              API unreachable · last known
            </span>
          )}
          <Status
            on={telemetryOn}
            title="Whether the collector is sampling the hosts at all."
          >
            {telemetryOn ? "Telemetry collecting" : "Telemetry off"}
          </Status>
          <Status
            on={readsTelemetry}
            title={
              readsTelemetry
                ? "This policy receives a view carrying observed state, and its score depends on it."
                : "This policy receives a view with no telemetry fields on it at all. The observed trace is recorded, and cannot reach the placement decision."
            }
          >
            {readsTelemetry ? "Placement reads it" : "Placement can't read it"}
          </Status>
          <button
            type="button"
            onClick={toggle}
            aria-label={dark ? "Switch to light mode" : "Switch to dark mode"}
            aria-pressed={dark}
            title={dark ? "Light mode" : "Dark mode"}
            className="ml-1 grid h-9 w-9 place-items-center rounded-full text-declared transition-colors hover:bg-reservedLight hover:text-reserved"
          >
            {dark ? <IconSun /> : <IconMoon />}
          </button>
        </div>
      </div>
    </header>
  );
}
