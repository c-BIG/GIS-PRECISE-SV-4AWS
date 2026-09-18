# gatksv_healthomics — HealthOmics Compute Backend

Nested SAM stack that submits GATK-SV workflow stages to AWS HealthOmics, driven by DynamoDB events routed from the parent stack.

Parent stack + deployment: see [../README.md](../README.md) and [../../SETUP_GUIDE.md](../../SETUP_GUIDE.md).

---

## Structure

```
gatksv_healthomics/
├── template.yaml                      # Nested stack (Lambdas, IAM, config layer)
├── shared/python/
│   ├── config_loader.py               # Loads config: S3-first, layer fallback
│   └── config/
│       ├── workflow_ids.json          # HealthOmics workflow ID per stage
│       ├── genome_references.json     # Reference file S3 paths
│       ├── docker_images.json         # ECR image URIs
│       └── templates/*.json           # Per-stage parameter templates
└── functions/
    ├── submit_workflow/               # GatherSampleEvidence (sample-level)
    ├── batch_readiness_checker/       # Triggers EvidenceQC when batch complete
    ├── submit_batch_workflow/         # Batch stages (EvidenceQC → FilterBatch)
    ├── submit_cohort_workflow/        # Cohort stages (MergeBatchSites → AnnotateVcf)
    ├── healthomics_status_monitor/    # Tracks run status, advances pipeline
    └── stage_advancer/                # Manual stage triggering
```

---

## Event Flow

```
1. DDB record inserted: pk=SAMPLE#<id>, sk=GatherSampleEvidence#<ts>, Status=PENDING
2. DDB Stream → EventBridge Pipe → EventBridge Bus (parent stack)
3. EventBridge rule matches pk prefix + Event + Status → triggers a submit Lambda
4. Lambda:
   - loads template (S3 first, layer fallback)
   - resolves dynamic params by querying DDB for prior-stage outputs
   - builds the parameter JSON (or S3 manifests if > HealthOmicsJsonSizeLimit)
   - submits via `aws omics start-run` (AWS CLI layer subprocess)
5. healthomics_status_monitor records completion, advances to the next stage
```

---

## Configuration

`config_loader.py` loads each config **from S3 first** (`s3://<OutputBucket>/<ByobConfig>/...`), falling back to the bundled Lambda layer. This lets you edit templates/dockers/references on S3 without redeploying (takes effect on the next Lambda cold start).

| File | Contents |
|------|----------|
| `workflow_ids.json` | `{ "<Stage>": { "id": "<workflow-id>", "version": null } }` |
| `genome_references.json` | Reference file → S3 path map |
| `docker_images.json` | Docker key → ECR URI (GATK-SV v1.1 images) |
| `templates/<Stage>.json` | `static_params` / `docker_params` / `dynamic_params` / `optional_params` |

Template dynamic-param query syntax (resolved at runtime from DDB), e.g.:
```
{{cohort_id}}, {{ped_file}}, {{query:ddb:samples_in_batch}}
{{query:ddb:output_paths:<Stage>:<folder>:<suffix>[:glob[:single]]}}
{{query:ddb:batch_outputs:<Stage>:<folder>:<suffix>:glob}}
```

---

## IAM

- **Lambda role** (`GatkSvLambdaRole`): `omics:StartRun/GetRun/ListRuns`, `iam:PassRole` (HealthOmics exec role), DynamoDB read/write, SQS/SNS, S3 read for config + manifest listing, conditional KMS.
- **HealthOmics execution role**: reads CRAM/inputs, reads/writes outputs, pulls ECR, writes logs. Provided externally via `HealthOmicsExecutionRole`, or self-created when that parameter is empty (`CreateRoles` condition).

Submit Lambdas need the **AWS CLI Lambda layer** (`AwsCliLayerArn`) — they shell out to `/opt/bin/aws omics start-run` with `file://params.json` to avoid Python's float→scientific-notation JSON mangling that HealthOmics rejects.

---

## Notes

- **Cache**: submit Lambdas use `CACHE_ALWAYS` with `HealthOmicsCacheId`. Change the cache ID to force fresh execution after WDL fixes.
- **Parameter size**: HealthOmics has a ~50KB run-parameter limit. When the built JSON exceeds `HealthOmicsJsonSizeLimit`, the Lambda writes file manifests to S3 instead of inline arrays.
- **FilterBatch split**: `FilterBatchSites` (auto) and `FilterBatchSamples` (manual QC gate) are separate stages/workflows.
- **GATK-SV v1.1**: uses stock Broad docker images.
