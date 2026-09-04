import json
import boto3
import os
import datetime
import time
import subprocess
from botocore.exceptions import ClientError
from parameter_builder import build_parameters
from config_loader import get_workflow_id

omics = boto3.client('omics')
sns = boto3.client('sns')
s3 = boto3.client('s3')
scheduler = boto3.client('scheduler')
dynamodb = boto3.resource('dynamodb')

cache_id = os.environ.get('HEALTHOMICS_CACHE_ID', '2758152')
cache_strategy = "CACHE_ALWAYS"

# Retry configuration
MAX_RETRIES = 5
INITIAL_BACKOFF = 1800  # 30 minutes
MAX_BACKOFF = 14400     # 4 hours

# Environment variables
OUTPUT_BUCKET = os.environ.get('OUTPUT_BUCKET', '<your-bucket>')
OUTPUT_PREFIX = os.environ.get('OUTPUT_PREFIX', 'healthomics_test/test_output')
PARAMETER_PREFIX = os.environ.get('PARAMETER_PREFIX', 'healthomics_input')
HEALTHOMICS_ROLE_ARN = os.environ.get('HEALTHOMICS_ROLE_ARN', '')
AWS_REGION = os.environ.get('AWS_REGION', 'ap-southeast-1')
AGGREGATES_TABLE_NAME = os.environ.get('AGGREGATES_TABLE_NAME', 'gatk-sv-dev-batch-cohort')
SNS_AGGREGATES_ARN = os.environ.get('SNS_AGGREGATES_ARN', '')
SCHEDULER_ROLE_ARN = os.environ.get('SCHEDULER_ROLE_ARN', '')
COST_TAG = os.environ.get('COST_TAG', 'gatk-sv')
OWNER = os.environ.get('OWNER', 'unknown')
ENV = os.environ.get('ENV', 'dev')
PARAMETER_SIZE_LIMIT = int(os.environ.get('HEALTHOMICS_PARAMETER_SIZE_LIMIT', '45000'))

# Aliases for backward compat
S3_PARAMETER_BUCKET = OUTPUT_BUCKET
PARAMETER_FILE_PREFIX = PARAMETER_PREFIX

GATKSV_STAGE_S3_PARAMETER = {
    'EvidenceQC': ['counts', 'dragen_vcfs', 'wham_vcfs', 'scramble_vcfs', 'manta_vcfs', 'melt_vcfs'],
    'TrainGCNV': ['count_files'],
    'GatherBatchEvidence': ['PE_files', 'SR_files', 'SD_files', 'counts', 'dragen_vcfs', 'wham_vcfs', 'scramble_vcfs', 'manta_vcfs', 'melt_vcfs', 'gcnv_model_tars'],
    'ClusterBatch': [],
    'GenerateBatchMetrics': [],
    'FilterBatchSites': [],
    'FilterBatchSamples': []
}

def estimate_parameter_size(parameters):
    """Estimate JSON parameter size in bytes"""
    return len(json.dumps(parameters, separators=(',', ':')))

def upload_parameters_to_s3(batch_id, parameters, workflow_stage):
    """Upload large parameter arrays to S3 with order preservation validation"""
    
    s3_prefix = f"{PARAMETER_FILE_PREFIX}/{workflow_stage}/{batch_id}/"
    uploaded_files = {}
    
    # Define which parameters to upload to S3 (large arrays)
    s3_parameters = GATKSV_STAGE_S3_PARAMETER[workflow_stage]
    
    # Validate sample list exists and get expected order
    sample_key = 'sample_ids' if 'sample_ids' in parameters else 'samples' if 'samples' in parameters else None
    if not sample_key:
        raise ValueError("sample_ids or samples parameter is required for order validation")
    
    expected_length = len(parameters[sample_key])
    print(f"Order validation: Expected sample count = {expected_length} (key: {sample_key})")
    
    # Arrays that are NOT per-sample (e.g. one per gCNV scatter shard)
    NON_SAMPLE_ARRAYS = {'gcnv_model_tars'}
    
    for param_name in s3_parameters:
        if param_name in parameters and isinstance(parameters[param_name], list):
            param_list = parameters[param_name]
            
            # Validate length matches sample list for per-sample file arrays (critical for GATK-SV)
            if param_name not in ('sample_ids', 'samples') and param_name not in NON_SAMPLE_ARRAYS and len(param_list) != expected_length:
                raise ValueError(f"GATK-SV order error: {param_name} length ({len(param_list)}) doesn't match sample_ids length ({expected_length})")
            
            # Upload to S3 with explicit order preservation
            s3_key = f"{s3_prefix}{param_name}.json"
            s3_url = f"s3://{S3_PARAMETER_BUCKET}/{s3_key}"
            
            # Use consistent JSON formatting to preserve order
            json_content = json.dumps(param_list, separators=(',', ':'), ensure_ascii=False)
            
            s3.put_object(
                Bucket=S3_PARAMETER_BUCKET,
                Key=s3_key,
                Body=json_content,
                ContentType='application/json',
                Metadata={
                    'original_length': str(len(param_list)),
                    'batch_id': batch_id,
                    'parameter_name': param_name,
                    'workflow_stage': workflow_stage
                }
            )
            
            uploaded_files[f"{param_name}_file"] = s3_url
            print(f"✓ Uploaded {param_name}: {len(param_list)} items to {s3_url} (order preserved)")
    
    print(f"✓ Order validation passed: All {len([p for p in s3_parameters if p in parameters])} parameter arrays aligned with {expected_length} samples")
    return uploaded_files

def submit_workflow_with_hybrid_approach(batch_id, workflow_stage, parameters, retry_count=0):
    """Submit workflow via AWS CLI to preserve exact JSON serialization"""
    
    workflow_id, workflow_version = get_workflow_id(workflow_stage)
    
    estimated_size = estimate_parameter_size(parameters)
    print(f"Estimated parameter size: {estimated_size} bytes")
    
    if estimated_size > PARAMETER_SIZE_LIMIT:
        print(f"Large batch detected ({estimated_size} bytes), uploading parameters to S3 for audit")
        upload_parameters_to_s3(batch_id, parameters, workflow_stage)
    
    run_name = f"{ENV}-{workflow_stage}-{batch_id}-r{retry_count}"
    output_uri = f"s3://{OUTPUT_BUCKET}/{OUTPUT_PREFIX}/{workflow_stage}/{batch_id}"
    
    sample_key = 'sample_ids' if 'sample_ids' in parameters else 'samples'
    tags = {
        'Project': COST_TAG,
        'Owner': OWNER,
        'environment': ENV,
        'batch_id': batch_id,
        'workflow_stage': workflow_stage,
        'retry_count': str(retry_count),
        'sample_count': str(len(parameters.get(sample_key, [])))
    }
    
    # Write params to /tmp — file:// preserves exact JSON (no float mangling)
    params_file = '/tmp/params.json'
    with open(params_file, 'w') as f:
        json.dump(parameters, f)
    
    # Upload to S3 for verification
    s3_key = f"{PARAMETER_PREFIX}/{workflow_stage}/{batch_id}/params_r{retry_count}.json"
    s3.upload_file(params_file, OUTPUT_BUCKET, s3_key)
    print(f"Params uploaded to s3://{OUTPUT_BUCKET}/{s3_key}")
    
    # AWS CLI from Lambda layer at /opt/bin/aws
    aws_bin = '/opt/bin/aws'
    cmd = [
        aws_bin, 'omics', 'start-run',
        '--region', AWS_REGION,
        '--workflow-id', workflow_id,
        '--role-arn', HEALTHOMICS_ROLE_ARN,
        '--name', run_name,
        '--output-uri', output_uri,
        '--parameters', f'file://{params_file}',
        '--tags', json.dumps(tags),
        '--cache-id', cache_id,
        '--cache-behavior', cache_strategy,
        '--storage-type', 'DYNAMIC',
    ]
    if workflow_version:
        cmd.extend(['--workflow-version-name', workflow_version])
    
    print(f"CLI command: {' '.join(cmd)}")
    
    env = os.environ.copy()
    env['PATH'] = f"/opt/bin:{env.get('PATH', '')}"
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    
    if result.returncode != 0:
        error_msg = result.stderr.strip()
        print(f"CLI error: {error_msg}")
        if 'ThrottlingException' in error_msg or 'ServiceQuotaExceededException' in error_msg or 'TooManyRequestsException' in error_msg:
            raise ClientError(
                {'Error': {'Code': 'ThrottlingException', 'Message': error_msg}},
                'StartRun'
            )
        raise ClientError(
            {'Error': {'Code': 'ValidationException', 'Message': error_msg}},
            'StartRun'
        )
    
    response = json.loads(result.stdout)
    return response

def lambda_handler(event, context):
    """
    Submit batch-level GATK-SV workflows to HealthOmics
    Handles: EvidenceQC, TrainGCNV, GatherBatchEvidence, ClusterBatch, 
             GenerateBatchMetrics, FilterBatchSites, FilterBatchSamples, GenotypeBatch, MergeBatchSites
    """
    
    print(f"Event: {json.dumps(event)}")
    
    detail = event['detail']
    new_image = detail['dynamodb']['NewImage']
    
    pk = new_image['pk']['S']
    batch_id = pk.removeprefix('BATCH#')
    workflow_stage = new_image['Event']['S']
    
    # Idempotency guard: skip if already STARTED or COMPLETED
    agg_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
    existing = agg_table.get_item(Key={'pk': pk, 'Event': workflow_stage})
    if 'Item' in existing:
        existing_status = existing['Item'].get('Status', '')
        if existing_status in ('STARTED', 'COMPLETED'):
            print(f"Idempotency guard: {workflow_stage} for {batch_id} already {existing_status}, skipping")
            return {'statusCode': 200, 'body': f'Already {existing_status}'}
    
    total_samples = int(new_image['Count_Total_Entities']['N'])
    retry_count = int(new_image.get('retry_count', {}).get('N', '0'))
    
    # Extract processed_entities from incoming DDB stream event
    processed_entities = list(new_image.get('processed_entities', {}).get('SS', []))
    
    # Get sample list - try Info field first, fallback to DynamoDB query
    ddb_data = get_sample_list_smart(new_image, batch_id, workflow_stage)
    
    # Build run config — optional_params defaults come from the S3/layer template
    run_config = {
        'batch_id': batch_id,
    }
    
    # Extract ped_file from Info field (stored as Map in DDB stream)
    info_map = new_image.get('Info', {}).get('M', {})
    ped_file = info_map.get('ped_file', {}).get('S', '')
    if ped_file:
        run_config['ped_file'] = ped_file
        print(f"Using ped_file: {ped_file}")
    
    # Build parameters using template
    try:
        parameters = build_parameters(workflow_stage, run_config, ddb_data)
    except Exception as e:
        print(f"Error building parameters: {str(e)}")
        raise
    
    # Submit workflow using hybrid approach with retry logic
    try:
        response = submit_workflow_with_hybrid_approach(batch_id, workflow_stage, parameters, retry_count)
        
        run_id = response['id']
        run_arn = response['arn']
        
        print(f"Started {workflow_stage} run {run_id} for batch {batch_id} ({len(ddb_data)} samples)")
        
        sns.publish(
            TopicArn=SNS_AGGREGATES_ARN,
            Message=json.dumps({
                'pk': f"BATCH#{batch_id}",
                'Event': workflow_stage,
                'Status': 'STARTED',
                'Count_Total_Entities': total_samples,
                'processed_entities': processed_entities,
                'Info': {
                    'run_id': run_id,
                    'run_arn': run_arn,
                    'retry_count': retry_count,
                    'started_at': datetime.datetime.now(datetime.UTC).isoformat(),
                    'ped_file': run_config.get('ped_file', '')
                }
            }),
            MessageGroupId=batch_id,
            MessageDeduplicationId=f"{batch_id}-{workflow_stage}-{run_id}"
        )
        
        return {'statusCode': 200, 'body': json.dumps({'run_id': run_id})}
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        
        if error_code in ('ThrottlingException', 'ServiceQuotaExceededException', 'TooManyRequestsException'):
            # Wait for HealthOmics to register the run, then check if it was accepted despite the throttle
            run_name = f"{ENV}-{workflow_stage}-{batch_id}-r{retry_count}"
            time.sleep(10)
            existing = omics.list_runs(name=run_name, maxResults=1)
            if existing.get('items', []):
                existing_status = existing['items'][0].get('status', '')
                if existing_status in ('PENDING', 'STARTING', 'RUNNING', 'COMPLETED'):
                    print(f"Run {run_name} already exists with status {existing_status}, skipping retry")
                    return {'statusCode': 200, 'body': f'Run already {existing_status}'}
            
            if retry_count < MAX_RETRIES:
                backoff = min(INITIAL_BACKOFF * (2 ** retry_count), MAX_BACKOFF)
                print(f"Throttled. Retry {retry_count + 1}/{MAX_RETRIES} after {backoff}s")
                
                schedule_retry(batch_id, workflow_stage, retry_count + 1, backoff,
                               total_samples, processed_entities)
                
                return {
                    'statusCode': 429,
                    'body': json.dumps({
                        'message': 'Throttled, retry scheduled',
                        'retry_count': retry_count + 1,
                        'backoff_seconds': backoff
                    })
                }
            else:
                print(f"Max retries ({MAX_RETRIES}) exceeded for batch {batch_id}")
                publish_failure(batch_id, workflow_stage, f"Max retries exceeded: {str(e)}",
                                total_samples, processed_entities)
                raise
        else:
            print(f"Error: {str(e)}")
            publish_failure(batch_id, workflow_stage, str(e),
                            total_samples, processed_entities)
            raise
    
    except Exception as e:
        print(f"Error: {str(e)}")
        publish_failure(batch_id, workflow_stage, str(e),
                        total_samples, processed_entities)
        raise


def schedule_retry(batch_id, workflow_stage, retry_count, backoff_seconds, total_samples, processed_entities):
    """Schedule retry by flipping Status back to PENDING in aggregates DDB after a delay"""
    
    now = datetime.datetime.now(datetime.UTC)
    retry_time = now + datetime.timedelta(seconds=backoff_seconds)
    schedule_name = f"retry-{batch_id}-{workflow_stage}-r{retry_count}"
    schedule_time = retry_time.strftime('%Y-%m-%dT%H:%M:%S')
    
    # The scheduled event updates the aggregates DDB record to PENDING,
    # which fires the DDB stream → EventBridge → submit_batch_workflow again
    aggregates_table_name = AGGREGATES_TABLE_NAME
    
    # Publish RETRY_SCHEDULED status to aggregates
    sns.publish(
        TopicArn=SNS_AGGREGATES_ARN,
        Message=json.dumps({
            'pk': f"BATCH#{batch_id}",
            'Event': workflow_stage,
            'Status': 'RETRY_SCHEDULED',
            'Count_Total_Entities': total_samples,
            'processed_entities': processed_entities,
            'Info': {
                'retry_count': retry_count,
                'next_retry_at': retry_time.isoformat()
            }
        }),
        MessageGroupId=batch_id,
        MessageDeduplicationId=f"{batch_id}-{workflow_stage}-retry-{retry_count}"
    )
    
    # Schedule a DDB update to flip Status back to PENDING after backoff
    # EventBridge Scheduler → SNS → event handler → DDB update → stream → this Lambda
    retry_message = {
        'pk': f"BATCH#{batch_id}",
        'Event': workflow_stage,
        'Status': 'PENDING',
        'Count_Total_Entities': total_samples,
        'processed_entities': processed_entities,
        'Info': {
            'retry_count': retry_count,
            'scheduled_at': schedule_time
        }
    }
    
    try:
        scheduler.create_schedule(
            Name=schedule_name,
            ScheduleExpression=f"at({schedule_time})",
            FlexibleTimeWindow={'Mode': 'OFF'},
            Target={
                'Arn': SNS_AGGREGATES_ARN,
                'RoleArn': SCHEDULER_ROLE_ARN,
                'Input': json.dumps(retry_message)
            },
            State='ENABLED'
        )
        print(f"Scheduled retry for batch {batch_id} at {schedule_time}")
    except scheduler.exceptions.ConflictException:
        print(f"Schedule {schedule_name} already exists")


def publish_failure(batch_id, workflow_stage, error_message, total_samples, processed_entities):
    """Update batch record with error, preserving existing Info fields"""
    aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
    aggregates_table.update_item(
        Key={'pk': f'BATCH#{batch_id}', 'Event': workflow_stage},
        UpdateExpression='SET #st = :status, Info.error_message = :err, last_updated = :ts',
        ExpressionAttributeNames={'#st': 'Status'},
        ExpressionAttributeValues={
            ':status': 'FAILED',
            ':err': str(error_message),
            ':ts': datetime.datetime.now(datetime.UTC).isoformat()
        }
    )


def get_sample_list_smart(new_image, batch_id, workflow_stage):
    """
    Get sample list using smart approach:
    1. Try processed_entities (DynamoDB Set, most reliable)
    2. Try Info.sample_list (fast, no DynamoDB query)
    3. Try Info.batch_definition_key (single DynamoDB query)
    4. Fallback to get_batch_samples (GSI query)
    """
    
    # Option 1: Use processed_entities (DynamoDB Set) - most reliable
    if 'processed_entities' in new_image:
        processed_entities_set = new_image['processed_entities'].get('SS', [])
        if processed_entities_set:
            print(f"Using processed_entities: {len(processed_entities_set)} samples")
            return [{'sample_id': sid} for sid in processed_entities_set]
    
    # Option 2: Try to get sample list from Info field
    info_field = new_image.get('Info', {}).get('S', '{}')
    try:
        info = json.loads(info_field)
        
        # Direct sample list
        if 'sample_list' in info:
            sample_list = info['sample_list']
            print(f"Using sample_list from Info: {len(sample_list)} samples")
            return [{'sample_id': sid} for sid in sample_list]
        
        # Batch definition reference
        elif 'batch_definition_key' in info:
            batch_def_key = info['batch_definition_key']
            batch_def = get_batch_definition_by_key(batch_def_key)
            if batch_def and 'entity_member' in batch_def.get('Info', {}):
                sample_list = batch_def['Info']['entity_member']
                print(f"Using batch_definition_key {batch_def_key}: {len(sample_list)} samples")
                return [{'sample_id': sid} for sid in sample_list]
        
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Error parsing Info field: {e}")
    
    # Fallback to DynamoDB GSI query
    print(f"Fallback to DynamoDB query for batch {batch_id}")
    previous_stage = get_previous_stage(workflow_stage)
    return get_batch_samples(batch_id, previous_stage)


def get_batch_definition_by_key(batch_def_key):
    """Get batch definition from aggregates table by key"""
    try:
        aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
        # FIX: Use query for composite key table
        response = aggregates_table.query(
            KeyConditionExpression='pk = :pk',
            ExpressionAttributeValues={':pk': batch_def_key},
            Limit=1
        )
        items = response.get('Items', [])
        return items[0] if items else None
    except Exception as e:
        print(f"Error getting batch definition {batch_def_key}: {e}")
        return None


def get_batch_samples(batch_id, workflow_stage):
    """
    Get batch samples from batch definition - NO GSI NEEDED
    Uses batch definition from aggregates table (much more efficient)
    """
    
    # Get batch definition which contains sample list
    batch_def = get_batch_definition_by_key(f'BATCH#{batch_id}')
    if not batch_def or 'Info' not in batch_def:
        print(f"No batch definition found for {batch_id}")
        return []
    
    sample_list = batch_def['Info'].get('entity_member', [])
    print(f"Found {len(sample_list)} samples in batch definition for {batch_id}")
    
    # Convert to expected format for parameter builder
    items = []
    for sample_id in sample_list:
        items.append({
            'sample_id': sample_id,
            'pk': f'SAMPLE#{sample_id}',
            'Event': workflow_stage,
            'Status': 'COMPLETED'  # Assume completed since batch is ready
        })
    
    # Sort by sample_id for consistent order
    items.sort(key=lambda x: x['sample_id'])
    
    return items


def get_previous_stage(workflow_stage):
    """Get the previous workflow stage"""
    stage_order = {
        'EvidenceQC': 'GatherSampleEvidence',
        'TrainGCNV': 'GatherSampleEvidence',
        'GatherBatchEvidence': 'EvidenceQC',
        'ClusterBatch': 'GatherBatchEvidence',
        'GenerateBatchMetrics': 'ClusterBatch',
        'FilterBatchSites': 'GenerateBatchMetrics',
        'FilterBatchSamples': 'FilterBatchSites',
        'GenotypeBatch': 'FilterBatchSamples',
        'MergeBatchSites': 'GenotypeBatch'
    }
    return stage_order.get(workflow_stage, 'GatherSampleEvidence')
