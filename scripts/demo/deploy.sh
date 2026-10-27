#!/usr/bin/env bash
#
# Provision the AWS stack and ship the working tree to it.
#
#   1. terraform apply           network, EC2 fleet, SQS, DynamoDB, S3, IAM, CloudWatch
#   2. package the working tree  -> s3://<bucket>/release/nimbus.tar.gz
#   3. restart running instances (SSM), so a redeploy picks the new release up
#   4. wait until the API reports every worker HEALTHY
#
# Every instance pulls the release from S3 before each start of its Nimbus unit
# (infra/user_data/fetch_release.sh). Workers have no public ingress at all, so
# nothing on this laptop could push code to them; S3 is the one place both
# sides can reach. Fresh instances retry until the upload lands.
#
# The working tree is shipped rather than a git clone. Every benchmark manifest
# records a git sha on the assumption that the deployed code *is* the code that
# produced it, and a clone opens a window in which those disagree: an
# uncommitted fix on the laptop, a run on the instance without it, and a
# manifest that says otherwise. The release carries a RELEASE file with the sha
# and whether the tree was dirty.
#
# Re-running this script is how you redeploy code changes. For a code-only
# change, `deploy.sh --code-only` skips Terraform and just ships the release.
#
# Cost: roughly $0.08/hour with the default three workers plus one control
# plane (docs/cost-model.md section 1). Run teardown.sh when you are done. That
# is not a suggestion; it is the cost control the ~$8 budget depends on.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

code_only=0
if [[ "${1:-}" == "--code-only" ]]; then
  code_only=1
  shift
fi

for tool in terraform aws tar curl python3; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed" >&2; exit 1; }
done
aws sts get-caller-identity >/dev/null 2>&1 || {
  echo "the aws CLI has no working credentials; run: aws configure" >&2
  exit 1
}

if [[ $code_only -eq 0 && -z "${TF_VAR_admin_cidr:-}" ]]; then
  cat >&2 <<'MSG'
TF_VAR_admin_cidr is not set.

It is the only thing standing between an unauthenticated job-submission API and
the internet (the API has no authentication before Phase 7). There is no default
on purpose, and 0.0.0.0/0 is rejected.

    export TF_VAR_admin_cidr="$(curl -s https://checkip.amazonaws.com)/32"
MSG
  exit 1
fi

# -- 1. infrastructure ---------------------------------------------------------
cd "$here/infra"
if [[ $code_only -eq 0 ]]; then
  terraform init -input=false
  terraform apply -input=false "$@"
else
  echo "code only: skipping terraform apply, using the existing stack"
fi

control_plane="$(terraform output -raw control_plane_public_ip)"
bucket="$(terraform output -raw s3_bucket)"
region="$(terraform output -json nimbus_env | python3 -c 'import json,sys; print(next(l.split("=",1)[1] for l in json.load(sys.stdin).splitlines() if l.startswith("AWS_REGION=")))')"
read -r -a instance_ids <<<"$(terraform output -json instance_ids | python3 -c 'import json,sys; print(" ".join(json.load(sys.stdin)))')"
worker_count="$(terraform output -json worker_ids | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
terraform output -raw nimbus_env > "$here/.env.aws"
echo "wrote $here/.env.aws"

# -- 2. package and upload -----------------------------------------------------
cd "$here"
if [[ -d frontend/node_modules ]]; then
  echo "building the dashboard"
  npm --prefix frontend run build >/dev/null
elif [[ ! -f frontend/dist/index.html ]]; then
  echo "WARNING: frontend/node_modules missing and no built dashboard; the API will run without it." >&2
  echo "         run 'npm --prefix frontend install' and deploy again to include it." >&2
fi

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
sha="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
dirty="$([[ -n "$(git status --porcelain 2>/dev/null)" ]] && echo " (dirty working tree)" || true)"
printf '%s%s\nbuilt %s by %s\n' "$sha" "$dirty" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(whoami)" > "$stage/RELEASE"

# Terraform state and .env files never leave this machine: state can hold
# secrets, and the instances get their configuration from user data.
tar -czf "$stage/nimbus.tar.gz" \
  --exclude=./.git --exclude=./.venv --exclude=./.nimbus --exclude=./results \
  --exclude=./frontend/node_modules --exclude=./infra/.terraform \
  --exclude='*.tfstate' --exclude='*.tfstate.*' --exclude='*.tfplan' \
  --exclude='./.env' --exclude='./.env.*' \
  --exclude='__pycache__' --exclude='.pytest_cache' \
  -C "$here" . -C "$stage" RELEASE
echo "release: $(head -1 "$stage/RELEASE"), $(du -h "$stage/nimbus.tar.gz" | cut -f1)"

# The bucket policy rejects unencrypted uploads (infra/s3.tf).
aws s3 cp --region "$region" --sse AES256 --only-show-errors \
  "$stage/nimbus.tar.gz" "s3://$bucket/release/nimbus.tar.gz"
echo "uploaded s3://$bucket/release/nimbus.tar.gz"

# -- 3. restart instances that are already running an older release ------------
# Best effort. Brand-new instances may not have registered with SSM yet; they
# pick the release up on their own because their unit keeps retrying.
# try-restart touches only units that are running, and each instance has
# exactly one of the two.
if aws ssm send-command --region "$region" \
  --instance-ids "${instance_ids[@]}" \
  --document-name AWS-RunShellScript \
  --comment "nimbus: install new release" \
  --parameters 'commands=["systemctl try-restart nimbus-api.service nimbus-worker.service || true"]' \
  --query Command.CommandId --output text >/dev/null 2>&1; then
  echo "asked running instances to restart onto the new release"
else
  echo "instances not reachable over SSM yet; they will pick the release up on boot"
fi

# -- 4. wait for the fleet -----------------------------------------------------
api="http://$control_plane:8000"
echo
echo "waiting for the API and $worker_count healthy workers (first boot takes a few minutes)"
deadline=$((SECONDS + 900))
status=""
while ((SECONDS < deadline)); do
  if body="$(curl -fs --max-time 5 "$api/healthz" 2>/dev/null)"; then
    status="$(python3 -c '
import json, sys
workers = json.loads(sys.argv[1]).get("workers", {})
healthy = sum(1 for s in workers.values() if s == "HEALTHY")
print(f"{healthy}/{len(workers)} workers healthy: " + ", ".join(f"{w}={s}" for w, s in sorted(workers.items())))
' "$body")"
    echo "  $status"
    [[ "$status" == "$worker_count/$worker_count "* ]] && break
  else
    echo "  API not answering yet"
  fi
  sleep 15
done

echo
if [[ "$status" == "$worker_count/$worker_count "* ]]; then
  echo "Nimbus is up on AWS."
else
  echo "WARNING: the fleet is not fully healthy after 15 minutes. Inspect an instance with:" >&2
  echo "  aws ssm start-session --region $region --target <instance-id>" >&2
  echo "  journalctl -u nimbus-api -u nimbus-worker -n 100" >&2
  echo "instance ids: ${instance_ids[*]}" >&2
fi
echo "Dashboard: $api/"
echo "API docs:  $api/docs"
echo "Teardown:  scripts/demo/teardown.sh   (do not skip this; it stops the billing)"
