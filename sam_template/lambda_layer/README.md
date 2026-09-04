# Lambda Layers

This directory contains build instructions for Lambda layers used by the GATK-SV HealthOmics stack.

## Layers Overview

| Layer | Purpose | Required By | Size |
|-------|---------|-------------|------|
| **awscli** | AWS CLI v1 at `/opt/bin/aws` | All submit functions (sample, batch, cohort) | ~50 MB |
| **gatksv-config** | Shared config (genome refs, docker images, templates) | All Lambda functions | ~30 KB |

---

## AWS CLI Layer

### Why It's Needed

The HealthOmics `start-run` API requires parameter JSON passed via `file://` to avoid Python's `json.dumps()` converting floats like `0.000001` to scientific notation (`1e-06`), which HealthOmics WDL rejects. The submit Lambdas call `/opt/bin/aws omics start-run` via subprocess.

### Build & Publish

Run from any machine with Docker (or directly on Amazon Linux / CloudShell):

```bash
# 1. Build the layer
mkdir -p /tmp/awscli-layer/bin
pip install awscli -t /tmp/awscli-layer/python --no-cache-dir

cat > /tmp/awscli-layer/bin/aws << 'EOF'
#!/bin/bash
export PYTHONPATH="/opt/python:${PYTHONPATH}"
exec python3 -c "import sys; from awscli.clidriver import main; sys.exit(main())" "$@"
EOF
chmod +x /tmp/awscli-layer/bin/aws

# 2. Package
cd /tmp/awscli-layer
zip -r /tmp/awscli-layer.zip bin/ python/

# 3. Publish to your account
aws lambda publish-layer-version \
  --layer-name awscli \
  --description "AWS CLI v1 for HealthOmics start-run subprocess calls" \
  --zip-file fileb:///tmp/awscli-layer.zip \
  --compatible-runtimes python3.12 \
  --region ap-southeast-1
```

Note the ARN from the output, e.g.:
```
arn:aws:lambda:ap-southeast-1:123456789012:layer:awscli:1
```

### Deploy with the Stack

Add to `samconfig.toml` parameter overrides:

```toml
[gatksv-dev-healthomics.deploy.parameters]
parameter_overrides = [
    "AwsCliLayerArn=arn:aws:lambda:ap-southeast-1:123456789012:layer:awscli:1",
    # ... other params
]
```

Or pass directly:
```bash
sam deploy --config-env gatksv-dev-healthomics \
  --parameter-overrides AwsCliLayerArn=arn:aws:lambda:ap-southeast-1:123456789012:layer:awscli:1
```

If `AwsCliLayerArn` is left empty (default), the layer is not attached and the submit functions will fail with `FileNotFoundError: [Errno 2] No such file or directory: '/opt/bin/aws'`.

### Verification

After deploying, test from the Lambda console:

```bash
# In the Lambda test event, or via CLI:
aws lambda invoke --function-name <function_name> \
  --payload '{}' /dev/null

# Or just verify the layer is attached:
aws lambda get-function-configuration \
  --function-name <function_name> \
  --query 'Layers[].Arn'
```

### Updating the Layer

AWS CLI v1 rarely needs updating. If needed:

```bash
# Rebuild and publish a new version
aws lambda publish-layer-version \
  --layer-name awscli \
  --zip-file fileb:///tmp/awscli-layer.zip \
  --compatible-runtimes python3.12 \
  --region ap-southeast-1

# Update samconfig.toml with new version number (e.g., :2)
# Redeploy the stack
```

---

## Config Layer (gatksv-config)

### What It Contains

```
python/
├── config_loader.py           # S3 config loading helper
├── healthomics_outputs.py     # Output path resolution
└── config/
    ├── genome_references.json # Reference file S3 paths
    ├── docker_images.json     # ECR image URIs
    ├── workflow_ids.json       # HealthOmics workflow IDs
    └── templates/             # Per-stage parameter templates
```

### Build & Upload

Use the setup script (recommended):

```bash
./scripts/setup/09_build_config_layer.sh <bucket> <profile> <region>
# → uploads to s3://<bucket>/lambda_layer/gatksv_config_layer.zip
```

Or manually:

```bash
cd sam_template/gatksv_healthomics/shared
zip -r gatksv_config_layer.zip python/
aws s3 cp gatksv_config_layer.zip s3://<your-bucket>/lambda_layer/gatksv_config_layer.zip
```

Then set in samconfig.toml:
```toml
"ConfigLayerS3Bucket=<your-bucket>",
"ConfigLayerS3Key=lambda_layer/gatksv_config_layer.zip",
```

### Updating Templates Without Redeploying

The config layer is a fallback. At runtime, Lambdas load configs from S3 first (via `CONFIG_PREFIX` env var). To change parameters without redeploying:

```bash
# Edit template on S3 directly
aws s3 cp s3://<bucket>/<config-prefix>/templates/EvidenceQC.json /tmp/
# edit /tmp/EvidenceQC.json
aws s3 cp /tmp/EvidenceQC.json s3://<bucket>/<config-prefix>/templates/

# No redeploy needed — Lambda picks up S3 version on next invocation
```

---

## Multi-Account Deployment Checklist

When deploying to a new AWS account:

1. **AWS CLI Layer**: Build and publish in the target account (layer ARNs are account-specific)
2. **Config Layer**: Upload the zip to the target account's S3 bucket, update `ConfigLayerS3Bucket` and `ConfigLayerS3Key` params
3. **samconfig.toml**: Set `AwsCliLayerArn` to the new account's layer ARN
4. **Deploy**: `sam build && sam deploy --config-env <env>`

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `FileNotFoundError: '/opt/bin/aws'` | AWS CLI layer not attached | Set `AwsCliLayerArn` parameter and redeploy |
| `Import error: config_loader` | Config layer not attached or corrupt | Re-upload config layer zip to S3, redeploy |
| `Layer version does not exist` | Wrong ARN or deleted layer | Verify ARN with `aws lambda list-layer-versions --layer-name awscli` |
| Layer size exceeds limit | Unzipped layers > 250 MB combined | Remove unused packages from awscli layer (`pip install awscli --no-deps` + only required deps) |
