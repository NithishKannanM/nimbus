#!/bin/bash
# Pull the application release from S3 into /opt/nimbus.
#
# Runs as root from ExecStartPre on every start of the Nimbus unit, on the
# control plane and on every worker. scripts/demo/deploy.sh uploads the
# working tree to s3://$NIMBUS_S3_BUCKET/release/nimbus.tar.gz; this unpacks it.
#
# Why S3 and not SSH: workers have no public ingress at all (infra/networking.tf),
# so nothing on a laptop can reach them, and SSH would also need a key pair the
# defaults deliberately leave out. Both instance roles can already read the
# artifact bucket, and workers reach S3 through the VPC gateway endpoint.
#
# Exits non-zero while no release exists yet. The unit has Restart=always and no
# start limit, so a fresh instance simply retries until deploy.sh has uploaded.
#
# NIMBUS_S3_BUCKET and AWS_REGION come from /etc/nimbus.env via the unit's
# EnvironmentFile.
set -euo pipefail

key="release/nimbus.tar.gz"
app="${NIMBUS_APP_DIR:-/opt/nimbus}"     # overridable for local testing only
owner="${NIMBUS_APP_USER:-ec2-user}"
stamp="$app/.release-etag"

etag="$(aws s3api head-object --region "$AWS_REGION" --bucket "$NIMBUS_S3_BUCKET" \
  --key "$key" --query ETag --output text 2>/dev/null)" || {
  echo "no release at s3://$NIMBUS_S3_BUCKET/$key yet; run scripts/demo/deploy.sh" >&2
  exit 1
}

# Already running this release (a crash restart, or a reboot): skip the unpack.
if [[ -f "$stamp" && "$(cat "$stamp")" == "$etag" ]]; then
  exit 0
fi

# Unpack beside /opt/nimbus (same filesystem) and swap directories, so a failed
# download or a corrupt archive never leaves a half-written tree behind.
tmp="$(mktemp -d "$app-release.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

aws s3 cp --region "$AWS_REGION" --only-show-errors \
  "s3://$NIMBUS_S3_BUCKET/$key" "$tmp/release.tar.gz"
mkdir "$tmp/app"
tar -xzf "$tmp/release.tar.gz" -C "$tmp/app"

# Local state survives a new release.
if [[ -d "$app/.nimbus" ]]; then
  mv "$app/.nimbus" "$tmp/app/.nimbus"
fi
echo "$etag" > "$tmp/app/.release-etag"
chown -R "$owner:$owner" "$tmp/app"

rm -rf "$app.old"
if [[ -d "$app" ]]; then
  mv "$app" "$app.old"
fi
mv "$tmp/app" "$app"
rm -rf "$app.old"

echo "installed release $etag ($(head -1 "$app/RELEASE" 2>/dev/null || echo unknown))"
