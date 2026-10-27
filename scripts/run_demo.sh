#!/usr/bin/env bash
#
# One command for the live dashboard demo, locally:
#
#   1. the Nimbus API + scheduler + 3 local workers   (uvicorn, :8000)
#   2. a job stream with CPU pressure on worker-1      (scripts/demo_dashboard.py)
#   3. the dashboard                                   (vite dev server, :5173)
#
# Ctrl+C stops all three, including the CPU stressor.
#
# Usage:
#   scripts/run_demo.sh                 # start on fifo, then switch to adaptive in the UI
#   scripts/run_demo.sh --no-load       # API + dashboard only, no jobs, no pressure
#   scripts/run_demo.sh --built         # serve the built dashboard from the API (one port)
#
# Environment:
#   NIMBUS_PYTHON    python to use (default: ~/.venvs/nimbus, then .venv, then python3)
#   NIMBUS_STRATEGY  starting strategy (default: fifo)
#   API_PORT / UI_PORT   (default: 8000 / 5173)
#   DEMO_RATE        jobs submitted per second (default: 1.2)
#
# Nothing here touches .nimbus/artifacts/benchmarks/: the demo stream is not
# evidence and never lands in the archive (see scripts/demo_dashboard.py).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

load=1
built=0
for arg in "$@"; do
  case "$arg" in
    --no-load) load=0 ;;
    --built) built=1 ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

API_PORT="${API_PORT:-8000}"
UI_PORT="${UI_PORT:-5173}"
export NIMBUS_STRATEGY="${NIMBUS_STRATEGY:-fifo}"

# The repo's own .venv lives on NTFS and is broken there; the working venv is on
# ext4 at ~/.venvs/nimbus. Fall back so the script still works elsewhere.
if [[ -n "${NIMBUS_PYTHON:-}" ]]; then
  py="$NIMBUS_PYTHON"
elif [[ -x "$HOME/.venvs/nimbus/bin/python" ]]; then
  py="$HOME/.venvs/nimbus/bin/python"
elif [[ -x "$root/.venv/bin/python" ]]; then
  py="$root/.venv/bin/python"
else
  py="python3"
fi
"$py" -c "import fastapi, uvicorn, httpx, psutil" 2>/dev/null || {
  echo "$py is missing dependencies. Install them with:" >&2
  echo "    $py -m pip install -r requirements.txt" >&2
  exit 1
}

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
for port in "$API_PORT" $([[ $built -eq 0 ]] && echo "$UI_PORT"); do
  if port_busy "$port"; then
    echo "port $port is already in use. Stop whatever is on it first, e.g.:" >&2
    echo "    fuser -k $port/tcp" >&2
    exit 1
  fi
done

if [[ $built -eq 1 || ! -d frontend/node_modules ]]; then
  command -v npm >/dev/null || { echo "npm not found; install Node.js" >&2; exit 1; }
fi
if [[ ! -d frontend/node_modules ]]; then
  echo "installing dashboard dependencies (first run only)"
  npm --prefix frontend install --no-audit --no-fund
fi
if [[ $built -eq 1 ]]; then
  echo "building the dashboard"
  npm --prefix frontend run build >/dev/null
fi

logs="$root/.nimbus/demo-logs"
mkdir -p "$logs"
pids=()

cleanup() {
  trap - EXIT INT TERM
  echo
  echo "stopping demo..."
  # Each child leads its own process group, so this also stops uvicorn's
  # worker processes, the CPU stressor and vite's esbuild.
  for pid in "${pids[@]}"; do kill -TERM -- "-$pid" 2>/dev/null || true; done
  sleep 1
  for pid in "${pids[@]}"; do kill -KILL -- "-$pid" 2>/dev/null || true; done
  echo "stopped. logs are in ${logs#"$root"/}/"
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

echo "starting API on :$API_PORT (strategy $NIMBUS_STRATEGY)"
setsid "$py" -m uvicorn backend.api.app:app --port "$API_PORT" >"$logs/api.log" 2>&1 &
pids+=($!)

for _ in $(seq 1 60); do
  curl -fs "http://127.0.0.1:$API_PORT/healthz" >/dev/null 2>&1 && break
  if ! kill -0 "${pids[0]}" 2>/dev/null; then
    echo "API failed to start; last lines of $logs/api.log:" >&2
    tail -20 "$logs/api.log" >&2
    exit 1
  fi
  sleep 0.5
done
curl -fs "http://127.0.0.1:$API_PORT/healthz" >/dev/null || { echo "API did not become healthy" >&2; exit 1; }

if [[ $load -eq 1 ]]; then
  echo "starting job stream + CPU pressure on worker-1"
  setsid "$py" scripts/demo_dashboard.py --api "http://127.0.0.1:$API_PORT" \
    --rate "${DEMO_RATE:-1.2}" >"$logs/load.log" 2>&1 &
  pids+=($!)
fi

if [[ $built -eq 1 ]]; then
  url="http://127.0.0.1:$API_PORT/"
else
  NIMBUS_API="http://127.0.0.1:$API_PORT" setsid npm --prefix frontend run dev -- \
    --port "$UI_PORT" --strictPort >"$logs/ui.log" 2>&1 &
  pids+=($!)
  url="http://localhost:$UI_PORT/"
  for _ in $(seq 1 40); do port_busy "$UI_PORT" && break; sleep 0.5; done
fi

cat <<MSG

  Dashboard:  $url
  API docs:   http://127.0.0.1:$API_PORT/docs

  Demo: worker-1 is pinned at 100% CPU by load Nimbus did not place.
  1. On fifo, watch jobs still land on worker-1 (Decision ledger, Jobs table).
  2. Click "adaptive" in the strategy switch.
  3. Watch the Live strategy comparison: fewer jobs to the busy worker, shorter execution.

  Ctrl+C to stop everything.
MSG

# Exit (and clean up) as soon as any part dies, so a crash is not mistaken for
# a quiet demo.
while true; do
  for pid in "${pids[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "a demo process exited unexpectedly; see ${logs#"$root"/}/" >&2
      exit 1
    fi
  done
  sleep 2
done
