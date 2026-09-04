"""
StageAdvancer Lambda

Triggers the next pipeline stage when a DDB aggregates record transitions to COMPLETED.
Listens to DDB stream events via EventBridge (custom bus).

Interface: Any DDB update with Status=COMPLETED on BATCH# or COHORT# records triggers advancement.
This enables both automated (from RunStatusRecorder) and manual (DDB console/CLI) triggers.
"""

import json
import boto3
import os
import datetime

SNS_AGGREGATES_ARN = os.environ['SNS_AGGREGATES_ARN']
AGGREGATES_TABLE_NAME = os.environ.get('AGGREGATES_TABLE_NAME', 'gatk-sv-dev-batch-cohort')
OUTPUT_BUCKET = os.environ.get('OUTPUT_BUCKET', '<your-bucket>')

sns = boto3.client('sns')
s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

# Batch stage auto-progression: None = QC gate (manual trigger required)
BATCH_STAGE_NEXT = {
    'EvidenceQC': None,
    'TrainGCNV': 'GatherBatchEvidence',
    'GatherBatchEvidence': 'ClusterBatch',
    'ClusterBatch': 'GenerateBatchMetrics',
    'GenerateBatchMetrics': 'FilterBatchSites',
    'FilterBatchSites': None,
    'FilterBatchSamples': None,
}

# Cohort stage auto-progression
COHORT_STAGE_NEXT = {
    'MergeBatchSites': 'GenotypeBatch',
    'GenotypeBatch': 'RegenotypeCNVs',
    'RegenotypeCNVs': 'MakeCohortVcf',
    'MakeCohortVcf': 'RefineComplexVariants',
    'RefineComplexVariants': 'JoinRawCalls',
    'JoinRawCalls': 'SVConcordance',
    'SVConcordance': 'FilterGenotypes',
    'FilterGenotypes': 'AnnotateVcf',
    'AnnotateVcf': None,
}


def lambda_handler(event, context):
    """
    Process DDB stream events from aggregates table (via EventBridge Pipe → custom bus).
    Triggers next stage when Status=COMPLETED.
    """
    print(f"Event: {json.dumps(event)}")

    # Parse DDB stream event from EventBridge
    detail = event.get('detail', {})
    ddb_event = detail.get('dynamodb', {})
    event_name = detail.get('eventName', '')

    # Only process INSERT/MODIFY with NewImage
    if event_name not in ('INSERT', 'MODIFY'):
        return {'statusCode': 200, 'body': 'Skipped: not INSERT/MODIFY'}

    new_image = ddb_event.get('NewImage', {})
    pk = new_image.get('pk', {}).get('S', '')
    event_stage = new_image.get('Event', {}).get('S', '')
    status = new_image.get('Status', {}).get('S', '')

    # Only act on COMPLETED status
    if status != 'COMPLETED':
        return {'statusCode': 200, 'body': f'Skipped: status={status}'}

    print(f"Processing: {pk} / {event_stage} = {status}")

    try:
        if pk.startswith('BATCH#'):
            batch_id = pk.replace('BATCH#', '')
            handle_batch_completed(batch_id, event_stage)

        elif pk.startswith('COHORT#'):
            cohort_id = pk.replace('COHORT#', '')
            handle_cohort_completed(cohort_id, event_stage)

        return {'statusCode': 200, 'body': f'Processed: {pk}/{event_stage}'}

    except Exception as e:
        print(f"✗ Error: {e}")
        raise


def handle_batch_completed(batch_id, completed_stage):
    """Handle batch stage completion — advance to next stage or stop at QC gate."""

    # FilterBatchSamples: update cohort counter
    if completed_stage == 'FilterBatchSamples':
        update_cohort_counter(batch_id)

    # Auto-advance
    if completed_stage in BATCH_STAGE_NEXT:
        next_stage = BATCH_STAGE_NEXT[completed_stage]
        if next_stage:
            trigger_next_batch_stage(batch_id, next_stage, completed_stage)
        else:
            print(f"QC gate: {completed_stage} completed for {batch_id} — manual trigger required")


def handle_cohort_completed(cohort_id, completed_stage):
    """Handle cohort stage completion — advance to next stage."""

    if completed_stage in COHORT_STAGE_NEXT:
        next_stage = COHORT_STAGE_NEXT[completed_stage]
        if next_stage:
            aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
            resp = aggregates_table.get_item(
                Key={'pk': f'COHORT#{cohort_id}', 'Event': 'FilterBatchSamples'}
            )
            cohort_record = resp.get('Item', {})
            trigger_cohort_workflow(cohort_id, next_stage, cohort_record)
        else:
            print(f"Pipeline complete: {completed_stage} finished for cohort {cohort_id}")


def trigger_next_batch_stage(batch_id, next_stage, completed_stage):
    """Publish PENDING message for next batch workflow stage."""
    aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
    response = aggregates_table.get_item(
        Key={'pk': f'BATCH#{batch_id}', 'Event': completed_stage}
    )
    completed_record = response.get('Item', {})
    processed_entities = list(completed_record.get('processed_entities', set()))
    total = int(completed_record.get('Count_Total_Entities', 0))
    ped_file = completed_record.get('Info', {}).get('ped_file', '')

    message = {
        'pk': f'BATCH#{batch_id}',
        'Event': next_stage,
        'Status': 'PENDING',
        'Count_Total_Entities': total,
        'processed_entities': processed_entities,
        'Info': {
            'triggered_by': completed_stage,
            'retry_count': 0,
            'ped_file': ped_file
        }
    }

    sns.publish(
        TopicArn=SNS_AGGREGATES_ARN,
        Message=json.dumps(message),
        MessageGroupId=batch_id,
        MessageDeduplicationId=f"{batch_id}-{next_stage}-pending-from-{completed_stage}"
    )
    print(f"✓ Auto-advancing {batch_id}: {completed_stage} → {next_stage}")


def trigger_cohort_workflow(cohort_id, workflow_stage, cohort_record):
    """Publish PENDING message for a cohort-level workflow."""
    info = cohort_record.get('Info', {})
    batch_ids = sorted(list(info.get('entity_member', [])))
    if not batch_ids:
        batch_ids = sorted(list(cohort_record.get('processed_entities', set())))

    message = {
        'pk': f'COHORT#{cohort_id}',
        'Event': workflow_stage,
        'Status': 'PENDING',
        'Count_Total_Entities': len(batch_ids),
        'Info': {
            'triggered_by': 'FilterBatchSamples',
            'cohort_id': cohort_id,
            'entity_member': batch_ids,
        }
    }
    if batch_ids:
        message['processed_entities'] = batch_ids

    sns.publish(
        TopicArn=SNS_AGGREGATES_ARN,
        Message=json.dumps(message),
        MessageGroupId=cohort_id,
        MessageDeduplicationId=f"{cohort_id}-{workflow_stage}-pending"
    )
    print(f"✓ Triggered {workflow_stage} for cohort {cohort_id} ({len(batch_ids)} batches)")


def update_cohort_counter(batch_id):
    """When FilterBatchSamples completes for a batch, update cohort counter and trigger MergeBatchSites if all done."""
    aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)

    scan_resp = aggregates_table.scan(
        FilterExpression='begins_with(pk, :prefix) AND #evt = :evt',
        ExpressionAttributeNames={'#evt': 'Event'},
        ExpressionAttributeValues={':prefix': 'COHORT#', ':evt': 'FilterBatchSamples'}
    )
    items = [i for i in scan_resp.get('Items', [])
             if batch_id in i.get('Info', {}).get('entity_member', [])]

    if not items:
        print(f"No COHORT FilterBatchSamples entry with batch {batch_id}")
        return

    cohort_item = items[0]
    cohort_pk = cohort_item['pk']
    entity_member = cohort_item.get('Info', {}).get('entity_member', [])

    resp = aggregates_table.update_item(
        Key={'pk': cohort_pk, 'Event': 'FilterBatchSamples'},
        UpdateExpression='ADD processed_entities :batch_set SET last_updated = :ts',
        ExpressionAttributeValues={
            ':batch_set': {batch_id},
            ':ts': datetime.datetime.now(datetime.UTC).isoformat()
        },
        ReturnValues='ALL_NEW'
    )

    updated = resp.get('Attributes', {})
    processed = updated.get('processed_entities', set())

    print(f"Cohort {cohort_pk}: {len(processed)}/{len(entity_member)} batches done")

    if set(entity_member).issubset(processed) and len(processed) > 0:
        cohort_id = cohort_pk.replace('COHORT#', '')
        cohort_ped_file = collate_cohort_ped_file(cohort_id, entity_member)
        if cohort_ped_file:
            aggregates_table.update_item(
                Key={'pk': cohort_pk, 'Event': 'FilterBatchSamples'},
                UpdateExpression='SET Info.ped_file = :pf',
                ExpressionAttributeValues={':pf': cohort_ped_file}
            )
            resp2 = aggregates_table.get_item(Key={'pk': cohort_pk, 'Event': 'FilterBatchSamples'})
            updated = resp2.get('Item', updated)

        print(f"All batches done — triggering MergeBatchSites for {cohort_id}")
        trigger_cohort_workflow(cohort_id, 'MergeBatchSites', updated)


def collate_cohort_ped_file(cohort_id, batch_ids):
    """Concatenate per-batch ped files into a single cohort ped file."""
    aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)

    ped_paths = []
    for bid in batch_ids:
        for stage in ['GatherBatchEvidence', 'EvidenceQC', 'FilterBatchSamples']:
            resp = aggregates_table.get_item(Key={'pk': f'BATCH#{bid}', 'Event': stage})
            item = resp.get('Item', {})
            pf = item.get('Info', {}).get('ped_file', '')
            if pf:
                ped_paths.append(pf)
                break

    if not ped_paths:
        print(f"WARNING: No ped_file found for any batch in {batch_ids}")
        return None

    header = None
    seen_samples = set()
    lines = []
    for ped_uri in ped_paths:
        bucket = ped_uri.replace('s3://', '').split('/')[0]
        key = ped_uri.replace('s3://', '').split('/', 1)[1]
        body = s3.get_object(Bucket=bucket, Key=key)['Body'].read().decode('utf-8')
        for line in body.strip().split('\n'):
            if line.startswith('#'):
                if header is None:
                    header = line
                continue
            sample_id = line.split('\t')[1] if '\t' in line else line.split()[1]
            if sample_id not in seen_samples:
                seen_samples.add(sample_id)
                lines.append(line)

    content = ''
    if header:
        content = header + '\n'
    content += '\n'.join(lines) + '\n'

    cohort_ped_key = f"qc_outputs/evidence_qc/batching/ped_files/{cohort_id}.ped"
    s3.put_object(Bucket=OUTPUT_BUCKET, Key=cohort_ped_key, Body=content)
    cohort_ped_uri = f"s3://{OUTPUT_BUCKET}/{cohort_ped_key}"
    print(f"✓ Cohort ped file: {cohort_ped_uri} ({len(lines)} samples)")
    return cohort_ped_uri
