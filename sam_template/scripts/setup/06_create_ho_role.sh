#!/bin/bash
# Create the HealthOmics workflow execution IAM role.
# HealthOmics assumes this role to read S3 inputs, write outputs, pull ECR images, write logs.
#
# Usage: ./06_create_ho_role.sh <role-name> <bucket> <account-id> <profile> <region> [kms-key-arn]
set -euo pipefail

ROLE_NAME="${1:?Usage: $0 <role-name> <bucket> <account-id> <profile> <region> [kms-key-arn]}"
BUCKET="${2:?Missing bucket}"
ACCOUNT_ID="${3:?Missing account-id}"
PROFILE="${4:-default}"
REGION="${5:-ap-southeast-1}"
KMS_ARN="${6:-}"

TRUST=$(cat << EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "omics.amazonaws.com"},
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": {"aws:SourceAccount": "${ACCOUNT_ID}"}
    }
  }]
}
EOF
)

# Build the permission policy
KMS_STMT=""
if [ -n "$KMS_ARN" ]; then
KMS_STMT=$(cat << EOF
,
    {
      "Effect": "Allow",
      "Action": ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
      "Resource": "${KMS_ARN}"
    }
EOF
)
fi

POLICY=$(cat << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": [
        "arn:aws:s3:::${BUCKET}",
        "arn:aws:s3:::${BUCKET}/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetAuthorizationToken"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams"
      ],
      "Resource": "arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:/aws/omics/*"
    }${KMS_STMT}
  ]
}
EOF
)

if aws iam get-role --role-name "$ROLE_NAME" --profile "$PROFILE" >/dev/null 2>&1; then
    echo "Role ${ROLE_NAME} already exists — updating policy."
else
    echo "Creating role ${ROLE_NAME}..."
    aws iam create-role --role-name "$ROLE_NAME" \
        --assume-role-policy-document "$TRUST" \
        --profile "$PROFILE" >/dev/null
fi

aws iam put-role-policy --role-name "$ROLE_NAME" \
    --policy-name "gatksv-healthomics-execution" \
    --policy-document "$POLICY" \
    --profile "$PROFILE"

ARN=$(aws iam get-role --role-name "$ROLE_NAME" --profile "$PROFILE" \
    --query 'Role.Arn' --output text)

echo ""
echo "Role ready:"
echo "  HealthOmicsExecutionRole=\"${ARN}\""
echo ""
echo "Set this in samconfig.toml."
