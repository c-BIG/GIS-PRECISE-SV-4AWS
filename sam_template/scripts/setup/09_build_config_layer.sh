#!/bin/bash
# Build the config Lambda layer (bundles config/ dir as a fallback for runtime) and upload to S3.
# Usage: ./09_build_config_layer.sh <bucket> <profile> <region> [layer-s3-key]
set -euo pipefail

BUCKET="${1:?Usage: $0 <bucket> <profile> <region> [layer-s3-key]}"
PROFILE="${2:-default}"
REGION="${3:-ap-southeast-1}"
LAYER_KEY="${4:-lambda_layer/gatksv_config_layer.zip}"

SHARED_DIR="$(dirname "$0")/../../gatksv_healthomics/shared"
BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT

# Lambda Python layers expect code under python/
mkdir -p "${BUILD_DIR}/python"
cp -r "${SHARED_DIR}/python/"* "${BUILD_DIR}/python/"

cd "$BUILD_DIR"
zip -r -q /tmp/gatksv_config_layer.zip python/

echo "Uploading config layer to s3://${BUCKET}/${LAYER_KEY}"
aws s3 cp /tmp/gatksv_config_layer.zip "s3://${BUCKET}/${LAYER_KEY}" \
    --profile "$PROFILE" --region "$REGION"

rm -f /tmp/gatksv_config_layer.zip

echo ""
echo "Set in samconfig.toml:"
echo "  ConfigLayerS3Bucket=\"${BUCKET}\""
echo "  ConfigLayerS3Key=\"${LAYER_KEY}\""
