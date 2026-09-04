# HealthOmics Status Monitor

Tracks HealthOmics run completion and advances the pipeline. Updates the sample record and the batch/cohort counters in DynamoDB, then triggers the next stage.

## Event Source

Triggered by an EventBridge rule on HealthOmics run state changes:

- **Source:** `aws.omics`
- **Detail Type:** `Run Status Change`
- **Status matched:** `COMPLETED`, `FAILED`, `CANCELLED`
- **Filter:** `runName` prefix `${Env}-<Stage>-` for each pipeline stage (env-scoped to avoid cross-stack leakage)

## Run Name Parsing

Stage, entity, and retry are parsed from the **run name** (not tags):

```
{env}-{stage}-{entity_id}-r{retry}
e.g.  dev-GatherSampleEvidence-HG00171-r0
```

- `parts[0]` = env
- `parts[1]` = workflow stage
- `parts[2:-1]` = entity ID (sample or batch/cohort ID; may contain hyphens)
- `parts[-1]` = retry (`rN`)

## Actions

1. Reads `runId`, `runName`, `status`, `runOutputUri` from the event `detail`
2. Parses stage + entity + retry from the run name
3. Writes a status event record to the sample/aggregates table (with `output_uri`, error message on failure)
4. Updates batch/cohort completion counters (`Count_Completed_Entities`, `Count_Failed_Entities`)
5. Advances the pipeline — when a batch/cohort reaches its expected count, the next stage's PENDING record is written, firing the DDB stream → next submit Lambda

## Environment

| Var | Purpose |
|-----|---------|
| `DDB_TABLE_NAME` | Sample table |
| `AGGREGATES_TABLE_NAME` | Batch/cohort table |

## Testing

Simulate a completion event:
```bash
aws events put-events --entries '[
  {
    "Source": "aws.omics",
    "DetailType": "Run Status Change",
    "Detail": "{\"runId\":\"1234567\",\"runName\":\"dev-GatherSampleEvidence-HG00096-r0\",\"status\":\"COMPLETED\",\"runOutputUri\":\"s3://<bucket>/<out>/GatherSampleEvidence/HG00096\"}"
  }
]' --region <region> --profile <profile>
```

## Monitoring

```bash
aws logs tail /aws/lambda/<owner>-<vendor>-<env>-gatksv-healthomics-status-monitor --follow
```
