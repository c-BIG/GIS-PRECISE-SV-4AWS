# GIS-PRECISE-SV-4AWS

A port of the Broad Institute's **[GATK-SV](https://broadinstitute.github.io/gatk-sv/docs/intro)** structural-variant calling pipeline to **AWS HealthOmics**.

GATK-SV is a cloud-native, WDL-based pipeline originally built to run on Terra / Google Cloud (Cromwell + GCS). This project adapts the stable **[GATK-SV v1.1](https://github.com/broadinstitute/gatk-sv/tree/v1.1)** release to run serverlessly on AWS: WDL workflows execute on **AWS HealthOmics**, orchestration is event-driven via **DynamoDB Streams → EventBridge → Lambda**, and all I/O is on **S3**. Sample QC and batching are done in Jupyter notebooks ported from the Broad originals (Terra data tables → DynamoDB, GCS → S3).

**Upstream source:** https://github.com/broadinstitute/gatk-sv/tree/v1.1
This repo tracks the pinned v1.1 release. The WDL workflows, reference files, and Docker images are all sourced from that tag: WDLs are copied into `wdl/`, references are mirrored from Broad's public buckets to your S3, and Broad's pre-built Docker images are mirrored as-is to your ECR (no images are built from source, aside from a small AWS CLI helper image used by the HealthOmics manifest-reader task).

---

## Two Ways to Use This Repo

**A. Just the HealthOmics-compliant WDLs** — if you already have (or want to build) your own way of running workflows on AWS HealthOmics, you only need the `wdl/` folder. These are the GATK-SV v1.1 WDLs adapted to run on the HealthOmics WDL engine. Register them as HealthOmics workflows and drive them with whatever orchestration you prefer (your own scripts, Step Functions, the HealthOmics console, etc.). You still need the reference files on S3 and the Docker images in ECR — see [Step 4](SETUP_GUIDE.md) and [Step 5](SETUP_GUIDE.md) of the setup guide — but you can ignore the entire `sam_template/` stack.

See [HealthOmics WDL adaptations](#healthomics-wdl-adaptations) below for what was changed vs. the Broad originals.

**B. The full turnkey stack** — the `sam_template/` SAM application provides complete serverless orchestration: event-driven stage progression, sample/batch tracking in DynamoDB, automatic handoff between stages, and QC/batching notebooks. This is what the rest of this README and the [SETUP_GUIDE](SETUP_GUIDE.md) describe.

---

## What's Here

```
GIS-PRECISE-SV-4AWS/
├── README.md                      # this file
├── SETUP_GUIDE.md                 # detailed new-account setup walkthrough
├── docs/
│   ├── HEALTHOMICS_WDL_ADAPTATIONS.md   # what changed in the WDLs for HealthOmics
│   └── GATKSV_UPGRADE_CHECKLIST.md      # re-port checklist for the next GATK-SV release
├── wdl/                           # GATK-SV v1.1 HealthOmics-compliant WDLs
└── sam_template/
    ├── template.yaml              # parent SAM stack (messaging + HealthOmics nested stack)
    ├── samconfig.toml             # deploy config (per-environment profiles)
    ├── gatksv_healthomics/        # HealthOmics compute backend (Lambdas, IAM, config)
    ├── scripts/                   # operational scripts + scripts/setup/ (new-account automation)
    ├── dockerfile/                # dockerfile.awscli (manifest reader for HealthOmics)
    ├── lambda_layer/              # AWS CLI Lambda layer notes
    └── QC_notebook_aws_compliant/ # SampleQC + Batching notebooks (AWS-ported)
```

---

## Architecture

```
Parent Stack (sam_template/template.yaml)
├── Messaging: SNS topics, SQS queues (+ DLQs), EventBridge Bus
├── DynamoDB integration: EventBridge Pipes (sample + aggregates streams → bus)
├── LambdaUpdateDDB / LambdaUpdateAggregates: SQS → DynamoDB writers
└── Nested stack:
    └── gatksv_healthomics/  (HealthOmics compute backend)
```

Event-driven flow: a DynamoDB record insert (`SAMPLE#<id>` / `<Stage>#PENDING`) → DDB Stream → EventBridge Pipe → EventBridge Bus → submit Lambda → HealthOmics `start-run`. On completion, a status-monitor Lambda records the result and advances to the next stage.

The two DynamoDB tables (sample + batch/cohort aggregates) are **created outside the stack** and passed in as ARN parameters.

### Pipeline stages (auto-advancing)

```
GatherSampleEvidence (sample)
  → EvidenceQC → TrainGCNV → GatherBatchEvidence → ClusterBatch
  → GenerateBatchMetrics → FilterBatchSites → [QC gate] → FilterBatchSamples
  → MergeBatchSites → GenotypeBatch → RegenotypeCNVs → MakeCohortVcf
  → RefineComplexVariants → JoinRawCalls → SVConcordance → FilterGenotypes → AnnotateVcf
```

`FilterBatchSites` is a manual QC gate — review SV count plots (SampleQC/Batching notebooks), then trigger `FilterBatchSamples` with the chosen nIQR cutoff.

---

## New Account Setup

Deploying to a fresh AWS account is a one-time sequence of steps. Automation scripts live in `sam_template/scripts/setup/`. See **[SETUP_GUIDE.md](SETUP_GUIDE.md)** for full detail; the summary:

| # | Step | Script | One-time? |
|---|------|--------|-----------|
| 1 | Prerequisites (AWS CLI, SAM CLI, Docker, Python + boto3) | manual | yes |
| 2 | Create S3 bucket | `01_create_buckets.sh` | yes |
| 3 | Create DynamoDB tables (+ streams) | `02_create_dynamodb.sh` | yes |
| 4 | Download reference genome → S3 | `03_setup_references.sh` | yes |
| 5 | Mirror Broad v1.1 Docker images → ECR | `04_mirror_dockers.py` | yes |
| 6 | Build & publish AWS CLI Lambda layer | `05_build_awscli_layer.sh` | yes |
| 7 | Create HealthOmics execution IAM role | `06_create_ho_role.sh` | yes |
| 8 | Register WDL workflows in HealthOmics | `07_register_workflows.py` | yes |
| 9 | Update config files (dockers, refs, workflow IDs) | `08_update_configs.py` | yes |
| 10 | Build & upload config Lambda layer | `09_build_config_layer.sh` | per config change |
| 11 | Sync config to S3 | `sync_config_to_s3.sh` | per config change |
| 12 | Configure `samconfig.toml` | manual | per environment |
| 13 | Deploy: `sam build && sam deploy` | — | per change |

`scripts/setup/setup_all.sh` runs steps 2–11 in sequence with confirmation prompts.

### Prerequisites

```bash
# AWS CLI v2
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip awscliv2.zip && sudo ./aws/install

# AWS SAM CLI — https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html
# Docker — https://docs.docker.com/engine/install/
pip install boto3

aws configure --profile <your-profile>
aws sts get-caller-identity --profile <your-profile>
```

### Quick start (automated)

```bash
cd sam_template/scripts/setup
# edit the CONFIG block at the top of setup_all.sh
./setup_all.sh          # prompts before each step
# then fill in samconfig.toml with the ARNs the scripts print
cd ../.. && sam build && sam deploy --config-env <env>
```

### Key setup notes

- **DynamoDB tables are NOT created by the SAM template** — step 3 is mandatory. Two tables, each keyed `pk`(S) + `sk`(S) with a NEW_IMAGE stream.
- **Docker images** are mirrored from Broad's registry pinned to the **v1.1** tag (`https://raw.githubusercontent.com/broadinstitute/gatk-sv/v1.1/inputs/values/dockers.json`). Do not use `main` — it moves.
- **Reference files** come from Broad's public GCS buckets (`resources_hg38.json`) and are mirrored to your S3.
- **AWS CLI Lambda layer** is required — the submit Lambdas shell out to `/opt/bin/aws omics start-run` (via `file://params.json`) to avoid Python's float → scientific-notation JSON mangling that HealthOmics rejects.
- **`manifest_reader_docker`** (amazonlinux + aws cli) is the one AWS-specific image not in Broad's list — build from `sam_template/dockerfile/dockerfile.awscli`.
- `08_update_configs.py --patch-template` rewrites config for the new account and strips account-specific `AllowedValues` constraints from `template.yaml`.

---

## Configuration

Runtime config loads **from S3 first, Lambda layer fallback** — so you can edit templates/dockers/references on S3 without redeploying (applies on next Lambda cold start).

Config files (`sam_template/gatksv_healthomics/shared/python/config/`):
- `workflow_ids.json` — HealthOmics workflow ID per stage
- `genome_references.json` — reference file S3 paths
- `docker_images.json` — ECR image URIs
- `templates/*.json` — per-stage parameter templates

```bash
# edit on S3 (no redeploy):
aws s3 cp s3://<bucket>/<ByobConfig>/templates/TrainGCNV.json /tmp/x.json
# edit, then:
aws s3 cp /tmp/x.json s3://<bucket>/<ByobConfig>/templates/TrainGCNV.json

# or sync the whole config dir:
./sam_template/scripts/sync_config_to_s3.sh <bucket> <ByobConfig> <profile>
```

---

## Running Samples

See `sam_template/scripts/README.md`. Main entry points:

| Script | Purpose |
|--------|---------|
| `submit_samples_sqs.py` | Register + submit samples (hot / glacier / pending / redrive modes) |
| `scan_ddb_status.py` | Full-lifecycle status summary across all samples |
| `bulk_trigger_filterbatch.py` | Bulk-start FilterBatchSamples after QC review |
| `redrive_failed_gse.py` | Resubmit failed GatherSampleEvidence runs |

QC and batching are done in `QC_notebook_aws_compliant/` (SampleQC + Batching notebooks). Set `AWS_PROFILE` at the top of each notebook.

---

## HealthOmics WDL Adaptations

The WDLs in `wdl/` are the Broad GATK-SV v1.1 workflows adapted to run on the AWS
HealthOmics WDL engine (which differs from Cromwell/Terra in a few ways). The main changes:

- **Explicit index inputs** — HealthOmics localizes each `File` separately, so VCF/`.tbi`
  pairs are passed as explicit inputs (and symlinked where tools need them co-located).
- **`select_first()` on optionals** — replaced with `if defined(...)` to avoid `EvalError`
  when all values are null.
- **Strict-mode fixes** — optional interpolation, non-string `default=`, no `String→File`
  coercion.
- **GCS removed** — GCS OAuth token export and `gsutil`/`gcloud` usage replaced with
  AWS-native equivalents (or a download task using an AWS-CLI image).
- **~50KB parameter limit** — large sample arrays passed as an S3 manifest that a
  `ReadManifest` task downloads at runtime.
- Misc: `gz` gunzip symlink fix, `write_tsv()` output path, float serialization at submit.

Full detail (with the specific files touched): **[docs/HEALTHOMICS_WDL_ADAPTATIONS.md](docs/HEALTHOMICS_WDL_ADAPTATIONS.md)**. That doc also has a minimal checklist for running the WDLs with your own orchestration.

**Upgrading to a newer GATK-SV release?** See **[docs/GATKSV_UPGRADE_CHECKLIST.md](docs/GATKSV_UPGRADE_CHECKLIST.md)** — a category-by-category re-port checklist with scan commands for each fix.

---

## Notes

- Based on **GATK-SV v1.1** — uses the stock Broad Docker images (no custom rebuilds).
- Compute backend is HealthOmics only.
- HealthOmics has a ~50KB run-parameter limit; the submit Lambda writes S3 manifests for large inputs (gated by `HealthOmicsJsonSizeLimit`).

---

## Attribution

Built on the Broad Institute's GATK-SV pipeline (v1.1). See the upstream repository for the pipeline's scientific documentation, WDL sources, and licensing:
https://github.com/broadinstitute/gatk-sv/tree/v1.1

The WDL workflows in `wdl/` are derived from GATK-SV and remain Copyright (c) 2009-2026,
Broad Institute, Inc. — see `wdl/README.md`.

---

## Citation

If you use this software, please cite the gnomAD-SV paper that describes the underlying
GATK-SV method (as requested by the GATK-SV team):

> Collins, R.L., Brand, H., Karczewski, K.J. et al. A structural variation reference for
> medical and population genetics. *Nature* 581, 444–451 (2020).
> https://doi.org/10.1038/s41586-020-2287-8

See [CITATION.md](CITATION.md) for details.

---

## License

This project is licensed under the **BSD 3-Clause License** (see [LICENSE.TXT](LICENSE.TXT)),
the same license used by upstream GATK-SV. The WDL workflows in `wdl/` are derived from
GATK-SV and retain the Broad Institute copyright; all other code (SAM stack, Lambdas,
orchestration scripts, notebooks) is copyright the project authors under the same license.
