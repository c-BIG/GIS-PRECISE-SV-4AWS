# Setup Scripts — New AWS Account

Automation for deploying GATK-SV HealthOmics to a fresh account. See `../../../SETUP_GUIDE.md` for the full walkthrough.

## Quick Start

1. Install prerequisites: AWS CLI v2, SAM CLI, Docker, Python 3.12 + boto3
2. Configure AWS credentials: `aws configure --profile <profile>`
3. Edit the CONFIG block in `setup_all.sh`
4. Run: `./setup_all.sh` (prompts before each step)
5. Fill in `samconfig.toml` with the ARNs printed by the scripts
6. `cd ../.. && sam build && sam deploy --config-env <env>`

## Scripts (run in order, or via setup_all.sh)

| Script | Purpose | Speed |
|--------|---------|-------|
| `01_create_buckets.sh` | Create S3 bucket | fast |
| `02_create_dynamodb.sh` | Create sample + aggregates tables with streams | fast |
| `03_setup_references.sh` | Download refs from Broad GCS → your S3 | slow (~10 GB) |
| `04_mirror_dockers.py` | Mirror Broad v1.1 dockers → your ECR, `--write-config` | very slow |
| `05_build_awscli_layer.sh` | Publish AWS CLI Lambda layer | fast |
| `06_create_ho_role.sh` | Create HealthOmics execution IAM role | fast |
| `07_register_workflows.py` | Register WDL workflows → workflow_ids.json | slow |
| `08_update_configs.py` | Rewrite config files for new account | fast |
| `09_build_config_layer.sh` | Build + upload config layer | fast |
| `setup_all.sh` | Orchestrates 2-11 with prompts | — |

Then `../sync_config_to_s3.sh` to push config to S3.

## Notes

- **DynamoDB tables are NOT created by the SAM template** — step 2 is mandatory.
- `04_mirror_dockers.py` fetches Broad's image list pinned to the **v1.1** tag. Pass `--source-config <path>` to mirror from a local dockers.json instead. (Note: this repo intentionally stays on v1.1, NOT v1.1.1.)
- `08_update_configs.py --patch-template` strips `AllowedValues` constraints from account-specific parameters in `template.yaml` (backs up the original).
- `manifest_reader_docker` (amazonlinux + aws cli) is AWS-specific, not in Broad's list — build from `../../dockerfile/dockerfile.awscli`.
- Re-running any script is safe (idempotent — skips existing resources).
- `docker_images.v1.1.template.json` is a reference of the expected config output (replace `<ACCOUNT>`/`<REGION>`).

## Per-account values to collect for samconfig.toml

The scripts print these as they run:
- DynamoDB names/ARNs/stream ARNs (step 2)
- `AwsCliLayerArn` (step 5)
- `HealthOmicsExecutionRole` (step 6)
- `ConfigLayerS3Bucket` / `ConfigLayerS3Key` (step 9)
- `RefS3Prefix` (step 3)
