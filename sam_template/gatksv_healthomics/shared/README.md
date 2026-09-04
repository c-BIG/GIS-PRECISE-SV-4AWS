# Config Lambda Layer

This directory (`python/`) is packaged into the **config Lambda layer** — the runtime
fallback for the pipeline config (genome references, docker images, workflow IDs, per-stage
templates). At runtime the Lambdas load config from S3 first and fall back to this layer.

Build and upload with the setup script:

```bash
./scripts/setup/09_build_config_layer.sh <bucket> <profile> <region>
# → s3://<bucket>/lambda_layer/gatksv_config_layer.zip
```

Or manually:

```bash
cd sam_template/gatksv_healthomics/shared
zip -r gatksv_config_layer.zip python/
aws s3 cp gatksv_config_layer.zip s3://<your-bucket>/lambda_layer/gatksv_config_layer.zip
```

Then set in `samconfig.toml`:

```toml
"ConfigLayerS3Bucket=<your-bucket>",
"ConfigLayerS3Key=lambda_layer/gatksv_config_layer.zip",
```

See `../../lambda_layer/README.md` for both the config layer and the AWS CLI layer.
