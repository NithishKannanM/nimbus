#!/usr/bin/env bash
#
# Destroy the AWS stack. Pulls the run archives down first.
#
# docs/cost-model.md section 4 lists this as a non-negotiable cost control. The
# archives are fetched before the destroy because the artifact bucket is
# force_destroy -- which is what stops a redeploy failing on a non-empty bucket,
# and which also means the results go with it.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$here/infra"

if bucket="$(terraform output -raw s3_bucket 2>/dev/null)" && [[ -n "$bucket" ]]; then
  echo "pulling benchmark archives from s3://$bucket/benchmarks/"
  mkdir -p "$here/.nimbus/artifacts/benchmarks"
  aws s3 sync "s3://$bucket/benchmarks/" "$here/.nimbus/artifacts/benchmarks/" || {
    echo "WARNING: archive sync failed. Destroying now loses those runs." >&2
    read -r -p "continue anyway? [y/N] " reply
    [[ "$reply" == "y" ]] || exit 1
  }
fi

terraform destroy -input=false "$@"
rm -f "$here/.env.aws"
echo "destroyed. verify in the console: no running instances, no NAT, no idle endpoints."
