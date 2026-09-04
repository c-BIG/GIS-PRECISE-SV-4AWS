#!/bin/bash
# Create S3 bucket for GATK-SV pipeline (inputs, outputs, config, references).
# Usage: ./01_create_buckets.sh <bucket-name> <profile> <region>
set -euo pipefail

BUCKET="${1:?Usage: $0 <bucket-name> <profile> <region>}"
PROFILE="${2:-default}"
REGION="${3:-ap-southeast-1}"

echo "Creating bucket: ${BUCKET} in ${REGION} (profile: ${PROFILE})"

if aws s3api head-bucket --bucket "$BUCKET" --profile "$PROFILE" --region "$REGION" 2>/dev/null; then
    echo "Bucket already exists — skipping creation."
else
    if [ "$REGION" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "$BUCKET" \
            --profile "$PROFILE" --region "$REGION"
    else
        aws s3api create-bucket --bucket "$BUCKET" \
            --create-bucket-configuration LocationConstraint="$REGION" \
            --profile "$PROFILE" --region "$REGION"
    fi
    echo "Bucket created."
fi

# Block public access (best practice)
aws s3api put-public-access-block --bucket "$BUCKET" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
    --profile "$PROFILE" --region "$REGION"

echo ""
echo "Done. Suggested prefix layout:"
echo "  s3://${BUCKET}/genome/gatk-sv/          <- reference files (step 4)"
echo "  s3://${BUCKET}/pipeline_config/          <- config files (step 11, ByobConfig)"
echo "  s3://${BUCKET}/<cram-prefix>/            <- input CRAMs (Byob)"
echo "  s3://${BUCKET}/<output-prefix>/          <- pipeline outputs (ByobOutput)"
echo "  s3://${BUCKET}/lambda_layer/             <- config layer zip (step 10)"
