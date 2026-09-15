/**
 * The Nimbus API, typed.
 *
 * The types here mirror the pydantic models rather than being invented for the
 * UI, and in particular they keep DECLARED, RESERVED and OBSERVED as three
 * separate objects on `WorkerState`. Flattening them into one comfortable
 * `{cpu, memory}` shape for rendering would quietly undo the distinction the
 * whole project rests on -- and the dashboard's job is to make that split
 * visible, not to smooth it over.
 */

const BASE = import.meta.env.DEV ? "/api" : "";

export type WorkerStatus = "HEALTHY" | "UNHEALTHY" | "DRAINING" | "DEAD";

export type JobStatus =
  | "QUEUED"
  | "SCHEDULED"
  | "RUNNING"
  | "COMPLETED"
  | "FAILED"
  | "RETRYING"
  | "TIMED_OUT";

/** DECLARED -- operator configuration. Never inferred from telemetry. */
export interface DeclaredCapacity {
  cpu_units: number;
  memory_mb: number;
  slots: number;
}

/** RESERVED -- Nimbus's own bookkeeping. */
export interface ReservedResources {
  cpu_units: number;
  memory_mb: number;
  active_jobs: number;
}

/** OBSERVED -- psutil. Null when telemetry is off, and that is meaningful. */
export interface WorkerTelemetry {
  worker_id: string;
  timestamp: string;
  cpu_percent: number;
  memory_percent: number;
  process_memory_mb: number;
  active_jobs: number;
  available_memory_mb: number;
  load_average_1m: number;
}

export interface WorkerState {
  worker_id: string;
  status: WorkerStatus;
  declared: DeclaredCapacity;
  reserved: ReservedResources;
  observed: WorkerTelemetry | null;
  observed_window: {
    cpu_percent_mean: number;
    cpu_percent_latest: number;
    cpu_trend_pct_per_s: number;
    samples: number;
    span_s: number;
  } | null;
  heartbeat_age_s: number;
  telemetry_age_s: number | null;
}

export interface Job {
  job_id: string;
  job_type: string;
  workload_class: string;
  status: JobStatus;
  priority: number;
  assigned_worker: string | null;
  attempts: number;
  max_attempts: number;
  cpu_units: number;
  memory_mb: number;
  created_at: string;
  completed_at: string | null;
  execution_time_ms: number | null;
  error: string | null;
}

export interface Candidate {
  eligible: boolean;
  ineligible_reason: string | null;
  healthy: boolean;
  declared_cpu_units: number;
  declared_memory_mb: number;
  declared_slots: number;
  reserved_cpu_units: number;
  reserved_memory_mb: number;
  active_jobs: number;
  observed: Record<string, number | null> | null;
  score: number | null;
  terms: Record<string, number>;
}

export interface Decision {
  decision_id: string;
  job_id: string;
  strategy: string;
  ordering_policy: string;
  placement_policy: string;
  algorithm_version: string;
  telemetry_enabled: boolean;
  weights: Record<string, number>;
  candidates: Record<string, Candidate>;
  selected_worker: string | null;
  runner_up: string | null;
  margin: number | null;
  reason: string;
  decision_latency_us: number;
  decided_at: string;
}

export interface SchedulerState {
  strategy: string;
  ordering_policy: string;
  placement_policy: string;
  algorithm_version: string;
  available: string[];
  telemetry_enabled: boolean;
  submitted: number;
  completed: number;
  failed: number;
  retried: number;
  timed_out: number;
  worker_lost: number;
  dead_lettered: number;
  no_candidate: number;
  dispatch_rejected: number;
  queue_visible: number;
  queue_in_flight: number;
  dead_letters: number;
  status_counts: Record<JobStatus, number>;
  telemetry_overhead: Record<string, number>;
}

export interface BenchmarkRunRow {
  run_id: string;
  profile: string;
  strategy: string;
  algorithm_version: string;
  jobs_submitted: number;
  jobs_completed: number;
  truncated: boolean;
  makespan_s: number | null;
  throughput_jobs_s: number | null;
  mean_fleet_utilisation: number | null;
  load_regime_ok: boolean;
  started_at: string;
}

/** The external pressure a benchmark profile applies, outside Nimbus entirely --
 *  no lease, no slot, no reservation. See `GET /benchmarks/profiles`. */
export interface ProfilePressureSpec {
  kind: "cpu" | "memory";
  target_worker: string;
  intensity: number;
}

export interface BenchmarkProfile {
  name: string;
  description: string;
  arrival: string;
  rate_per_s: number;
  size_distribution: string;
  mix: Record<string, number>;
  pressure: ProfilePressureSpec[];
}

export interface SchedulerSwitchResult {
  strategy: string;
  ordering_policy: string;
  placement_policy: string;
  available: string[];
}

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${BASE}${path}`, { signal });
  if (!response.ok) {
    throw new ApiError(response.status, `${path} -> ${response.status}`);
  }
  return (await response.json()) as T;
}

async function put<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    throw new ApiError(response.status, `${path} -> ${response.status}`);
  }
  return (await response.json()) as T;
}

export const api = {
  workers: (signal?: AbortSignal) => get<WorkerState[]>("/workers", signal),
  scheduler: (signal?: AbortSignal) => get<SchedulerState>("/scheduler", signal),
  jobs: (limit = 50, signal?: AbortSignal) =>
    get<Job[]>(`/jobs?limit=${limit}`, signal),
  decisions: (limit = 25, signal?: AbortSignal) =>
    get<Decision[]>(`/scheduler/decisions?limit=${limit}`, signal),
  telemetryHistory: (workerId: string, limit = 60, signal?: AbortSignal) =>
    get<WorkerTelemetry[]>(
      `/workers/${workerId}/telemetry?limit=${limit}`,
      signal,
    ),
  benchmarkRuns: (signal?: AbortSignal) =>
    get<BenchmarkRunRow[]>("/benchmarks/runs?limit=200", signal),
  profiles: (signal?: AbortSignal) =>
    get<BenchmarkProfile[]>("/benchmarks/profiles", signal),
  /** PUT /scheduler -- unauthenticated by design, and the only write this
   *  dashboard makes. It swaps which placement policy runs next; it touches no
   *  job, no worker, no queue state. */
  setStrategy: (strategy: string, signal?: AbortSignal) =>
    put<SchedulerSwitchResult>("/scheduler", { strategy }, signal),
};

/** Whether the active placement policy is permitted to read telemetry.
 *
 * `adaptive` is the only policy that consumes observed state; the static
 * policies receive a view with no telemetry fields on it at all. The dashboard
 * shows telemetry either way, so without this the two cases would look
 * identical on screen -- and "telemetry is displayed" would be mistaken for
 * "telemetry is being used". */
export function placementReadsTelemetry(policy: string): boolean {
  return policy.startsWith("adaptive");
}
