#!/usr/bin/env bash
#
# Provision the AWS stack and ship the working tree to it.
#
# The tree is rsynced rather than cloned. Every benchmark manifest records a git
# sha on the assumption that the deployed code *is* the code that produced it,
# and a clone opens a window in which those disagree -- an uncommitted fix on
# the laptop, a run on the instance without it, and a manifest that says
# otherwise.
#
# Cost: roughly $0.08/hour with the default three workers plus one control
# plane (docs/cost-model.md section 1). Run teardown.sh when you are done. That
# is not a suggestion; it is the cost control the ~$8 budget depends on.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$here/infra"

if [[ -z "${TF_VAR_admin_cidr:-}" ]]; then
  cat >&2 <<'MSG'
TF_VAR_admin_cidr is not set.

It is the only thing standing between an unauthenticated job-submission API and
the internet (the API has no authentication before Phase 7). There is no default
on purpose, and 0.0.0.0/0 is rejected.

    export TF_VAR_admin_cidr="$(curl -s https://checkip.amazonaws.com)/32"
MSG
  exit 1
fi

terraform init -input=false
terraform apply -input=false "$@"

control_plane="$(terraform output -raw control_plane_public_ip)"
echo
echo "control plane: $control_plane"
terraform output -raw nimbus_env > "$here/.env.aws"
echo "wrote $here/.env.aws"

# Ship the tree. --delete so a file removed locally is removed there too;
# otherwise a deleted module keeps running on the instance.
rsync -az --delete \
  --exclude '.venv' --exclude '.git' --exclude '.nimbus' \
  --exclude '__pycache__' --exclude '.pytest_cache' --exclude 'frontend/node_modules' \
  -e "ssh -o StrictHostKeyChecking=accept-new" \
  "$here/" "ec2-user@$control_plane:/opt/nimbus/"

echo
echo "API:      http://$control_plane:8000/docs"
echo "teardown: scripts/demo/teardown.sh"
