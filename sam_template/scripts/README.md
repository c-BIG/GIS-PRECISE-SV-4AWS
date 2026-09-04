# Scripts

## Active Scripts

### submit_samples_sqs.py

Unified sample initialization and submission script. Handles multiple modes:

| Mode | Description | Triggers GSE? |
|------|-------------|---------------|
| `hot` | CRAMs in S3 standard storage. Sends SQS immediately. | Yes (via SQS → DDB → EventBridge) |
| `glacier` | CRAMs in Glacier. Writes GLACIER_QUEUE for restore orchestrator. | No (orchestrator handles after restore) |
| `pending` | CRAMs don't exist yet (e.g., dragen still running). | No (external signal needed) |
| `waiting` | Registered, waiting for external signal. | No |
| `redrive` | Resend SQS for failed samples. No METADATA or aggregates writes. | Yes |

All modes except `redrive`: write METADATA to sample DDB.
Glacier mode: also writes GLACIER_QUEUE entries (pk=`GLACIER_QUEUE`, sk=`batch_id#sample_id`).
Aggregates DDB batch definitions: only sent if `--aggregates-queue-url` is provided.

```bash
# Hot storage — submit to pipeline immediately
python3 scripts/submit_samples_sqs.py --mode hot \
  --queue-url <sample-sqs-url> \
  --aggregates-queue-url <aggregates-sqs-url> \
  --csv-file samples.csv \
  --target-batch-size 500 --min-batch-size 100

# Glacier — register for restore orchestrator
python3 scripts/submit_samples_sqs.py --mode glacier \
  --sample-table gatk-sv-dev-sample \
  --aggregates-queue-url <aggregates-sqs-url> \
  --csv-file samples.csv

# Glacier — without creating new batch entries (batch already exists)
python3 scripts/submit_samples_sqs.py --mode glacier \
  --sample-table gatk-sv-dev-sample \
  --csv-file samples.csv

# Redrive failed GSE — resend SQS only, no DDB writes
python3 scripts/submit_samples_sqs.py --mode redrive \
  --queue-url <sample-sqs-url> \
  --csv-file failed_samples.csv

# Dry run (any mode)
python3 scripts/submit_samples_sqs.py --mode hot --csv-file samples.csv --dry-run
```

**CSV format** (comma or tab):
```
sample_id,gender,cram,crai,dragen_vcf,dragen_vcf_index,dragen_wgs_coverage_metrics,melt_preprocess_s3_uri
HG00096,male,s3://bucket/HG00096.cram,s3://bucket/HG00096.cram.crai,s3://bucket/HG00096.sv.vcf.gz,s3://bucket/HG00096.sv.vcf.gz.tbi,,
```
Required: `sample_id`, `gender`, `cram`, `crai` (or `cram_path`, `cram_index_path`)
Optional: `dragen_vcf`, `dragen_vcf_index`, `manta_vcf`, `manta_vcf_index`, `dragen_wgs_coverage_metrics`, `melt_preprocess_s3_uri`

**Sample ID validation**: Must be alphanumeric + underscores only (no hyphens, spaces, or special characters). Hyphens are used as delimiters in HealthOmics run names.

**Batching**:
- Sorts samples by `sample_id` lexicographically
- Gender-balanced within each batch
- Runt batch handling: if last batch < `--min-batch-size`, merges with previous and splits evenly
- `--batch-prefix` (default: `batch`), `--batch-start-num` (auto-detect if not set)

**DDB records created (glacier mode)**:
```
METADATA:       pk=SAMPLE#HG00096, sk=METADATA, Status=REGISTERED, Info={cram_path, dragen_vcf, ...}
GLACIER_QUEUE:  pk=GLACIER_QUEUE, sk=batch_0001#HG00096, Status=TO_RESTORE
```
### bulk_trigger_filterbatch.py

Bulk-start FilterBatchSamples after QC inspection (sets nIQR cutoff).

```bash
# Dry run
python3 scripts/bulk_trigger_filterbatch.py --dry-run

# Start ALL
python3 scripts/bulk_trigger_filterbatch.py

# HEALTHOMICS stack only
python3 scripts/bulk_trigger_filterbatch.py \
  --table gatk-sv-dev-batch-cohort --limit 0 \
  --stage FilterBatchSites \
  
# BATCH stack only
python3 scripts/bulk_trigger_filterbatch.py \
  --table gatk-sv-dev-batch-cohort-BATCH --limit 0
```

---
### redrive_failed_gse.py

Redrive failed GatherSampleEvidence samples. Scans sample DDB for samples whose latest GSE event is FAILED, then sends a new PENDING message to SQS to retrigger the pipeline. Supports throttling to avoid overwhelming EFS/Batch.

```bash
# Dry run — see which samples would be redriven
python3 scripts/redrive_failed_gse.py --dry-run

# Redrive all failed GSE samples (no throttle)
python3 scripts/redrive_failed_gse.py

# Redrive specific samples only
python3 scripts/redrive_failed_gse.py --sample-ids NPM1019F1D NPM1019F9B

# Redrive first 10 only
python3 scripts/redrive_failed_gse.py --limit 10

# Throttled: 2 seconds between each message
python3 scripts/redrive_failed_gse.py --delay 2

# Throttled: send 10, pause 60s, send 10 more, ...
python3 scripts/redrive_failed_gse.py --batch-size 10 --batch-delay 60

# Combined: 1s per message + extra 30s pause every 20 messages
python3 scripts/redrive_failed_gse.py --delay 1 --batch-size 20 --batch-delay 30
```

| Arg | Description | Default |
|-----|-------------|---------|
| `--sample-table` | DDB table name | `gatk-sv-prod-sample-BATCH` |
| `--queue-url` | SQS FIFO queue for sample events | prod queue |
| `--sample-ids` | Specific sample IDs to redrive | all failed |
| `--limit` | Max samples to redrive | 0 (unlimited) |
| `--delay` | Seconds between each SQS message | 0 |
| `--batch-size` | Messages per batch (for batch-delay) | 10 |
| `--batch-delay` | Seconds to pause every batch-size messages | 0 |
| `--dry-run` | Show what would be redriven, don't send | off |

---

