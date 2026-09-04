# GATK-SV → HealthOmics: Version Upgrade Checklist

When a new GATK-SV release lands and you want to re-port it, this is the checklist of
adaptations to re-apply to the WDLs in `wdl/`. All changes are **additive** — no upstream
logic is removed, so a 3-way merge against the new release usually works, followed by
re-applying the items below that the merge didn't carry.

Base for this list: Broad **GATK-SV v1.1.1** (`gatk-sv/wdl/`). ~57 WDLs were modified.

Quick scan commands are given per category to find where each fix must be applied in the
new release.

---

## Category 1 — Explicit index inputs (file co-location)

**Why:** HealthOmics localizes each `File` into its own directory, so `vcf + ".tbi"`
points to a nonexistent path. Indexes must be passed as explicit inputs; tasks needing
co-located files symlink the index next to the data in the command block.

**Scan the new release for constructed index paths:**
```bash
grep -n '+ "\.tbi"\|+ "\.bai"\|+ "\.crai"\|+ "\.csi"' wdl/*.wdl \
  | grep -vE '^\s*#|String |File.*=.*name|outfile|_vcf_name|merged_name|index_file_name'
```
Every active hit is a candidate: add an explicit `File <x>_index` input, comment out the
constructed path, and thread the index through **every caller**.

**Also scan `call` blocks** — a workflow may declare the index input correctly but still
reconstruct the path when calling a sub-workflow (passes validation, fails at runtime):
```bash
grep -n '_index = .* + "\.tbi"\|_idx = .* + "\.tbi"' wdl/*.wdl
```

Files touched in v1.1.1 (non-exhaustive): AnnotateVcf, CollectQcVcfWide, CombineBatches,
DepthClustering, FilterBatch, FilterGenotypes, GatherBatchEvidence, GenerateBatchMetrics,
GenotypeBatch, GenotypeComplexVariants, GenotypeCpxCnvs(PerBatch), JoinRawCalls, MainVcfQc,
MakeCohortVcf, RegenotypeCNVs, ReshardVcf, ResolveComplexVariants, ResolveCpxSv,
SVConcordance, ScatterCpxGenotyping, TasksClusterBatch (SVCluster), TinyResolve,
{SR,PE,BAF,RD}Test(+Chromosome), TrainRDGenotyping, Genotype{PESR,Depth}Part{1,2},
PESRClustering, Vapor, VisualizeCnvs, Whamg.

Add the corresponding `*_idx` reference paths to `genome_references.json` and the relevant
stage templates' `static_params`.

---

## Category 2 — Nested-workflow default-value evaluated as null ⚠️

**Why (the subtle one):** In Cromwell/Terra, when a parent workflow calls a task/subworkflow
and does **not** pass an optional input, the callee's declared default (e.g.
`Int? min_size = 1000000`) is used. On the **HealthOmics** engine, the parent passes an
explicit `null` down, which **overrides** the callee default → the parameter interpolates as
empty string, silently producing wrong behavior (not always an error).

**Symptom:** a stage "runs" but produces incorrect results because a threshold/param came
through empty (e.g. an awk `minsize=` becomes 0, so every event passes a size filter).

**Fix:** at the **call site in the parent**, make the default explicit:
```wdl
# BEFORE (relies on callee default — breaks on HealthOmics)
call Foo { input: min_size = cnmops_large_min_size }

# AFTER (parent supplies the default)
call Foo { input: min_size = select_first([cnmops_large_min_size, 1000000]) }
```
Or set the value explicitly in the stage template's `optional_params` so it's always passed.

**Scan:** hard to grep mechanically. Audit every optional param that has a **meaningful
default in the callee** and is passed from a parent without one. Known v1.1.1 instances:

| File | Param | Default |
|------|-------|---------|
| GatherBatchEvidence.wdl | `cnmops_large_min_size` (→ CNMOPS min_size) | 1000000 |
| GermlineCNVTasks.wdl | `feature_query_lookahead` | 1000000 |
| TrainGCNV.wdl | `min_interval_size` | 101 |
| TrainGCNV.wdl | `max_interval_size` | 2000 |

When in doubt, prefer setting the value in the template `optional_params` — it's platform-
independent.

---

## Category 3 — WDL strict-mode fixes

HealthOmics' WDL engine is stricter than Cromwell:

- **Optional interpolation** — `~{"--flag " + optional_var}` fails when None:
  `~{if defined(var) then "--flag " + select_first([var]) else ""}`
- **`default=` with non-string types** (`Int?`, `Float?`) fails:
  `~{select_first([var, DEFAULT])}` (or quote it: `default="2000"`)
- **`select_first([A, B])` all-null** throws `EvalError` even for `File?`:
  use `if defined(A) then A else B`. Do NOT paper over it with a bogus third default.
- **No `String → File` coercion** — `read_lines()` → `Array[File]` yields all `None`
  (see Category 6, manifests).

**Scan:**
```bash
grep -n '~{"[^}]*" *+ ' wdl/*.wdl           # optional string concat
grep -n 'default=[0-9]' wdl/*.wdl            # unquoted numeric defaults
```

Known files: CollectCoverage (default quoting), TrainGCNV, GermlineCNVTasks.

---

## Category 4 — Remove GCP-native calls

**Why:** no gcloud/gsutil or GCP creds on HealthOmics.

- **GCS auth token:**
  `export GCS_OAUTH_TOKEN=\`gcloud ...\`` → `export GCS_OAUTH_TOKEN="" || true`
  (Utils.wdl, Vapor.wdl, Whamg.wdl, DeNovoSVsScatter.wdl)
- **`gsutil`/`gcloud` in task commands** → replace with a download task using an AWS-CLI
  image (fetch + tar to a `File` the HealthOmics engine stages). Applied to
  CollectSiteLevelBenchmarking.wdl (and MainVcfQc/FilterGenotypes thread the
  `manifest_reader_docker` through).

**Scan:**
```bash
grep -rn 'gsutil\|gcloud\|GCS_OAUTH_TOKEN' wdl/*.wdl
```

---

## Category 5 — `gz` gunzip symlink workaround

**Why:** localized files are symlinks; in-place `gunzip` breaks. Copy locally first:
```bash
grep gz$ "$list" | xargs -l1 -P0 -I {} sh -c 'cp "{}" ./ && gunzip "$(basename "{}")"'
```
Files: GermlineCNVCase.wdl, GermlineCNVCohort.wdl, GermlineCNVTasks.wdl.

---

## Category 6 — ~50KB parameter limit → manifest pattern

**Why:** HealthOmics run-input JSON caps at ~50KB; large sample arrays exceed it.

For each large `Array[File]` input, add an optional `<x>_manifest` (S3-URI-per-line file)
and a `ReadManifest` task (AWS-CLI image) that downloads at runtime:
```wdl
Array[File]? original_array
File? original_array_manifest
String? manifest_reader_docker
if (defined(original_array_manifest)) {
  call ReadManifest as ReadX { input: manifest = select_first([original_array_manifest]),
        manifest_reader_docker = select_first([manifest_reader_docker]) }
}
Array[File] resolved_array = select_first([ReadX.files, original_array])
```
Files: EvidenceQC.wdl, GatherBatchEvidence.wdl (both define the `ReadManifest` task).

---

## Category 7 — `write_tsv()` output path

`mv "~{write_tsv(...)}" out.tsv` breaks HealthOmics output resolution; use
`cat "~{write_tsv(...)}" > out.tsv`. (MakeBincovMatrix.wdl)

---

## Category 8 — Runtime / resource tuning

HealthOmics maps requested cpu+mem to the smallest fitting omics instance. Some tasks
needed bumps for stability at production scale:

| File | Task | Before | After |
|------|------|--------|-------|
| TasksClusterBatch.wdl | SVCluster | 3.75 GB | 5.75 GB |
| Scramble.wdl | MakeScrambleVcf | 3.0 GB | 8.0 GB |
| RecalibrateGq.wdl | (java mem) | 9.0 GB | 14.0 GB |
| CollectSVEvidence.wdl | RunCollectSVEvidence | 1 CPU / 3.75 GB | 2 CPU / 4 GB (bump to 8 GB if GC-thrashing) |
| MELT.wdl | (if used) | 1 CPU / 7 GB | 2 CPU / 16 GB |

Note: the omics tier boundaries mean 4 GB and 8 GB map to different instances — verify the
task actually gets the memory you expect (see `docs/HEALTHOMICS_WDL_ADAPTATIONS.md`).

**Watch for OOM that manifests as a hang, not a crash:** if a JVM task is just under its
limit it GC-thrashes and stalls for hours instead of exiting. Bump memory in the WDL and
re-register the workflow (the registered workflow, not the on-disk file, is what runs).

---

## Category 9 — FilterGenotypes header workarounds

- Add `MINSL` INFO header before `apply_sl_filter.py` (script expects it; VCF may lack it).
- Add `LOW_QUALITY` FILTER header if missing (bcftools annotate needs it to remove it).

---

## Category 10 — GatherSampleEvidence caller wiring (dragen support)

`GatherSampleEvidence.wdl` has the largest delta. Key additions:
- `FailWithMessage` task — enforce ≥1 SV caller provided.
- optional caller/index inputs; `defined()` guards instead of `select_first()` on optional
  caller outputs.
- `LocalizeReads` uses `samtools_cloud_docker`.

(Dragen-4.x-specific BND→INV and MELT-v2.2 work is intentionally **not** part of the
baseline HealthOmics port — omit unless targeting dragen 4.x.)

---

## Cross-cutting: config + platform

- **`genome_references.json`** — add every new `*_idx` path introduced by Category 1.
- **Stage templates** (`static_params` / `docker_params` / `optional_params`) — add new
  reference keys, `manifest_reader_docker` where manifests/downloads are used, and any
  Category-2 defaults you chose to set at the template level.
- **ECR pull policy** — each mirrored image repo needs `omics.amazonaws.com` allowed to
  `GetDownloadUrlForLayer` / `BatchGetImage` / `BatchCheckLayerAvailability`.
- **Submit via AWS CLI `--parameters file://...`** — avoids Python float→`1e-06` mangling
  the engine rejects.
- **Re-register workflows** after WDL edits — the registered HealthOmics workflow is
  immutable; changes on disk don't take effect until you register a new workflow ID and
  update `workflow_ids.json`.

---

## Recommended upgrade order

1. 3-way merge the new release over `wdl/` (or copy fresh + re-apply below).
2. Category 1 + 3 (mechanical, low risk) — run the scan commands, fix every hit.
3. Category 4 (GCP removal) — scan for gsutil/gcloud/GCS_OAUTH_TOKEN.
4. Category 2 (nested defaults) — audit optional params with meaningful callee defaults.
5. Categories 5–7, 9 (targeted per-file).
6. Category 8 (resource tuning) — start from the table, adjust from run_analyzer reports.
7. Category 10 (GSE wiring) — largest, test first.
8. Update `genome_references.json` + templates; re-register workflows; test
   GatherSampleEvidence, then batch stages, then cohort stages.

## Legacy files — do not port

- `GATKSVPipelineBatch.wdl` — monolithic wrapper, unused in the stage-by-stage pipeline.
- `MakeGqRecalibratorTrainingSetFromPacBio.wdl` — PacBio training path, not active.
