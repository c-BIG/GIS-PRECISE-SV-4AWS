# GATK-SV on AWS HealthOmics

Serverless orchestration of the GATK-SV structural variant pipeline (v1.1.1) on AWS HealthOmics. Event-driven: DynamoDB Streams → EventBridge → Lambda → HealthOmics.

For first-time deployment to a new account, see **[../SETUP_GUIDE.md](../SETUP_GUIDE.md)**.

---

## Architecture

```
Main Stack (template.yaml)
├── Messaging: SNS topics, SQS queues (+ DLQs), EventBridge Bus
├── DynamoDB integration: EventBridge Pipes (sample + aggregates streams → bus)
├── LambdaUpdateDDB / LambdaUpdateAggregates: SQS → DynamoDB writers
└── Nested stack:
    └── gatksv_healthomics/  (HealthOmics compute backend)
```

The two DynamoDB tables (sample + batch/cohort aggregates) are **created outside** the stack and passed in as ARN parameters (see SETUP_GUIDE step 3).

### HealthOmics Lambdas (`gatksv_healthomics/functions/`)

| Lambda | Role |
|--------|------|
| `submit_workflow` | Submits GatherSampleEvidence (per sample) |
| `batch_readiness_checker` | Triggers EvidenceQC when all samples in a batch complete GSE |
| `submit_batch_workflow` | Submits batch stages (EvidenceQC → FilterBatch) |
| `submit_cohort_workflow` | Submits cohort stages (MergeBatchSites → AnnotateVcf) |
| `healthomics_status_monitor` | Tracks run status, advances the pipeline |
| `stage_advancer` | Manual stage triggering via DynamoDB update |

Submit Lambdas call `/opt/bin/aws omics start-run` via subprocess (AWS CLI layer) to avoid float→scientific-notation JSON mangling.

---

## Pipeline Stages

Sample → Batch → Cohort, auto-advancing via DynamoDB events:

```
GatherSampleEvidence (sample)
  → EvidenceQC → TrainGCNV → GatherBatchEvidence → ClusterBatch
  → GenerateBatchMetrics → FilterBatchSites → [QC gate] → FilterBatchSamples
  → MergeBatchSites → GenotypeBatch → RegenotypeCNVs → MakeCohortVcf
  → RefineComplexVariants → JoinRawCalls → SVConcordance → FilterGenotypes → AnnotateVcf
```

`FilterBatchSites` is a manual QC gate — review SV count plots, then trigger `FilterBatchSamples` with the chosen nIQR cutoff.

---

## DynamoDB Model

Two tables, both keyed `pk` (String) + `sk` (String), both with NEW_IMAGE streams.

- **Sample table**: `pk=SAMPLE#<id>`, `sk=METADATA` (registry) or `sk=<Stage>#<timestamp>` (event records that trigger Lambdas). Also `pk=GLACIER_QUEUE` entries for restore queuing (if used).
- **Aggregates table**: `pk=BATCH#<id>` or `COHORT#<id>`, `sk=Event=<Stage>`. Tracks per-batch/cohort completion counts.

---

## Deployment

```bash
cd sam_template
sam build
sam deploy --config-env gatksv-dev-healthomics
```

Deploy parameters are in `samconfig.toml` (TOML array format, one `Key=Value` per line). See SETUP_GUIDE for the full list and how to obtain each value.

---

## Configuration

Runtime config loads from **S3 first, Lambda layer fallback**. Edit on S3 to change without redeploying:

```bash
# Templates / dockers / references (takes effect on next Lambda cold start)
aws s3 cp s3://<bucket>/<ByobConfig>/templates/TrainGCNV.json /tmp/x.json
# edit, then upload back
aws s3 cp /tmp/x.json s3://<bucket>/<ByobConfig>/templates/TrainGCNV.json

# Or sync the whole config dir:
./scripts/sync_config_to_s3.sh <bucket> <ByobConfig> <profile>
```

Config files (`gatksv_healthomics/shared/python/config/`):
- `workflow_ids.json` — HealthOmics workflow IDs per stage
- `genome_references.json` — reference file S3 paths
- `docker_images.json` — ECR image URIs
- `templates/*.json` — per-stage parameter templates

---

## Scripts

See **[scripts/README.md](scripts/README.md)** for full usage. Key ones:

| Script | Purpose |
|--------|---------|
| `submit_samples_sqs.py` | Register + submit samples (hot / glacier / pending / redrive modes) |
| `scan_ddb_status.py` | Lifecycle status summary across all samples |
| `bulk_trigger_filterbatch.py` | Bulk-start FilterBatchSamples after QC review |
| `redrive_failed_gse.py` | Resubmit failed GatherSampleEvidence runs |
| `setup/` | New-account setup automation (see SETUP_GUIDE) |

---

## Monitoring

```bash
# Lifecycle summary
python3 scripts/scan_ddb_status.py --table <sample-table> --profile <profile>

# Lambda logs
aws logs tail /aws/lambda/<owner>-<vendor>-<env>-gatksv-submit-sample-workflow --follow

# HealthOmics runs — check the console or:
aws omics list-runs --region <region> --profile <profile>
```

---

## Notes

- Based on **GATK-SV v1.1.1** — uses the stock Broad docker images (no custom rebuilds).
- Submit Lambdas require the **AWS CLI Lambda layer** (`AwsCliLayerArn`) — see `lambda_layer/README.md`.
- HealthOmics has a ~50KB workflow-parameter limit; the submit Lambda writes S3 manifests for large inputs (gated by `HealthOmicsJsonSizeLimit`).
