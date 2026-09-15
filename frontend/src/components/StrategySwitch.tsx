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
 * pressure and watch the divergence strip's finding sentence and the decision
 * ledger change without touching anything else.
 *
 * Kept as its own component rather than folded into HeaderRail. HeaderRail is
 * pure display -- no state, no callbacks -- and every other panel on this page
 * keeps that same read-only contract. A control that owns a pending/error
 * state machine around a write is a different kind of thing, and shouldn't
 * quietly turn the one honestly-read-only component into a stateful one.
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
      setError(err instanceof ApiError ? `could not switch: ${err.status}` : "could not switch");
      setTimeout(() => setError(null), 4000);
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-ink/15 pt-3">
      <span className="legend text-declared">strategy</span>
      {available.map((name) => {
        const isActive = name === active;
        const isPending = pending === name;
        return (
          <button
            key={name}
            type="button"
            disabled={pending !== null}
            onClick={() => switchTo(name)}
            title={isActive ? "active" : `switch to ${name}`}
            className={[
              "border px-2.5 py-1 font-log text-[11px] transition-colors disabled:cursor-wait",
              isActive
                ? "border-ink bg-ink text-paper"
                : "border-ink/30 text-ink hover:border-ink/60 hover:bg-ink/5",
              isPending ? "opacity-60" : "",
            ].join(" ")}
          >
            {isPending ? `switching to ${name}…` : name}
          </button>
        );
      })}
      {error && <span className="legend text-observed">{error}</span>}
    </div>
  );
}
