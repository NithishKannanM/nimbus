import { useState } from "react";

import { api, ApiError } from "../lib/api";

/**
 * The one write this dashboard makes.
 *
 * `PUT /scheduler` is unauthenticated by design (the scheduler router is
 * mounted unconditionally, unlike the admin router) and touches nothing but
 * which placement policy runs next -- no job, no worker, no queue state. That
 * makes it safe to expose directly, and it is also the single most legible way
 * to show how Nimbus works: flip `fifo` to `adaptive` while a worker is under
 * pressure and watch the finding sentence and the decision ledger change
 * without touching anything else.
 *
 * Kept as its own component so every display panel stays read-only.
 */
export function StrategySwitch({
  available,
  active,
  onSwitched,
}: {
  available: string[];
  active: string;
  onSwitched: () => void;
}) {
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function switchTo(name: string) {
    if (name === active || pending !== null) return;
    setPending(name);
    setError(null);
    try {
      await api.setStrategy(name);
      onSwitched();
    } catch (err) {
      setError(err instanceof ApiError ? `Could not switch: ${err.status}` : "Could not switch");
      setTimeout(() => setError(null), 4000);
    } finally {
      setPending(null);
    }
  }

  return (
    <div role="group" aria-labelledby="strategy-l" className="flex flex-col items-start gap-2 sm:items-end">
      <span id="strategy-l" className="sr-only">
        Scheduling strategy
      </span>
      <div className="flex flex-wrap gap-1 rounded-card bg-muted p-1">
        {available.map((name) => {
          const isActive = name === active;
          const isPending = pending === name;
          return (
            <button
              key={name}
              type="button"
              disabled={pending !== null}
              aria-pressed={isActive}
              onClick={() => switchTo(name)}
              title={isActive ? "Active" : `Switch to ${name}`}
              className={[
                "min-h-[36px] rounded-[5px] px-3.5 text-[13px] font-medium transition-colors disabled:cursor-wait",
                isActive
                  ? "bg-reserved text-white shadow-sm"
                  : "text-declared hover:bg-paper hover:text-ink",
                isPending ? "opacity-60" : "",
              ].join(" ")}
            >
              {isPending ? `Switching…` : name}
            </button>
          );
        })}
      </div>
      <span role="status" aria-live="polite" className="text-[12px] font-semibold text-observedText">
        {error}
      </span>
    </div>
  );
}
