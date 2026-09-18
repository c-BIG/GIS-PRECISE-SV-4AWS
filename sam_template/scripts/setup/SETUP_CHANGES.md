# GATK-SV on AWS — Docker/Config Setup Changes

> Context document for future Kiro iterations and for porting this setup into the
> private production repo. Describes **what was changed, why, and how the pieces
> fit together** for the docker-mirroring and config-generation part of the
> GATK-SV HealthOmics deployment.
>
> Last updated: 2026-09-18. Target: GATK-SV **v1.1** (rolled back from v1.1.1 —
> v1.1.1 = v1.1 + a BND/END representation change, Broad PR #835; this deployment
> stays on v1.1).

---

## 1. Design decision: Model A (Broad is the single source of truth)

The docker image set is **derived from Broad's upstream `dockers.json`**, pinned to
a release tag. Nothing is hand-maintained image-by-image, which avoids drift.

```
Broad dockers.json (pinned v1.1, fetched from GitHub)
        │  04_mirror_dockers.py  (mirror to ECR + --write-config)
        ▼
  ┌─────────────────────────────┐     ┌──────────────────────────────────────┐
  │ docker_images.json (FILLED) │     │ docker_images.v1.1.template.json       │
  │ real <account>.<region>     │     │ placeholders <ACCOUNT>/<REGION>        │
  │ = runtime config            │     │ = committed point-of-reference          │
  └─────────────────────────────┘     └──────────────────────────────────────┘
        ▲                                        │
        │  08_update_configs.py (re-point account/region without re-mirroring)
        └────────────────────────────────────────┘
```

- **`04 --write-config` regenerates BOTH** the filled config and the placeholder
  template in one step, so the template can never drift from what was mirrored.
- **`08_update_configs.py`** is for re-pointing an existing template to a new
  account/region/bucket **without** re-running the (slow) mirror. It seeds the
  runtime config from the templates, then substitutes placeholders.
- **Genome references** have no Broad equivalent (they are your own reference data
  in S3), so `genome_references.template.json` is hand-maintained and is itself
  the source of truth for reference paths.

---

## 2. Which docker images are actually needed

Verified against Broad's WDL input templates (`inputs/templates/**/*.tmpl`,
`{{ dockers.<key> }}` references) **and** the WDL tasks (`wdl/*.wdl`).

- **19 keys are referenced** by GATK-SV WDL and are mirrored:
  `cloud_sdk_docker, cnmops_docker, condense_counts_docker, gatk_docker,
  gatk_docker_pesr_override, genomes_in_the_cloud_docker, gq_recalibrator_docker,
  linux_docker, manta_docker, melt_docker, samtools_cloud_docker, scramble_docker,
  sv_base_docker, sv_base_mini_docker, sv_pipeline_docker, sv_pipeline_qc_docker,
  sv_utils_docker, vapor_docker, wham_docker`
- **+1 AWS-specific**: `manifest_reader_docker` (amazonlinux + aws cli, for the
  HealthOmics ReadManifest task; built separately from `dockerfile/dockerfile.awscli`).
- **Total = 20 entries** in `docker_images.json` / template.

### Dropped keys (defined by Broad but NOT referenced anywhere)
Verified zero references in `.tmpl` templates and `.wdl` files:
`cnmops-virtual-env, samtools-cloud-virtual-env, sv-base-virtual-env,
sv-pipeline-virtual-env, sv-utils-env, str, denovo, sv-shell`
→ Listed in `UNUSED_DOCKER_KEYS` in `04_mirror_dockers.py`, dropped by default.
Use `--keep-unused` to mirror the full Broad set anyway.

### Also dropped (optional overrides not in Broad's dockers.json)
- `gcnv_gatk_docker` — optional override in `GatherBatchEvidence.wdl`
  (`gatk_docker = select_first([gcnv_gatk_docker, gatk_docker])`). Falls back to
  `gatk_docker`, so not required.
- `dragen_bnd2inv_docker` — optional DRAGEN bnd2inv path in `GatherSampleEvidence.wdl`
  (`run_bnd2inv = defined(dragen_bnd2inv_docker)`). DRAGEN VCF is not a vanilla
  GATK-SV supported input, so this is dropped.

---

## 3. ECR namespacing

All mirrored images are pushed under a single **`gatk-sv/`** namespace in ECR so they
are easy to find/manage (e.g. `gatk-sv/cnmops`, `gatk-sv/gatk`, `gatk-sv/ubuntu1804`).

Implemented in `src_to_repo_tag()` in `04_mirror_dockers.py`:
- Strips the registry host (`us.gcr.io`, `marketplace.gcr.io`).
- Strips **all** leading org/owner segments (handles nested ones like
  `broad-dsde-methods/tsharpe/gatk` → `gatk`). Orgs: `broad-dsde-methods,
  broad-gotc-prod, talkowski-sv-gnomad, vjalili, markw, eph, tsharpe, google`.
- Drops an existing leading `gatk-sv` to avoid doubling, then prepends `gatk-sv/`.

`linux_docker` and `cloud_sdk_docker` point at the small ubuntu / cloud-sdk images
(mirrored to `gatk-sv/ubuntu1804`, `gatk-sv/cloud-sdk`); they are only used to run
small linux commands, so any small image suffices.

---

## 4. MELT licensing (the original problem)

`us.gcr.io/talkowski-sv-gnomad/melt:a85c92f` is **not freely pullable** (licensing).
`04_mirror_dockers.py` handles this gracefully:
- `KNOWN_RESTRICTED` maps the MELT image to an explanatory note.
- On pull failure, restricted images are reported under a **"Skipped (restricted)"**
  section (not a hard failure).
- Failures **roll over** by default and the script **exits 0** (so expected gaps
  don't fail the whole setup). Use `--strict` to make any failure fatal.
- Unmirrored images keep their **original source URI** in the written config (never
  a broken ECR path) and are flagged in the summary.
- `run()` streams docker stdout live but captures stderr, so each failure's real
  error is shown in the end report.

**To use MELT:** obtain access to the source image (or build/host it yourself),
then re-mirror with `--source-config` pointing at a dockers.json that references
your accessible MELT image.

---

## 5. `gq_recalibrator_docker` handling in FilterGenotypes (cleaner fix)

In the HO stack, `FilterGenotypes.wdl`'s `RecalibrateGq` step consumes `gatk_docker`
(not a separate `gq_recalibrator_docker` input). Previously the FilterGenotypes
config-grid template **hardcoded a full ECR URI** in `optional_params.gatk_docker`
to override the standard gatk image with the gq-recalibrator image — which leaked
the account ID/region and could drift.

**New mechanism — `{{docker:<key>}}` reference:**
- `config/templates/FilterGenotypes.json` now has:
  `"gatk_docker": "{{docker:gq_recalibrator_docker}}"`
- All three `parameter_builder.py` files
  (`submit_workflow`, `submit_batch_workflow`, `submit_cohort_workflow`) gained a
  `_resolve_docker_ref()` helper, called in the `optional_params` loop. It resolves
  `{{docker:<key>}}` → `DOCKER_IMAGES[<key>]` from `docker_images.json`, raising a
  clear error if the key is missing.
- Loop order guarantees the override wins: `docker_params` sets `gatk_docker` to the
  standard image, then `optional_params` overrides it with the resolved recalibrator
  image. Same runtime behavior as before, but the image URI now lives **only** in
  `docker_images.json` (single source of truth, mirrored to ECR, no hardcoded URI).

`gq_recalibrator_docker` is therefore kept in the docker set (so its image gets
mirrored), even though the WDL input is named `gatk_docker`.

---

## 6. Files changed / created in this work

### Changed
- **`scripts/setup/04_mirror_dockers.py`**
  - `UNUSED_DOCKER_KEYS` + drop logic in `load_source()` (`--keep-unused` to opt out).
  - `src_to_repo_tag()` flattens nested orgs and forces the `gatk-sv/` namespace.
  - `KNOWN_RESTRICTED` + roll-over failure handling + `--strict`.
  - `--write-config` now also regenerates `docker_images.v1.1.template.json`
    (placeholders) so it stays in lockstep with the mirror.
  - `ensure_repo()` applies the HealthOmics ECR repository policy
    (`omics.amazonaws.com` pull access) to each repo; new `--fix-policies` mode
    retrofits all existing `gatk-sv/*` repos. (See §10.)
- **`scripts/setup/02_create_dynamodb.sh`**
  - `create_table()` takes a range-key arg: sample table uses `sk`, aggregates table
    uses `Event` (previously both used `sk`, which broke aggregates writes). (See §9.)
- **`scripts/setup/08_update_configs.py`**
  - Seeds runtime config **from the setup/ templates** then fills placeholders
    (`--no-from-template` to patch in place instead).
  - `ECR_REGISTRY_RE` matches both `<ACCOUNT>/<REGION>` placeholders and real
    12-digit account/region forms (idempotent).
  - `genome_references` substitution uses `s3://<your-bucket>/<ref-prefix>/`
    placeholders; recurses into nested lists (e.g. `site_level_comparison_datasets`).
  - Removed the `--old-ref-bucket`/`--old-ref-prefix` flags (not needed in the
    vanilla flow).
  - `update_filtergenotypes()` migrates any legacy hardcoded ECR URI to
    `{{docker:gq_recalibrator_docker}}`; looks in both `config/templates/` and
    `config_grids/templates/`.
- **`gatksv_healthomics/functions/{submit_workflow,submit_batch_workflow,submit_cohort_workflow}/parameter_builder.py`**
  - Added `_resolve_docker_ref()` and call it in the `optional_params` loop.
- **`gatksv_healthomics/shared/python/config/templates/FilterGenotypes.json`**
  - `optional_params.gatk_docker` → `{{docker:gq_recalibrator_docker}}`.
- **`scripts/setup/docker_images.v1.1.template.json`**
  - Rebuilt: 20 entries, `gatk-sv/` namespace, placeholders. (Now regenerated by
    `04 --write-config`.)

### Created
- **`scripts/setup/genome_references.template.json`** — placeholder version of
  `genome_references.json` (`s3://<your-bucket>/<ref-prefix>/...`), hand-maintained
  source of truth for reference paths.
- **`scripts/sync_config_to_s3.sh`** — was referenced by `setup_all.sh` step 11 but
  missing. Syncs the local `config/` tree to `s3://<bucket>/<prefix>/` (default
  prefix `pipeline_config`), matching what `config_loader.py` reads at runtime.
  Uses `aws s3 sync --delete` (S3 mirrors local exactly). Signature:
  `./sync_config_to_s3.sh <bucket> [config-prefix] [profile] [region]`.

---

## 7. How the runtime consumes config (`config_loader.py`)

- Reads `s3://$CONFIG_BUCKET/$CONFIG_PREFIX/<name>.json` and
  `.../templates/<stage>.json`, **S3 takes priority** over the baked-in Lambda layer
  (so config can be edited without redeploying).
- Default `CONFIG_PREFIX` = `pipeline_config`.
- **The submit Lambdas must have env vars `CONFIG_BUCKET` and `CONFIG_PREFIX` set**
  (via `template.yaml` / `samconfig.toml`), otherwise the loader falls back to the
  layer and ignores S3.

---

## 8. `workflow_ids.json` — auto-generated, skippable

`config/workflow_ids.json` maps each pipeline stage to its HealthOmics workflow ID:
`{"<Stage>": {"id": "<workflow_id>", "version": null}}`.

- **It is written automatically by `07_register_workflows.py`** (setup step 8 in
  `setup_all.sh`). That script runs `aws omics create-workflow` per stage, waits for
  ACTIVE, and persists `workflow_ids.json` after each stage. **No manual editing is
  required.**
- The script **preserves existing entries**: it loads the current file and only
  overwrites the stages it (re)registers. Use `--stages <A> <B>` to register a
  subset without clobbering the rest.
- **You can skip step 07 entirely if the workflows already exist** in the target
  account/region (e.g. carried over from a previous run). The existing
  `workflow_ids.json` stays valid **as long as the deploy targets the same
  account+region** those IDs were registered in. Workflow IDs are account/region
  specific — if you deploy to a *different* account, re-run step 07 there.
- **Verify an ID resolves** (quick check when reusing an existing file):
  ```bash
  aws omics get-workflow --id <id> --region <REGION> --profile <PROFILE> --query 'status'
  ```

### Hand-curated entries (not produced by the script)
Some entries are added manually and are unique to this deployment; `07`'s
`STAGE_MAIN_WDL` map does **not** include them, so they are only present because they
were added by hand (and are preserved on re-run):
- `GatherSampleEvidence_ORIGINAL`, `GatherSampleEvidence_DRAGEN44_MELTv222`,
  `GatherSampleEvidence_DRAGEN44_MELTv222_CollectSVEvidence8GB` — variant
  registrations; the plain `GatherSampleEvidence` key points at whichever variant is
  active.
- `FilterBatch` plus `FilterBatchSites` / `FilterBatchSamples` (the latter two
  carry `"note": "Same as FilterBatch"`).

When registering new workflow variants, add/update these entries by hand (or extend
`STAGE_MAIN_WDL` in `07`).

---

## 9. DynamoDB aggregates table — range key MUST be `Event` (not `sk`)

**Gotcha / bug fixed this session.** There are two DynamoDB tables:
- **Sample table** (`<prefix>-sample`): composite key `pk` (HASH) + **`sk`** (RANGE).
  Records like `sk = METADATA`, `<stage>#...`.
- **Aggregates table** (`<prefix>-batch-cohort`): composite key `pk` (HASH) +
  **`Event`** (RANGE). The entire HO codebase keys this table by `{pk, Event}` —
  writer `api/aggregates/app.py` and all readers (`stage_advancer`,
  `healthomics_status_monitor`, `submit_workflow/batch/cohort`, `parameter_builder`).

The original `02_create_dynamodb.sh` created **both** tables with `sk` as the range
key. That made every `PutItem` to the aggregates table fail with
`ValidationException: Missing the key sk in the item` (the item has `pk` + `Event`,
not `sk`). Symptom: batch/cohort aggregates never update; messages retry 5× and hit
the aggregates DLQ.

**Fix applied:**
- `02_create_dynamodb.sh` — `create_table()` now takes a range-key arg; sample table
  uses `sk`, aggregates table uses `Event`.
- The live `gatksv-dev-batch-cohort` table was deleted and recreated with
  `pk` + `Event`. **Recreating changes the stream ARN** → update
  `AggregatesDynamoDBStreamArn` in `samconfig.toml` and redeploy (the
  `EBPipeAggregateEvents` pipe references that stream).

When porting to prod: create the aggregates table with range key `Event`. If an
existing prod table has `sk`, it must be recreated (empty) with `Event` and the
stream ARN refreshed in `samconfig.toml` + redeploy.

---

## 10. ECR repositories need a HealthOmics repository policy

**Gotcha / bug fixed this session.** HealthOmics pulls container images via the
**`omics.amazonaws.com` service principal** (not only via the run's execution role),
so each private ECR repo needs a resource-based repository policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "omics workflow access",
    "Effect": "Allow",
    "Principal": { "Service": "omics.amazonaws.com" },
    "Action": ["ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage", "ecr:BatchCheckLayerAvailability"]
  }]
}
```

The setup previously set only the **execution role's** identity-based ECR permissions
(`06_create_ho_role.sh`), NOT the per-repo policy — so runs could fail to pull images.

**Fix applied in `04_mirror_dockers.py`:**
- `ensure_repo()` now applies this policy to every repo it touches during a mirror
  (idempotent — `set-repository-policy` overwrites each run).
- New **`--fix-policies`** mode retrofits all existing `gatk-sv/*` repos without
  re-mirroring:
  ```bash
  python3 04_mirror_dockers.py --account-id <ACCOUNT> --region <REGION> \
      --profile <PROFILE> --fix-policies
  ```

When porting to prod: after mirroring (or on an existing registry), run with
`--fix-policies` so every `gatk-sv/*` repo grants HealthOmics pull access. Repos for
images that were never mirrored (e.g. MELT until licensed) get the policy
automatically once `ensure_repo()` creates them.

---

## 11. End-to-end command sequence (dev or prod account)

```bash
cd sam_template/scripts/setup

# 1. Mirror dockers from Broad → ECR and generate config + template.
#    (rolls over MELT/licensing failures; exit 0)
python3 04_mirror_dockers.py \
    --account-id <ACCOUNT_ID> --region <REGION> --profile <PROFILE> --write-config

# 2. (Only if re-pointing an existing template to a new account/bucket without
#     re-mirroring — otherwise step 1 already wrote docker_images.json.)
python3 08_update_configs.py \
    --account-id <ACCOUNT_ID> --region <REGION> \
    --ref-bucket <BUCKET> --ref-prefix <REF_PREFIX> \
    --patch-template

# 3. Build + upload the config Lambda layer.
./09_build_config_layer.sh <BUCKET> <PROFILE> <REGION>

# 4. Sync config to S3 (runtime loader reads this first).
cd .. && ./sync_config_to_s3.sh <BUCKET> pipeline_config <PROFILE> <REGION>

# 5. Fill samconfig.toml with printed ARNs, then:
cd .. && sam build && sam deploy --config-env <env>
```

Full new-account flow (buckets, DynamoDB, references, IAM role, workflow
registration) is orchestrated by `setup_all.sh` (steps 1–11 with prompts).

---

## 12. Porting to the private production repo

When copying this setup into the private repo that holds production info:

1. **Copy these files** (they are account-agnostic — placeholders only):
   - `scripts/setup/04_mirror_dockers.py`
   - `scripts/setup/08_update_configs.py`
   - `scripts/setup/docker_images.v1.1.template.json` (placeholders)
   - `scripts/setup/genome_references.template.json` (placeholders)
   - `scripts/sync_config_to_s3.sh`
   - the three `functions/*/parameter_builder.py`
   - `config/templates/FilterGenotypes.json`
2. **Do NOT commit filled files** with real account IDs / buckets:
   - `config/docker_images.json`, `config/genome_references.json` are **generated**
     (by `04 --write-config` / `08`). Keep them out of git or gitignore them; the
     templates are the committed source of truth.
   - `template.yaml.bak_allowedvalues` is a local backup — do not commit.
3. **Prod values** (account ID, bucket names, DynamoDB names, IAM role ARN, KMS ARN,
   region) belong in the private repo's `setup_all.sh` CONFIG block and
   `samconfig.toml`, not in the templates.
4. **Regeneration in prod**: run `04 --write-config` then `08` (or just `08` if not
   re-mirroring) against the prod account. Review the regenerated template diff so
   only placeholders land in git.
5. **MELT**: ensure the prod account has an accessible MELT image (licensed) and
   mirror it, or the FilterGenotypes/MELT-dependent steps will fail at runtime.

---

## 13. Verification done (this session)

- All Python scripts compile (`py_compile`).
- `04` drop-list + `gatk-sv/` prefix verified against Broad's real `dockers.json`
  (19 kept, 8 dropped, all flatten to `gatk-sv/<image>:tag`).
- `08` end-to-end run: seeded from templates, filled 20 docker URIs + 74 genome-ref
  paths for the dev account, no leftover placeholders, no vestigial keys.
- `_resolve_docker_ref()` unit-checked (`{{docker:...}}` → URI; literals/bools
  passthrough).
- **DynamoDB aggregates fix**: root-caused via the aggregates Lambda CloudWatch logs
  (`Missing the key sk`); confirmed table schema, recreated `gatksv-dev-batch-cohort`
  with `pk` + `Event` (ACTIVE), updated stream ARN in `samconfig.toml`, fixed
  `02_create_dynamodb.sh`.
- **ECR omics policy fix**: confirmed repos had no policy, added it to `04`
  (`ensure_repo` + `--fix-policies`), applied live to all 15 `gatk-sv/*` repos and
  verified the policy on `gatk-sv/sv-pipeline`.
- **Not run** (needs docker + AWS creds, or is yours to run): the real `04` mirror,
  `09` layer upload, `sync_config_to_s3.sh`, and the `sam build`/`deploy` needed to
  rebind the aggregates EventBridge pipe to the new stream ARN.
