#!/bin/bash
# Create the two DynamoDB tables required by GATK-SV (sample + aggregates), each with a stream.
# The SAM template does NOT create these — it consumes their ARNs as parameters.
#
# Usage: ./02_create_dynamodb.sh <table-prefix> <profile> <region>
#   e.g. ./02_create_dynamodb.sh jagc-gatk-sv-dev npm ap-southeast-1
#   Creates: <prefix>-sample and <prefix>-batch-cohort
set -euo pipefail

PREFIX="${1:?Usage: $0 <table-prefix> <profile> <region>}"
PROFILE="${2:-default}"
REGION="${3:-ap-southeast-1}"

SAMPLE_TABLE="${PREFIX}-sample"
AGG_TABLE="${PREFIX}-batch-cohort"

create_table() {
    local table="$1"
    if aws dynamodb describe-table --table-name "$table" --profile "$PROFILE" --region "$REGION" >/dev/null 2>&1; then
        echo "Table ${table} already exists — skipping."
        return
    fi
    echo "Creating table: ${table}"
    aws dynamodb create-table \
        --table-name "$table" \
        --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S \
        --key-schema AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE \
        --billing-mode PAY_PER_REQUEST \
        --stream-specification StreamEnabled=true,StreamViewType=NEW_IMAGE \
        --profile "$PROFILE" --region "$REGION" >/dev/null
    aws dynamodb wait table-exists --table-name "$table" --profile "$PROFILE" --region "$REGION"
    echo "  ${table} ready."
}

create_table "$SAMPLE_TABLE"
create_table "$AGG_TABLE"

echo ""
echo "=========================================================="
echo "Copy these into samconfig.toml parameter_overrides:"
echo "=========================================================="
for t in "$SAMPLE_TABLE" "$AGG_TABLE"; do
    ARN=$(aws dynamodb describe-table --table-name "$t" --profile "$PROFILE" --region "$REGION" \
        --query 'Table.TableArn' --output text)
    STREAM=$(aws dynamodb describe-table --table-name "$t" --profile "$PROFILE" --region "$REGION" \
        --query 'Table.LatestStreamArn' --output text)
    if [ "$t" = "$SAMPLE_TABLE" ]; then
        echo "DynamoDBName=\"${t}\""
        echo "DynamoDBArn=\"${ARN}\""
        echo "DynamoDBStreamArn=\"${STREAM}\""
    else
        echo "AggregatesDynamoDBName=\"${t}\""
        echo "AggregatesDynamoDBArn=\"${ARN}\""
        echo "AggregatesDynamoDBStreamArn=\"${STREAM}\""
    fi
done
echo "=========================================================="
