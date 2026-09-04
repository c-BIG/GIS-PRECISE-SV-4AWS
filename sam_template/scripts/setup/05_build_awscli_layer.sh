#!/bin/bash
# Build and publish the AWS CLI v1 Lambda layer (provides /opt/bin/aws for submit Lambdas).
# Usage: ./05_build_awscli_layer.sh <profile> <region>
set -euo pipefail

PROFILE="${1:-default}"
REGION="${2:-ap-southeast-1}"

LAYER_DIR=$(mktemp -d)
trap 'rm -rf "$LAYER_DIR"' EXIT

echo "Building AWS CLI layer..."
mkdir -p "${LAYER_DIR}/bin"
pip install awscli -t "${LAYER_DIR}/python" --no-cache-dir --quiet

# Wrapper that sets PYTHONPATH so the subprocess python finds awscli under /opt/python
cat > "${LAYER_DIR}/bin/aws" << 'EOF'
#!/bin/bash
export PYTHONPATH="/opt/python:${PYTHONPATH}"
exec python3 -c "import sys; from awscli.clidriver import main; sys.exit(main())" "$@"
EOF
chmod +x "${LAYER_DIR}/bin/aws"

cd "$LAYER_DIR"
zip -r -q /tmp/awscli-layer.zip bin/ python/

echo "Publishing layer..."
ARN=$(aws lambda publish-layer-version \
    --layer-name awscli \
    --description "AWS CLI v1 for HealthOmics start-run subprocess calls" \
    --zip-file fileb:///tmp/awscli-layer.zip \
    --compatible-runtimes python3.12 \
    --region "$REGION" --profile "$PROFILE" \
    --query 'LayerVersionArn' --output text)

rm -f /tmp/awscli-layer.zip

echo ""
echo "Layer published:"
echo "  AwsCliLayerArn=\"${ARN}\""
echo ""
echo "Set this in samconfig.toml."
