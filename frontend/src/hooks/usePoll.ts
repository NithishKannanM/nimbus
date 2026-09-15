import { useCallback, useEffect, useRef, useState } from "react";

export interface PollResult<T> {
  data: T | null;
  error: Error | null;
  /** True only for the first load, so a refresh does not blank the screen. */
  loading: boolean;
  /** Consecutive failures. Surfaced so a dead API is visible rather than
   *  presenting as a dashboard that has quietly stopped moving. */
  failures: number;
  refresh: () => void;
}

/**
 * Poll an endpoint on an interval.
 *
 * Polling rather than websockets, deliberately. The control plane is a single
 * scheduler loop whose throughput is already a documented bottleneck
 * (docs/limitations.md §2); adding a push-notification fan-out to it would put
 * dashboard traffic on the critical path of placement decisions. A 2 s poll of
 * three read-only endpoints is not a load worth engineering around.
 *
 * Stale data is kept on failure. A blank panel and a stale panel look very
 * different to an operator: the first says "nothing is happening", the second
 * says "I cannot reach the API", and only one of those is true.
 */
export function usePoll<T>(
  fetcher: (signal?: AbortSignal) => Promise<T>,
  intervalMs = 2000,
  enabled = true,
): PollResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [failures, setFailures] = useState(0);
  const [tick, setTick] = useState(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;

    const run = async () => {
      try {
        const value = await fetcher(controller.signal);
        if (!mounted.current) return;
        setData(value);
        setError(null);
        setFailures(0);
      } catch (err) {
        if (controller.signal.aborted || !mounted.current) return;
        setError(err as Error);
        setFailures((n) => n + 1);
        // data is deliberately left alone -- see the docstring.
      } finally {
        if (mounted.current) setLoading(false);
        // Chained rather than setInterval: a slow response must not stack
        // requests on a control plane that is already struggling.
        if (mounted.current && !controller.signal.aborted) {
          timer = setTimeout(run, intervalMs);
        }
      }
    };

    void run();
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [fetcher, intervalMs, enabled, tick]);

  return { data, error, loading, failures, refresh };
}
