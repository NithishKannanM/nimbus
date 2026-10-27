#!/usr/bin/env bash
#
# Live simulation against the AWS deployment: a stream of jobs submitted to the
# deployed API, plus CPU pressure on worker-1's own EC2 host (via SSM) that
# Nimbus never placed. Open the dashboard, start on fifo, switch to adaptive,
# and watch the Live strategy comparison.
#
#   AWS_PROFILE=nimbus-tf scripts/demo/simulate.sh                 # until Ctrl+C (pressure capped at 1 h)
#   AWS_PROFILE=nimbus-tf scripts/demo/simulate.sh --duration 600  # 10 minutes
#
# Extra arguments go to scripts/demo_dashboard.py (--rate, --target-worker, ...).
#
# Ctrl+C stops the job stream and the CPU hog. If this script dies anyway, the
# hog stops by itself on the instance when its timeout runs out.
#
# Cost: the stack itself is ~$0.10/hour in ap-southeast-2; a pinned t3.small
# worker adds roughly $0.08/hour of surplus CPU credits while this runs.
# Nothing here writes to the benchmark archive.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ -n "${NIMBUS_PYTHON:-}" ]]; then
  py="$NIMBUS_PYTHON"
elif [[ -x "$HOME/.venvs/nimbus/bin/python" ]]; then
  py="$HOME/.venvs/nimbus/bin/python"
else
  py="python3"
fi
"$py" -c "import boto3, httpx" 2>/dev/null || {
  echo "$py needs boto3 and httpx: $py -m pip install -r requirements-aws.txt" >&2
  exit 1
}

api="$(cd "$here/infra" && terraform output -raw api_url 2>/dev/null)" || {
  echo "no deployed stack found (terraform output api_url failed); run scripts/demo/deploy.sh first" >&2
  exit 1
}

echo "dashboard: $api/"
echo "switch the strategy from fifo to adaptive in the dashboard and watch the Live strategy comparison"
echo
PYTHONUNBUFFERED=1 exec "$py" "$here/scripts/demo_dashboard.py" --api "$api" --ssm-pressure "$@"
