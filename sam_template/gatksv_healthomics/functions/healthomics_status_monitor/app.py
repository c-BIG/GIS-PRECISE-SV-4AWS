import json
import boto3
import os
import datetime
import io

# Enrionment variable
SNS_SAMPLE_ARN          = os.environ['SNS_SAMPLE_ARN']
SNS_AGGREGATES_ARN      = os.environ['SNS_AGGREGATES_ARN']
BATCH_CHECKER_SNS_ARN   = os.environ['BATCH_CHECKER_SNS_ARN']
DDB_TABLE_NAME          = os.environ.get('DDB_TABLE_NAME', 'gatk-sv-dev')
AGGREGATES_TABLE_NAME   = os.environ.get('AGGREGATES_TABLE_NAME', 'gatk-sv-aggregates')
OUTPUT_BUCKET           = os.environ.get('OUTPUT_BUCKET', '<your-bucket>')

sns = boto3.client('sns')
s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

# Workflow type mappings
SAMPLE_WORKFLOWS = [
    'GatherSampleEvidence-'
]

BATCH_WORKFLOWS = [
    'EvidenceQC-',
    'TrainGCNV-',
    'GatherBatchEvidence-',
    'ClusterBatch-',
    'GenerateBatchMetrics-',
    'FilterBatchSites-',
    'FilterBatchSamples-'
]

COHORT_WORKFLOWS = [
    'MergeBatchSites-',
    'GenotypeBatch-',
    'RegenotypeCNVs-',
    'MakeCohortVcf-',
    'RefineComplexVariants-',
    'JoinRawCalls-',
    'SVConcordance-',
    'FilterGenotypes-', 
    'AnnotateVcf-'
]

# Batch stage auto-progression: None = QC gate (manual trigger required)
BATCH_STAGE_NEXT = {
    'EvidenceQC': None,                    # QC gate
    'TrainGCNV': 'GatherBatchEvidence',
    'GatherBatchEvidence': 'ClusterBatch',
    'ClusterBatch': 'GenerateBatchMetrics',
    'GenerateBatchMetrics': 'FilterBatchSites',
    'FilterBatchSites': None,              # QC gate — review SV count plots, set nIQR, then trigger FilterBatchSamples
    'FilterBatchSamples': None,            # cohort counter handles MergeBatchSites trigger
}

# Cohort stage auto-progression
# Order: MergeBatchSites → GenotypeBatch → RegenotypeCNVs → MakeCohortVcf →
#         RefineComplexVariants → JoinRawCalls → SVConcordance → FilterGenotypes → AnnotateVcf
COHORT_STAGE_NEXT = {
    'MergeBatchSites': 'GenotypeBatch',
    'GenotypeBatch': 'RegenotypeCNVs',
    'RegenotypeCNVs': 'MakeCohortVcf',
    'MakeCohortVcf': 'RefineComplexVariants',
    'RefineComplexVariants': 'JoinRawCalls',
    'JoinRawCalls': 'SVConcordance',
    'SVConcordance': 'FilterGenotypes',
    'FilterGenotypes': 'AnnotateVcf',
    'AnnotateVcf': None,                   # Pipeline complete
}

def get_workflow_type(run_name):
    """Determine workflow type from run name"""
    if any(prefix in run_name for prefix in SAMPLE_WORKFLOWS):
        return 'SAMPLE'
    elif any(prefix in run_name for prefix in BATCH_WORKFLOWS):
        return 'BATCH'
    elif any(prefix in run_name for prefix in COHORT_WORKFLOWS):
        return 'COHORT'
    else:
        return 'UNKNOWN'

def lambda_handler(event, context):
    """
    Process HealthOmics Task Status Change events
    Update sample records AND batch counters for efficiency
    """
   
    print(f"Event: {json.dumps(event)}")
    
    detail = event['detail']
    timestamp = event['time']
    run_id = detail['runId']
    status = detail['status']
    run_name = detail.get('runName', '')
    run_output = detail.get('runOutputUri', '')
    
    print(f"Processing: {run_name} (ID: {run_id}) - Status: {status}")
    
    # Format complete_time 
    dt_object = datetime.datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    healthomics_timestamp = dt_object.isoformat()
    
    # Check if this is a GATK-SV run by name pattern
    workflow_type = get_workflow_type(run_name)
    print(f"Detected workflow type: {workflow_type}")
    
    if workflow_type == 'UNKNOWN':
        print(f"Ignoring non-GATK-SV run: {run_name}")
        return {'statusCode': 200, 'body': f'Non-GATK-SV run ignored: {run_name}'}
    
    # Extract workflow stage, entity ID, and retry count from run name
    # Format: ENV-WorkflowStage-EntityId-rN (e.g., dev-GatherSampleEvidence-HG00171-r0)
    try:
        parts = run_name.split('-')
        env_prefix = parts[0]
        workflow_stage = parts[1]
        retry_count = int(parts[-1].replace('r', ''))
        sample_id = '-'.join(parts[2:-1])  # Everything between stage and retry
        
        if not sample_id:
            raise ValueError("No entity ID found")
        
        print(f"Parsed - Env: {env_prefix}, WorkflowStage: {workflow_stage}, EntityId: {sample_id}, Retry: {retry_count}")
        
    except (IndexError, ValueError) as e:
        print(f"Error parsing run name '{run_name}': {e}")
        return {'statusCode': 400, 'body': f'Invalid run name format: {run_name}'}
        
    # Build info object -> removed cache, Run has no cache, only Task has cache
    info = {
        'run_id': run_id,
        'run_arn': detail.get('arn', ''),
        'workflow_type': workflow_type,
        'workflow_id': detail.get('workflowId', ''),
        'retry_count': retry_count
    }
    # print(f"Info: {json.dumps(info)}")
    
    # Add failure details if failed
    if status in ['FAILED', 'CANCELLED']:
        info['status_message'] = detail.get('statusMessage', '')
        info['reason'] = detail.get('reason', '')
    
    try:
        
        # Update batch counters for efficiency
        if workflow_type == 'SAMPLE' and workflow_stage == 'GatherSampleEvidence':
            # 1. Update sample record 
            update_sample_record(sample_id, workflow_stage, status, info, workflow_type, healthomics_timestamp, run_id, run_output)
            print(f"Updated sample_record: {sample_id}")
            
            # GatherSampleEvidence: Build processed_entities set incrementally
            batch_id = get_sample_batch_id(sample_id)
            if batch_id:
                update_batch_counters_and_check_readiness(batch_id, status, sample_id, retry_count, workflow_stage, run_output)
                print(f"Updated batch_record: {batch_id}")

        elif workflow_type == 'BATCH':
            # Batch workflow completed — update aggregates record
            batch_id = sample_id  # For batch runs, entity ID is the batch_id
            update_batch_counters_and_check_readiness(batch_id, status, None, retry_count, workflow_stage, run_output)
            print(f"Updated batch_record: {batch_id}")
            
            # FilterBatch: update COHORT-level counters for cohort readiness tracking
            if workflow_stage == 'FilterBatchSamples' and status in ('COMPLETED', 'FAILED'):
                update_cohort_counter(batch_id, status)
            
            # Stage advancement now handled by StageAdvancer Lambda via DDB stream
            if status == 'COMPLETED' and workflow_stage in BATCH_STAGE_NEXT:
                next_stage = BATCH_STAGE_NEXT[workflow_stage]
                if next_stage:
                    print(f"Stage advancement delegated to StageAdvancer: {workflow_stage} → {next_stage}")
                else:
                    print(f"QC gate: {workflow_stage} completed for {batch_id} — manual trigger required for next stage")

        elif workflow_type == 'COHORT':
            # Cohort workflow completed — update aggregates record
            cohort_id = sample_id  # For cohort runs, entity ID is the cohort_id
            aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
            aggregates_table.update_item(
                Key={'pk': f'COHORT#{cohort_id}', 'Event': workflow_stage},
                UpdateExpression='SET #st = :status, last_updated = :ts, output_uri = :uri',
                ExpressionAttributeNames={'#st': 'Status'},
                ExpressionAttributeValues={
                    ':status': status,
                    ':ts': datetime.datetime.now(datetime.UTC).isoformat(),
                    ':uri': run_output
                }
            )
            print(f"Updated cohort_record: {cohort_id} / {workflow_stage} = {status}")
            
            # Stage advancement now handled by StageAdvancer Lambda via DDB stream
            if status == 'COMPLETED' and workflow_stage in COHORT_STAGE_NEXT:
                next_stage = COHORT_STAGE_NEXT[workflow_stage]
                if next_stage:
                    print(f"Stage advancement delegated to StageAdvancer: {workflow_stage} → {next_stage}")
                else:
                    print(f"Pipeline complete: {workflow_stage} finished for cohort {cohort_id}")
        
        return {
            'statusCode': 200, 
            'body': json.dumps({
                'run_id': run_id, 
                'status': status,
                'workflow_type': workflow_type,
                'sample_id': sample_id,
                'workflow_stage': workflow_stage
            })
        }
        
    except Exception as e:
        print(f"✗ Error processing HealthOmics status: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e), 'run_id': run_id})
        }


def update_sample_record(sample_id, workflow_stage, status, info, workflow_type, timestamp, run_id, run_output):
    """Update sample record (existing functionality)"""
    
    # Determine SNS routing
    if workflow_type == 'SAMPLE':
        sns_topic = SNS_SAMPLE_ARN
    elif workflow_type in ['BATCH', 'COHORT']:
        sns_topic = SNS_AGGREGATES_ARN
    else:
        raise ValueError(f"Unknown workflow type: {workflow_type}")
    
    # Build message in format expected by event handlers
    # Use the timestamp from Healthomics COMPLETED event
    message = {
        'SampleId': sample_id,
        'Event': workflow_stage,
        'Status': status,
        'Timestamp': timestamp,
        'OutputUrl': run_output,
        'Info': info
    }
    
    print(f"Built message: {json.dumps(message)}")
    
    # Publish to appropriate SNS
    response = sns.publish(
        TopicArn=sns_topic,
        Message=json.dumps(message),
        MessageGroupId=sample_id,
        MessageDeduplicationId=f"{sample_id}-{workflow_stage}-{status}-{run_id}",
        Subject=f'HealthOmics {status}: {workflow_stage}-{sample_id}'
    )
    
    print(f"✓ Published sample update to SNS: {response['MessageId']}")


def get_sample_batch_id(sample_id):
    """Get batch ID for a sample from sample DDB record"""
    
    try:
        sample_table = dynamodb.Table(DDB_TABLE_NAME)
        
        # Query for all records with this sample's pk
        response = sample_table.query(
            KeyConditionExpression='pk = :pk',
            ExpressionAttributeValues={':pk': f'SAMPLE#{sample_id}'},
            Limit=1  # Just need one record to get batch_id
        )
        
        items = response.get('Items', [])
        if items:
            item = items[0]  # Take the first record
            if 'Info' in item:
                return item['Info'].get('batch_id_initial')
        
        return None
        
    except Exception as e:
        print(f"Error getting batch ID for sample {sample_id}: {e}")
        return None


def get_batch_id_from_run(run_id):
    """Extract batch_id from batch workflow run parameters"""
    try:
        # Query HealthOmics to get run details and extract batch_id from parameters
        response = healthomics.get_run(id=run_id)
        parameters = response.get('parameters', {})
        
        # Look for batch_id in common parameter names
        batch_id = parameters.get('batch_id') or parameters.get('batchId')
        return batch_id
        
    except Exception as e:
        print(f"Error extracting batch_id from run {run_id}: {e}")
        return None


def update_batch_counters_and_check_readiness(batch_id, status, sample_id, retry_count, workflow_stage, run_output):
    """Update batch records - counting only for GatherSampleEvidence, inherit processed_entities for others"""
    
    aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
    
    try:
        if workflow_stage == 'GatherSampleEvidence':
            # GatherSampleEvidence: Build processed_entities set + count tracking for batch readiness
            if status == 'COMPLETED':
                update_expression = 'ADD processed_entities :sid_set SET last_updated = :timestamp'
                expression_values = {
                    ':timestamp': datetime.datetime.now(datetime.UTC).isoformat(),
                    ':sid_set': {sample_id}
                }
            else:
                update_expression = 'ADD Count_Failed_Entities :inc SET last_updated = :timestamp'
                expression_values = {
                    ':inc': 1,
                    ':timestamp': datetime.datetime.now(datetime.UTC).isoformat()
                }
            
            response = aggregates_table.update_item(
                Key={
                    'pk': f'BATCH#{batch_id}',
                    'Event': workflow_stage
                },
                UpdateExpression=update_expression,
                ExpressionAttributeValues=expression_values,
                ReturnValues='ALL_NEW'
            )
            
            # Derive Count_Completed_Entities from processed_entities set size and persist
            updated_item = response.get('Attributes', {})
            processed_set = updated_item.get('processed_entities', set())
            count_completed = len(processed_set)
            updated_item['Count_Completed_Entities'] = count_completed
            
            # Write back the derived count
            aggregates_table.update_item(
                Key={'pk': f'BATCH#{batch_id}', 'Event': workflow_stage},
                UpdateExpression='SET Count_Completed_Entities = :count',
                ExpressionAttributeValues={':count': count_completed}
            )
            
            # Only check batch readiness for GatherSampleEvidence
            check_batch_readiness_after_update(batch_id, updated_item, sample_id, status, retry_count)
            
        else:
            # All other workflows: Store status, output_uri and timestamp
            if status == 'COMPLETED' and run_output and run_output.strip():
                aggregates_table.update_item(
                    Key={
                        'pk': f'BATCH#{batch_id}',
                        'Event': workflow_stage
                    },
                    UpdateExpression='SET #s = :status, last_updated = :timestamp, output_uri = :output_uri',
                    ExpressionAttributeNames={'#s': 'Status'},
                    ExpressionAttributeValues={
                        ':status': status,
                        ':timestamp': datetime.datetime.now(datetime.UTC).isoformat(),
                        ':output_uri': run_output.strip()
                    }
                )
                
                # FilterBatch: update processed_entities to post-filter sample list
                if workflow_stage == 'FilterBatchSamples':
                    update_filterbatch_processed_entities(batch_id, run_output.strip())
            else:
                aggregates_table.update_item(
                    Key={
                        'pk': f'BATCH#{batch_id}',
                        'Event': workflow_stage
                    },
                    UpdateExpression='SET #s = :status, last_updated = :timestamp',
                    ExpressionAttributeNames={'#s': 'Status'},
                    ExpressionAttributeValues={
                        ':status': status,
                        ':timestamp': datetime.datetime.now(datetime.UTC).isoformat()
                    }
                )
        
    except Exception as e:
        print(f"Error updating batch record: {e}")


def check_batch_readiness_after_update(batch_id, batch_item, sample_id, status, retry_count):
    """Check if batch is ready after counter update and trigger batch checker"""
    
    # Extract total from Info, but counts from top-level where update_item put them
    total_samples = int(batch_item.get('Count_Total_Entities', 0))
    completed_samples = int(batch_item.get('Count_Completed_Entities', 0))
    failed_samples = int(batch_item.get('Count_Failed_Entities', 0))
    processed_entities = batch_item.get('processed_entities', {})
    
    print(f"Batch {batch_id}: {completed_samples} completed, {failed_samples} failed, {total_samples} total")
    
    # Check if all samples are processed (completed + failed = total)
    if completed_samples + failed_samples >= total_samples:
        print(f"Batch {batch_id}: All samples processed, triggering batch checker")
        trigger_batch_checker(batch_id, completed_samples, failed_samples, total_samples, processed_entities)


def trigger_batch_checker(batch_id, completed_samples, failed_samples, total_samples, processed_entities):
    """Trigger batch checker via SNS"""
    
    try:
        message = {
            'batch_id': batch_id,
            'Count_Completed_Entities': completed_samples,
            'Count_Failed_Entities': failed_samples,
            'Count_Total_Entities': total_samples,
            'processed_entities': list(processed_entities),  # Convert set to list for JSON serialization
            'action': 'verify_and_trigger_evidenceqc'
        }
        print(f"Message to batch_checker: {json.dumps(message)}")
        
        response = sns.publish(
            TopicArn=BATCH_CHECKER_SNS_ARN,
            Message=json.dumps(message),
            Subject=f'Batch Ready: {batch_id}'
        )
        
        print(f"✓ Triggered batch checker for {batch_id} (MessageId: {response['MessageId']})")
        
    except Exception as e:
        print(f"✗ Error triggering batch checker: {str(e)}")


def update_filterbatch_processed_entities(batch_id, output_uri):
    """After FilterBatch completes, update processed_entities to only include passing samples."""
    try:
        prefix = f"{output_uri.rstrip('/')}/out/batch_samples_postOutlierExclusion_file/"
        without_scheme = prefix.replace('s3://', '')
        bucket = without_scheme.split('/')[0]
        key_prefix = without_scheme.split('/', 1)[1]
        
        # Find the sample list file
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=key_prefix)
        files = [obj['Key'] for obj in resp.get('Contents', []) if obj['Key'].endswith('.list')]
        if not files:
            print(f"WARNING: No .list file in {prefix}")
            return
        
        # Download and parse sample IDs
        body = s3.get_object(Bucket=bucket, Key=files[0])['Body'].read().decode('utf-8')
        passing_samples = set(line.strip() for line in body.strip().split('\n') if line.strip())
        
        # Update processed_entities on FilterBatch record
        aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
        aggregates_table.update_item(
            Key={'pk': f'BATCH#{batch_id}', 'Event': 'FilterBatchSamples'},
            UpdateExpression='SET processed_entities = :pe',
            ExpressionAttributeValues={':pe': passing_samples}
        )
        print(f"✓ FilterBatch {batch_id}: processed_entities updated to {len(passing_samples)} passing samples")
    except Exception as e:
        print(f"WARNING: Failed to update FilterBatch processed_entities for {batch_id}: {e}")


def collate_cohort_ped_file(cohort_id, batch_ids):
    """Concatenate per-batch ped files into a single cohort ped file.
    Keeps header from first file, deduplicates by sample ID (col 2)."""
    aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
    
    # Collect ped_file paths from batch records
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
    
    # Download and concatenate
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
    
    # Build cohort ped content
    content = ''
    if header:
        content = header + '\n'
    content += '\n'.join(lines) + '\n'
    
    # Upload to S3
    cohort_ped_key = f"qc_outputs/evidence_qc/batching/ped_files/{cohort_id}.ped"
    s3.put_object(Bucket=OUTPUT_BUCKET, Key=cohort_ped_key, Body=content)
    cohort_ped_uri = f"s3://{OUTPUT_BUCKET}/{cohort_ped_key}"
    print(f"✓ Cohort ped file: {cohort_ped_uri} ({len(lines)} samples from {len(ped_paths)} batches)")
    return cohort_ped_uri


def update_cohort_counter(batch_id, status):
    """
    When FilterBatch completes for a batch:
    1. Find the COHORT#FilterBatch entry where batch_id is in Info.entity_member
    2. Add batch_id to processed_entities, increment counter
    3. Verify all Info.entity_member are in processed_entities before triggering MergeBatchSites
    """
    aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
    
    # Find the COHORT entry tracking FilterBatch (~200 items max, scan is fine)
    scan_resp = aggregates_table.scan(
        FilterExpression='begins_with(pk, :prefix) AND #evt = :evt',
        ExpressionAttributeNames={'#evt': 'Event'},
        ExpressionAttributeValues={':prefix': 'COHORT#', ':evt': 'FilterBatchSamples'}
    )
    # Match on Info.entity_member containing this batch_id
    items = [i for i in scan_resp.get('Items', []) 
             if batch_id in i.get('Info', {}).get('entity_member', [])]
    
    if not items:
        print(f"No COHORT FilterBatch entry with batch {batch_id} in Info.entity_member")
        return
    
    cohort_item = items[0]
    cohort_pk = cohort_item['pk']
    entity_member = cohort_item.get('Info', {}).get('entity_member', [])
    
    if status == 'COMPLETED':
        resp = aggregates_table.update_item(
            Key={'pk': cohort_pk, 'Event': 'FilterBatchSamples'},
            UpdateExpression='ADD processed_entities :batch_set SET last_updated = :ts',
            ExpressionAttributeValues={
                ':batch_set': {batch_id},
                ':ts': datetime.datetime.now(datetime.UTC).isoformat()
            },
            ReturnValues='ALL_NEW'
        )
    else:
        resp = aggregates_table.update_item(
            Key={'pk': cohort_pk, 'Event': 'FilterBatchSamples'},
            UpdateExpression='ADD Count_Failed_Entities :inc SET last_updated = :ts',
            ExpressionAttributeValues={
                ':inc': 1,
                ':ts': datetime.datetime.now(datetime.UTC).isoformat()
            },
            ReturnValues='ALL_NEW'
        )
    
    updated = resp.get('Attributes', {})
    processed = updated.get('processed_entities', set())
    completed = len(processed)
    failed = int(updated.get('Count_Failed_Entities', 0))
    
    print(f"Cohort {cohort_pk}: FilterBatch {completed} completed, {failed} failed, {len(processed)}/{len(entity_member)} processed")
    
    # Verify ALL entity_member are in processed_entities before triggering
    all_members_processed = set(entity_member).issubset(processed)
    if all_members_processed and completed > 0:
        cohort_id = cohort_pk.replace('COHORT#', '')
        
        # Collate per-batch ped files into cohort-level ped file
        cohort_ped_file = collate_cohort_ped_file(cohort_id, entity_member)
        if cohort_ped_file:
            aggregates_table.update_item(
                Key={'pk': cohort_pk, 'Event': 'FilterBatchSamples'},
                UpdateExpression='SET Info.ped_file = :pf',
                ExpressionAttributeValues={':pf': cohort_ped_file}
            )
            # Re-read updated record for trigger_cohort_workflow
            resp2 = aggregates_table.get_item(Key={'pk': cohort_pk, 'Event': 'FilterBatchSamples'})
            updated = resp2.get('Item', updated)
        
        print(f"All {len(entity_member)} batches verified in processed_entities — triggering MergeBatchSites")
        trigger_cohort_workflow(cohort_id, 'MergeBatchSites', updated)


def trigger_next_batch_stage(batch_id, next_stage, completed_stage):
    """Publish PENDING message for next batch workflow stage to aggregates SNS → DDB → submit_batch_workflow"""
    
    # Get processed_entities and Count_Total_Entities from the completed stage record
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
    
    # Get batch_ids from Info.entity_member (authoritative list of batches in cohort)
    info = cohort_record.get('Info', {})
    batch_ids = sorted(list(info.get('entity_member', [])))
    
    # Fallback to processed_entities if entity_member not set
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


def get_batch_definition(batch_id):
    """Get batch definition from aggregates table"""
    try:
        aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
        # FIX: Use query for composite key table
        response = aggregates_table.query(
            KeyConditionExpression='pk = :pk',
            ExpressionAttributeValues={':pk': f'BATCH#{batch_id}'},
            Limit=1
        )
        items = response.get('Items', [])
        return items[0] if items else None
    except Exception as e:
        print(f"Error getting batch definition for {batch_id}: {e}")
        return None
