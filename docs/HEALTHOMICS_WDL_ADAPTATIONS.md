# HealthOmics WDL Adaptations

Reference for anyone taking the WDLs in `wdl/` and running them on AWS HealthOmics with
their own orchestration. These are the changes made to the Broad GATK-SV v1.1.1 WDLs so
they run correctly on the HealthOmics WDL engine (which differs from Cromwell/Terra).

If you use the full `sam_template/` stack, all of this is already handled — this doc is
mainly for the "just the WDLs" path.

---

## 1. File co-location (indexes)

HealthOmics localizes each `File` input into its own directory, so a VCF and its `.tbi`
index do **not** end up side by side. GATK tools that auto-discover an index next to the
VCF then fail.

- Indexes are passed as **explicit WDL inputs** (not reconstructed as `vcf + ".tbi"`).
- Tasks that require co-located files symlink the index next to the data file in the
  command block.

Never use `File idx = file + ".tbi"` — that path won't exist at runtime. Also verify
`call` blocks pass the declared index input rather than reconstructing the path.

## 2. `select_first()` with optional outputs

`select_first([A, B])` throws `EvalError` when **all** values are null, even for `File?`.
Replaced with `if defined(A) then A else B`. Do not just append a default value — that
masks the error and fails later; instead ensure at least one value is non-null at runtime.

## 3. WDL strict-mode quirks

- Optional interpolation: `~{"--flag " + optional_var}` fails when None →
  `~{if defined(var) then "--flag " + select_first([var]) else ""}`
- `default=` with non-string types (`Int?`, `Float?`) fails →
  use `~{select_first([var, DEFAULT])}` or quote the default.
- No implicit `String → File` coercion: `read_lines()` → `Array[File]` yields all `None`.
  Files listed in a manifest must be physically downloaded (see #7).

## 4. GCS auth removed

Broad WDLs export a GCS OAuth token via `gcloud`. On AWS there is no gcloud/GCP creds, so
`export GCS_OAUTH_TOKEN=\`gcloud ...\`` was replaced with `export GCS_OAUTH_TOKEN="" || true`.
(Utils.wdl, Vapor.wdl, Whamg.wdl, DeNovoSVsScatter.wdl)

## 5. No GCP CLI tools in task commands

Any `gsutil`/`gcloud` usage fails on HealthOmics. Where a task pulled files with `gsutil`,
a preceding download task using an AWS-CLI image fetches + tars the files and passes the
tar as a normal `File` input (HealthOmics stages it, no in-task network needed).

## 6. `gz` file gunzip (symlink) issue

HealthOmics-localized files are symlinks; in-place `gunzip` breaks. Copy locally first:
`cp "{}" ./ && gunzip "$(basename {})"`. (GermlineCNVCase/Cohort/Tasks.wdl)

## 7. ~50KB parameter limit → manifest pattern

HealthOmics run-input JSON is capped at ~50KB, which large sample arrays exceed. Affected
stages accept a **manifest file** (one S3 URI per line) instead of an `Array[File]`; a
`ReadManifest` task downloads the files at runtime using an AWS-CLI image. (EvidenceQC.wdl,
GatherBatchEvidence.wdl)

## 8. `write_tsv()` output path

Moving a `write_tsv()` result with `mv` breaks HealthOmics output resolution. Use
`cat "~{write_tsv(...)}" > out.tsv` instead. (MakeBincovMatrix.wdl)

## 9. Float serialization

Python `json.dump(0.000001)` → `1e-06`, which the HealthOmics WDL engine rejects for
`Float?` params. Submit runs via the AWS CLI with `--parameters file://params.json`
(the AWS CLI preserves the literal number) rather than a boto3 client that re-serializes.

---

## Operational notes (HealthOmics platform)

- **ECR pull policy** — each image repo needs a policy allowing `omics.amazonaws.com` to
  `GetDownloadUrlForLayer` / `BatchGetImage` / `BatchCheckLayerAvailability`.
- **Manifest-reader image** — the `ReadManifest` task needs an image with the AWS CLI
  (`dockerfile/dockerfile.awscli`); it is not part of Broad's image set.
- **Throttling** — HealthOmics may return `TooManyRequestsException` yet still accept the
  run; check `list-runs` by name before retrying to avoid duplicates.
- **Run cache** — during development, cached results can replay a prior failure; change the
  cache ID (or use cache-on-failure) after fixing a WDL.

---

## Minimal "just the WDLs" checklist

1. Mirror Broad v1.1.1 Docker images to your ECR (see `scripts/setup/04_mirror_dockers.py`)
   and set the ECR pull policy for `omics.amazonaws.com`.
2. Mirror the reference files to S3 (see `scripts/setup/03_setup_references.sh`).
3. Build the AWS-CLI manifest-reader image (`dockerfile/dockerfile.awscli`).
4. Register each `wdl/` workflow as a HealthOmics workflow.
5. Build per-stage input JSON (references + dockers + your sample data) and call
   `aws omics start-run` — using `--parameters file://...` to avoid float mangling.
