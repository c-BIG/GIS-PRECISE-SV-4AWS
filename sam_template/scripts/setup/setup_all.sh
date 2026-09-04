#!/bin/bash
# Master setup orchestrator for a new AWS account.
# Runs steps 2-11 in sequence with confirmation prompts.
# Steps 5 (docker mirror) and 8 (workflow registration) are the slowest.
#
# Usage: edit the CONFIG block below, then: ./setup_all.sh
set -euo pipefail

# ============================ CONFIG (edit these) ============================
PROFILE="myprofile"
REGION="ap-southeast-1"
ACCOUNT_ID="123456789012"

BUCKET="my-gatksv-bucket"          # main bucket for refs/config/outputs
REF_PREFIX="genome/gatk-sv"        # reference genome prefix
CONFIG_PREFIX="pipeline_config"    # ByobConfig prefix

TABLE_PREFIX="myorg-gatk-sv-dev"   # -> <prefix>-sample and <prefix>-batch-cohort
HO_ROLE_NAME="gatksv-healthomics-exec"
KMS_ARN=""                          # optional; leave empty if bucket not SSE-KMS

WDL_DIR="$(dirname "$0")/../../../wdl"   # adjust to your repo's wdl dir
# ============================================================================

SCRIPT_DIR="$(dirname "$0")"

confirm() {
    read -rp ">> $1 [y/N] " ans
    [[ "$ans" == "y" || "$ans" == "Y" ]]
}

echo "GATK-SV new-account setup — account ${ACCOUNT_ID}, region ${REGION}"
echo ""

if confirm "Step 2: Create S3 bucket ${BUCKET}?"; then
    "${SCRIPT_DIR}/01_create_buckets.sh" "$BUCKET" "$PROFILE" "$REGION"
fi

if confirm "Step 3: Create DynamoDB tables (${TABLE_PREFIX}-sample, -batch-cohort)?"; then
    "${SCRIPT_DIR}/02_create_dynamodb.sh" "$TABLE_PREFIX" "$PROFILE" "$REGION"
fi

if confirm "Step 4: Download references to s3://${BUCKET}/${REF_PREFIX}/? (slow)"; then
    "${SCRIPT_DIR}/03_setup_references.sh" "$BUCKET" "$REF_PREFIX" "$PROFILE" "$REGION"
fi

if confirm "Step 5: Mirror Docker images to ECR? (very slow)"; then
    python3 "${SCRIPT_DIR}/04_mirror_dockers.py" \
        --account-id "$ACCOUNT_ID" --region "$REGION" --profile "$PROFILE" --write-config
fi

if confirm "Step 6: Build & publish AWS CLI layer?"; then
    "${SCRIPT_DIR}/05_build_awscli_layer.sh" "$PROFILE" "$REGION"
fi

if confirm "Step 7: Create HealthOmics execution role ${HO_ROLE_NAME}?"; then
    "${SCRIPT_DIR}/06_create_ho_role.sh" "$HO_ROLE_NAME" "$BUCKET" "$ACCOUNT_ID" "$PROFILE" "$REGION" "$KMS_ARN"
fi

if confirm "Step 8: Register WDL workflows in HealthOmics? (slow)"; then
    python3 "${SCRIPT_DIR}/07_register_workflows.py" \
        --wdl-dir "$WDL_DIR" --region "$REGION" --profile "$PROFILE"
fi

if confirm "Step 9: Update config files (dockers, refs, FilterGenotypes) + patch template?"; then
    python3 "${SCRIPT_DIR}/08_update_configs.py" \
        --account-id "$ACCOUNT_ID" --region "$REGION" \
        --ref-bucket "$BUCKET" --ref-prefix "$REF_PREFIX" \
        --patch-template
fi

if confirm "Step 10: Build & upload config Lambda layer?"; then
    "${SCRIPT_DIR}/09_build_config_layer.sh" "$BUCKET" "$PROFILE" "$REGION"
fi

if confirm "Step 11: Sync config to S3 (s3://${BUCKET}/${CONFIG_PREFIX}/)?"; then
    "${SCRIPT_DIR}/../sync_config_to_s3.sh" "$BUCKET" "$CONFIG_PREFIX" "$PROFILE"
fi

echo ""
echo "============================================================"
echo "Setup steps complete. Remaining manual steps:"
echo "  12. Fill in samconfig.toml with the ARNs/values printed above"
echo "  13. cd sam_template && sam build && sam deploy --config-env <env>"
echo "============================================================"
