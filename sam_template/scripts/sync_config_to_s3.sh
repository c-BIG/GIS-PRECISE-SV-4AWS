#!/bin/bash
# Sync the local config/ directory to S3 so the runtime loader (config_loader.py)
# can pick it up. S3 config takes priority over the baked-in Lambda layer, so this
# lets you edit config without redeploying.
#
# The runtime reads (see gatksv_healthomics/shared/python/config_loader.py):
#   s3://<bucket>/<prefix>/<name>.json            e.g. pipeline_config/docker_images.json
#   s3://<bucket>/<prefix>/templates/<stage>.json e.g. pipeline_config/templates/FilterGenotypes.json
#
# Usage: ./sync_config_to_s3.sh <bucket> [config-prefix] [profile] [region]
set -euo pipefail

BUCKET="${1:?Usage: $0 <bucket> [config-prefix] [profile] [region]}"
CONFIG_PREFIX="${2:-pipeline_config}"
PROFILE="${3:-default}"
REGION="${4:-ap-southeast-1}"

# config/ lives under the shared python dir (this script is in scripts/, one level
# above scripts/setup/).
CONFIG_DIR="$(dirname "$0")/../gatksv_healthomics/shared/python/config"

if [ ! -d "$CONFIG_DIR" ]; then
    echo "ERROR: config dir not found at $CONFIG_DIR" >&2
    exit 1
fi

DEST="s3://${BUCKET}/${CONFIG_PREFIX}"

echo "Syncing config:"
echo "  from: ${CONFIG_DIR}"
echo "  to:   ${DEST}"
echo "  (profile: ${PROFILE}, region: ${REGION})"
echo ""

# Sync the whole config/ tree (top-level *.json + templates/*.json), preserving the
# templates/ subdir the loader expects. --delete keeps S3 in lockstep with local so
# stale config can't linger. Exclude non-config files.
aws s3 sync "$CONFIG_DIR" "$DEST" \
    --delete \
    --exclude "*" \
    --include "*.json" \
    --exclude "*/__pycache__/*" \
    --exclude "README.md" \
    --exclude "workflow_ids_TEMPLATE.json" \
    --profile "$PROFILE" --region "$REGION"

echo ""
echo "Done. Config synced to ${DEST}/"
echo "Ensure the submit Lambdas have env vars:"
echo "  CONFIG_BUCKET=\"${BUCKET}\""
echo "  CONFIG_PREFIX=\"${CONFIG_PREFIX}\""
