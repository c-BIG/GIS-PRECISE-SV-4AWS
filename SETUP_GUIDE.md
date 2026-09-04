# GATK-SV on AWS — New Account Setup Guide

This guide walks through deploying the GATK-SV HealthOmics pipeline (GATK-SV v1.1.1) in a **new AWS account** from scratch. It covers the parent stack and the HealthOmics compute backend.

---

## Overview of Setup Steps

| # | Step | Automated? | One-time? |
|---|------|-----------|-----------|
| 1 | Prerequisites (tools, AWS CLI, SAM CLI) | Manual | Yes |
| 2 | Create S3 buckets | `01_create_buckets.sh` | Yes |
| 3 | Create DynamoDB tables (+ streams) | `02_create_dynamodb.sh` | Yes |
| 4 | Download & upload reference genome to S3 | `03_setup_references.sh` | Yes |
| 5 | Mirror Docker images to ECR | `04_mirror_dockers.py` | Yes |
| 6 | Build & publish AWS CLI Lambda layer | `05_build_awscli_layer.sh` | Yes |
| 7 | Create HealthOmics execution IAM role | `06_create_ho_role.sh` | Yes |
| 8 | Register WDL workflows in HealthOmics | `07_register_workflows.py` | Yes |
| 9 | Update config files (dockers, refs, workflow IDs) | `08_update_configs.py` | Yes |
| 10 | Build & upload config Lambda layer | `09_build_config_layer.sh` | Per config change |
| 11 | Sync config to S3 | `sync_config_to_s3.sh` | Per config change |
| 12 | Configure samconfig.toml | Manual | Per environment |
| 13 | Deploy the stack | `sam build && sam deploy` | Per change |

Run `setup_all.sh` to execute steps 2-11 in sequence (with confirmation prompts).

---

## Step 1: Prerequisites

Install on your workstation (or an EC2 instance in the target account):

```bash
# AWS CLI v2
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip awscliv2.zip && sudo ./aws/install

# AWS SAM CLI
# https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html

# Docker (for mirroring images and building layers)
# https://docs.docker.com/engine/install/

# Python 3.12 + boto3
pip install boto3

# Configure AWS credentials for the target account
aws configure --profile <your-profile>
```

Confirm access:
```bash
aws sts get-caller-identity --profile <your-profile>
```

---

## Step 2: Create S3 Buckets

The pipeline uses buckets for: CRAM inputs, pipeline outputs, config, and references. These can be the same bucket with different prefixes, or separate buckets.

```bash
./scripts/setup/01_create_buckets.sh <bucket-name> <profile> <region>
```

---

## Step 3: Create DynamoDB Tables

**CRITICAL**: The SAM template does NOT create the DynamoDB tables — they must exist beforehand and their ARNs are passed as parameters. Two tables are needed:

1. **Sample table** — `pk` (String), `sk` (String), with a DynamoDB Stream (NEW_IMAGE)
2. **Aggregates (batch/cohort) table** — `pk` (String), `sk` (String), with a DynamoDB Stream (NEW_IMAGE)

```bash
./scripts/setup/02_create_dynamodb.sh <table-prefix> <profile> <region>
# e.g. ./scripts/setup/02_create_dynamodb.sh jagc-gatk-sv-dev npm ap-southeast-1
# Creates: jagc-gatk-sv-dev-sample and jagc-gatk-sv-dev-batch-cohort
```

The script outputs the table ARNs and Stream ARNs — copy these into `samconfig.toml`.

---

## Step 4: Reference Genome

Source: https://github.com/broadinstitute/gatk-sv/blob/main/inputs/values/resources_hg38.json

The reference files are hosted on Google Cloud Storage (gs://) by Broad. Download them and re-upload to your S3 bucket.

```bash
./scripts/setup/03_setup_references.sh <s3-bucket> <s3-prefix> <profile>
# e.g. ./scripts/setup/03_setup_references.sh my-bucket genome/gatk-sv npm
```

This downloads from the public GCS/HTTPS mirrors and uploads to `s3://<bucket>/<prefix>/`. Requires `gsutil` (or the script falls back to `curl` for HTTPS-accessible files).

---

## Step 5: Mirror Docker Images to ECR

Source (pinned to stable **v1.1.1** tag): https://github.com/broadinstitute/gatk-sv/blob/v1.1.1/inputs/values/dockers.json

This repo targets **GATK-SV v1.1.1**. All images are stock Broad images — no custom rebuilds needed. Broad's images are on public registries (us.gcr.io, marketplace.gcr.io) which AWS HealthOmics cannot pull from directly, so mirror them to your ECR.

```bash
python3 scripts/setup/04_mirror_dockers.py \
  --account-id <aws-account-id> \
  --region ap-southeast-1 \
  --profile <profile> \
  --write-config
```

This:
1. Fetches Broad's `dockers.json` pinned to `v1.1.1` (do NOT use `main` — it moves)
2. Creates ECR repos, pulls/retags/pushes each image
3. With `--write-config`, regenerates `docker_images.json` pointing at your ECR

**Note — one AWS-specific image not in Broad's list**: `manifest_reader_docker` (amazonlinux + aws cli) is needed by the HealthOmics `ReadManifest` task. Build and push it separately using `dockerfile/dockerfile.awscli`:
```bash
docker build -t <ecr>/gatk-sv/awscli:v2.34.25 -f dockerfile/dockerfile.awscli .
docker push <ecr>/gatk-sv/awscli:v2.34.25
```

A reference of the expected config is at `scripts/setup/docker_images.v1.1.1.template.json`.

---

## Step 6: AWS CLI Lambda Layer

The submit Lambdas call `/opt/bin/aws omics start-run` via subprocess (avoids float→scientific-notation JSON mangling). This requires an AWS CLI layer in the target account and region.

```bash
./scripts/setup/05_build_awscli_layer.sh <profile> <region>
```

Note the layer ARN from the output → set `AwsCliLayerArn` in samconfig.toml.

See `sam_template/lambda_layer/README.md` for details.

---

## Step 7: HealthOmics Execution IAM Role

HealthOmics needs a role to read inputs from S3, write outputs, pull ECR images, and write logs.

```bash
./scripts/setup/06_create_ho_role.sh <role-name> <bucket> <account-id> <profile> <region>
```

Note the role ARN → set `HealthOmicsExecutionRole` in samconfig.toml.

---

## Step 8: Register WDL Workflows in HealthOmics

Source: `wdl/` folder in this repo — GATK-SV v1.1.1 HealthOmics-compliant WDLs.

Each pipeline stage is a separate HealthOmics workflow. Register them and collect the workflow IDs.

```bash
python3 scripts/setup/07_register_workflows.py \
  --wdl-dir wdl \
  --region ap-southeast-1 \
  --profile <profile>
```

This zips and registers each main workflow, then writes the workflow IDs to `workflow_ids.json`.

---

## Step 9: Update Config Files

Three config files under `sam_template/gatksv_healthomics/shared/python/config/` need account-specific values:

- **docker_images.json** — ECR URIs from step 5
- **genome_references.json** — S3 paths from step 4
- **workflow_ids.json** — workflow IDs from step 8 (auto-written by step 8)

```bash
python3 scripts/setup/08_update_configs.py \
  --account-id <aws-account-id> \
  --region ap-southeast-1 \
  --ref-bucket <bucket> \
  --ref-prefix genome/gatk-sv
```

This rewrites the ECR account ID in `docker_images.json` and the S3 bucket/prefix in `genome_references.json`.

**Also**: `config_grids/templates/FilterGenotypes.json` has a hardcoded `gatk_docker` override in `optional_params` that must be updated to your account's ECR URI (the script handles this too).

---

## Step 10: Config Lambda Layer

The config files are bundled into a Lambda layer as a fallback (runtime loads from S3 first, layer second).

```bash
./scripts/setup/09_build_config_layer.sh <bucket> <profile> <region>
```

Uploads the layer zip to S3 → set `ConfigLayerS3Bucket` and `ConfigLayerS3Key` in samconfig.toml.

---

## Step 11: Sync Config to S3

Uploads config files to S3 for runtime editing (changes apply without redeploy):

```bash
./scripts/sync_config_to_s3.sh <bucket> <ByobConfig-prefix> <profile>
```

The `ByobConfig` prefix must match what you set in samconfig.toml (`CONFIG_PREFIX` env var).

---

## Step 12: Configure samconfig.toml

Create a deploy profile in `sam_template/samconfig.toml` with all the account-specific parameters gathered from steps 2-10:

```toml
[myaccount-dev.deploy.parameters]
stack_name = "gatksv-dev"
resolve_s3 = true
s3_prefix = "gatksv-dev"
profile = "<profile>"
confirm_changeset = true
capabilities = "CAPABILITY_AUTO_EXPAND CAPABILITY_IAM CAPABILITY_NAMED_IAM"
parameter_overrides = "..."   # see template below

[myaccount-dev.global.parameters]
region = "ap-southeast-1"
```

Key parameters to set:
- `DynamoDBName`, `DynamoDBArn`, `DynamoDBStreamArn` (step 3)
- `AggregatesDynamoDBName`, `AggregatesDynamoDBArn`, `AggregatesDynamoDBStreamArn` (step 3)
- `CramBucketName`, `Byob`, `OutputBucketName`, `ByobOutput`, `ByobParameter`, `ByobConfig`
- `HealthOmicsExecutionRole` (step 7)
- `AwsCliLayerArn` (step 6)
- `ConfigLayerS3Bucket`, `ConfigLayerS3Key` (step 10)
- `RefS3Prefix` (step 4)
- `HealthOmicsJsonSizeLimit` (default 45000)
- `KmsKeyArn` (if using SSE-KMS on the bucket, else empty)
- `UserArn` (IAM user/role allowed to write to the SQS queue)
- `ComputeBackend="healthomics"`

The full parameter set (25 params) is documented in `REPO_CLEANUP.md`. `samconfig.toml` ships as a TOML-array template with placeholders — fill each value from the scripts' output.

---

## Step 13: Deploy

```bash
cd sam_template
sam build
sam deploy --config-env myaccount-dev
```

**AllowedValues note**: The template's `DynamoDBName`, `DynamoDBArn`, `AggregatesDynamoDBName`, `AggregatesDynamoDBArn`, `CramBucketName`, and Byob parameters may carry `AllowedValues` lists tied to the original accounts. For a new account, run `08_update_configs.py --patch-template` (step 9) to strip these constraints automatically, or add your values to each list manually.

---

## Verification

After deploy:
```bash
# Confirm Lambdas created
aws lambda list-functions --profile <profile> --region <region> \
  --query "Functions[?starts_with(FunctionName, '<owner>-<vendor>-<env>-gatksv')].FunctionName"

# Confirm AWS CLI layer attached to a submit function
aws lambda get-function-configuration \
  --function-name <owner>-<vendor>-<env>-gatksv-submit-batch-workflow \
  --query 'Layers[].Arn' --profile <profile> --region <region>

# Test config load — invoke submit lambda with a test DDB event, check CloudWatch logs
```

Submit a test sample via `scripts/submit_samples_sqs.py` (see `scripts/README.md`).

---

## Hardcoded Values Checklist (for a totally new account)

Items that carry placeholder account IDs (`<ACCOUNT>`), regions (`<REGION>`), or bucket names (`<your-bucket>`) that MUST be filled in for your account:

| File | What to change |
|------|----------------|
| `samconfig.toml` | All ARNs, bucket names, role ARNs, layer ARNs |
| `config/docker_images.json` | ECR account ID (all URIs) |
| `config/genome_references.json` | S3 bucket + prefix |
| `config/workflow_ids.json` | All workflow IDs |
| `config_grids/templates/FilterGenotypes.json` | `gatk_docker` override in optional_params |
| `template.yaml` | `AllowedValues` lists for DDB/bucket params; parameter defaults |

The setup scripts handle most of these automatically.
