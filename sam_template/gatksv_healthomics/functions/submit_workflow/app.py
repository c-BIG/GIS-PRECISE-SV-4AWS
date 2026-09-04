import json
import boto3
import os
import time
import random
import datetime
from botocore.exceptions import ClientError
from parameter_builder import build_parameters
from config_loader import get_workflow_id, get_docker_images

# Environment variables
PROJECT = os.environ.get('COST_TAG', 'sg100k-sv-gatksv')
OWNER = os.environ.get('OWNER', 'precise')
ENV = os.environ.get('ENV', 'dev')
HEALTHOMICS_ROLE_ARN = os.environ.get('HEALTHOMICS_ROLE_ARN', '')
SNS_TOPIC_ARN = os.environ.get('SNS_TOPIC_ARN', '')
SCHEDULER_ROLE_ARN = os.environ.get('SCHEDULER_ROLE_ARN', '')
OUTPUT_BUCKET = os.environ.get('OUTPUT_BUCKET', '<your-bucket>')
OUTPUT_PREFIX = os.environ.get('OUTPUT_PREFIX', 'healthomics_test/test_output')
PARAMETER_PREFIX = os.environ.get('PARAMETER_PREFIX', 'healthomics_input')

omics = boto3.client('omics')
sns = boto3.client('sns')
scheduler = boto3.client('scheduler')
s3 = boto3.client('s3')

ROLE = HEALTHOMICS_ROLE_ARN  # backward compat
DOCKER_IMAGES = get_docker_images()
print(f"Using ROLE : {ROLE}")

cache_id = os.environ.get('HEALTHOMICS_CACHE_ID', '1262595')

# Retry configuration
MAX_RETRIES = 5
INITIAL_BACKOFF = 1800  # 30 minutes
MAX_BACKOFF = 14400     # 4 hours


def resolve_melt_preprocess(s3_uri, sample_id):
    """
    Resolve melt_preprocess_s3_uri prefix into individual S3 file paths.
    Lists the prefix, finds .disc, .disc.bai, .fq files matching sample_id.
    Returns dict with keys disc, disc_index, fq — or None if incomplete.
    """
    s3_uri = s3_uri.rstrip()
    s3_uri = s3_uri.rstrip('/')
    # Parse bucket and prefix from s3://bucket/prefix
    parts = s3_uri.replace('s3://', '').split('/', 1)
    bucket = parts[0]
    prefix = parts[1] if len(parts) > 1 else ''

    try:
        response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix + '/')
        if 'Contents' not in response:
            print(f"No objects found at {s3_uri}/")
            return None

        files = {}
        for obj in response['Contents']:
            key = obj['Key']
            filename = key.split('/')[-1]
            # Match files containing sample_id
            if sample_id not in filename:
                continue
            if filename.endswith('.disc.bai'):
                files['disc_index'] = f"s3://{bucket}/{key}"
            elif filename.endswith('.disc'):
                files['disc'] = f"s3://{bucket}/{key}"
            elif filename.endswith('.fq'):
                files['fq'] = f"s3://{bucket}/{key}"

        if all(k in files for k in ('disc', 'disc_index', 'fq')):
            print(f"MELT preprocess files resolved: {files}")
            return files
        else:
            print(f"MELT preprocess files incomplete at {s3_uri}/: found {list(files.keys())}")
            return None
    except Exception as e:
        print(f"Error resolving MELT preprocess from {s3_uri}: {e}")
        return None


def lambda_handler(event, context):
    """
    Submit sample-level GATK-SV workflows to HealthOmics with exponential backoff retry
    Ensures exactly-once processing per sample/workflow stage
    """
    
    # Random jitter (0-30s) to spread concurrent submissions and avoid HealthOmics API throttle
    jitter = random.uniform(0, 30)
    print(f"Jitter: {jitter:.1f}s")
    time.sleep(jitter)
    
    print(f"Event: {json.dumps(event)}")
    
    detail = event['detail']
    new_image = detail['dynamodb']['NewImage']
        
    pk = new_image['pk']['S']
    sample_id = pk.removeprefix('SAMPLE#')
    current_status = new_image.get('Status', {}).get('S', 'UNKNOWN')
    
    workflow_event = new_image['Event']['S']
    
    # 1. Access the top-level 'Info' Map ('M')
    info_map = new_image.get('Info', {}).get('M', {})

    # 2. Extract values from the nested Info map
    # Note: 'cram_path' is inside 'Info', not 'input_files'
    gender = info_map.get('gender', {}).get('S', 'unknown')
    batch_id = info_map.get('batch_id_initial', {}).get('S', 'UNKNOWN')
    run_id = info_map.get('run_id', {}).get('S', '')
    retry_count = int(info_map.get('retry_count', {}).get('N', '0'))
    #retry_count = int(new_image.get('retry_count', {}).get('N', '0'))
    #run_id = new_image.get('run_id', {}).get('S', '')
    
    cram_path = info_map.get('cram_path', {}).get('S', '')
    crai_path = info_map.get('cram_index_path', {}).get('S', '')

    # 3. Optional: Access other fields for your run_config if needed
    dragen_vcf = info_map.get('dragen_vcf', {}).get('S', '')
    dragen_vcf_index = info_map.get('dragen_vcf_index', {}).get('S', '')
    manta_vcf = info_map.get('manta_vcf', {}).get('S', '')
    manta_vcf_index = info_map.get('manta_vcf_index', {}).get('S', '')
    dragen_wgs_coverage_metrics = info_map.get('dragen_wgs_coverage_metrics', {}).get('S', '')
    melt_preprocess_s3_uri = info_map.get('melt_preprocess_s3_uri', {}).get('S', '')
    
    # Obsolete code
    # Handle nested input_files structure
    # input_files = new_image.get('input_files', {}).get('M', {})
    # cram_path = input_files.get('cram', {}).get('S', '') if input_files else new_image.get('cram_s3_path', {}).get('S', '')
     
    # Build run config — optional_params defaults come from the S3/layer template
    run_config = {
        'sample_id': sample_id,
        'cram_path': cram_path,
        'cram_index_path': crai_path,
    }
    # Insert optional file parameter ie dragen_vcf
    if dragen_vcf:
        run_config['dragen_vcf'] = dragen_vcf
        run_config['dragen_bnd2inv_docker'] = DOCKER_IMAGES.get('dragen_bnd2inv_docker', '')
    if dragen_vcf_index:
        run_config['dragen_vcf_index'] = dragen_vcf_index
    if manta_vcf:
        run_config['manta_vcf'] = manta_vcf
    if manta_vcf_index:
        run_config['manta_vcf_index'] = manta_vcf_index
    if dragen_wgs_coverage_metrics:
        run_config['dragen_wgs_coverage_metrics'] = dragen_wgs_coverage_metrics
    # Resolve melt_preprocess_s3_uri prefix into individual file paths
    if melt_preprocess_s3_uri:
        preprocess_files = resolve_melt_preprocess(melt_preprocess_s3_uri, sample_id)
        if preprocess_files:
            run_config['melt_preprocess_disc'] = preprocess_files['disc']
            run_config['melt_preprocess_disc_index'] = preprocess_files['disc_index']
            run_config['melt_preprocess_fq'] = preprocess_files['fq']
   
    # Build parameters using template
    try:
        parameters = build_parameters(workflow_event, run_config)
    except Exception as e:
        print(f"Error building parameters: {str(e)}")
        raise
    
    # Get workflow ID
    workflow_id, workflow_version = get_workflow_id(workflow_event)
    
    print(f"json: {parameters} ")
    
    # Submit to HealthOmics with retry logic
    try:
        response = submit_with_retry(
            workflow_id=workflow_id,
            workflow_version=workflow_version,
            workflow_stage=workflow_event,
            sample_id=sample_id,
            batch_id=batch_id,
            gender=gender,
            parameters=parameters,
            retry_count=retry_count
        )
        
        run_id = response['id']
        run_arn = response['arn']
        
        print(f"Started {workflow_event} run {run_id} for {sample_id}")
        
        # Publish status update
        # timestamp = datetime.utcnow().isoformat() + 'Z'
        timestamp = datetime.datetime.now(datetime.UTC).isoformat()
        response_sns = sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Message=json.dumps({
                'SampleId': sample_id,
                'Event': workflow_event,
                'Status': "STARTED",
                'Timestamp': timestamp,
                'Info': {
                    'run_id': run_id,
                    'run_arn': run_arn,
                    'retry_count': retry_count,
                    'batch_id_initial': batch_id
                }
            }),
            MessageGroupId=sample_id,
            MessageDeduplicationId=f"{sample_id}-{workflow_event}-{run_id}"
        )
        
        print(f"response_sns: {response_sns}")
        return {'statusCode': 200, 'body': json.dumps({'run_id': run_id})}
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        
        # Check if it's a throttling error
        if error_code in ['ThrottlingException', 'ServiceQuotaExceededException', 'TooManyRequestsException']:
            # Wait for HealthOmics to register the run, then check if it was accepted despite the throttle
            run_name = f"{ENV}-{workflow_event}-{sample_id}-r{retry_count}"
            time.sleep(10)
            existing = omics.list_runs(name=run_name, maxResults=1)
            if existing.get('items', []):
                existing_status = existing['items'][0].get('status', '')
                if existing_status in ('PENDING', 'STARTING', 'RUNNING', 'COMPLETED'):
                    print(f"Run {run_name} already exists with status {existing_status}, skipping retry")
                    return {'statusCode': 200, 'body': f'Run already {existing_status}'}
            
            if retry_count < MAX_RETRIES:
                # Calculate backoff time
                backoff = min(INITIAL_BACKOFF * (2 ** retry_count), MAX_BACKOFF)
                
                print(f"Throttled. Retry {retry_count + 1}/{MAX_RETRIES} after {backoff}s")
                
                # Update DynamoDB with retry info and schedule retry
                schedule_retry(sample_id, workflow_event, retry_count + 1, backoff, batch_id, gender, cram_path, crai_path)
                
                return {
                    'statusCode': 429,
                    'body': json.dumps({
                        'message': 'Throttled, retry scheduled',
                        'retry_count': retry_count + 1,
                        'backoff_seconds': backoff
                    })
                }
            else:
                print(f"Max retries ({MAX_RETRIES}) exceeded for {sample_id}")
                publish_failure(sample_id, workflow_event, f"Max retries exceeded: {str(e)}")
                raise
        else:
            # Non-throttling error
            print(f"Error: {str(e)}")
            publish_failure(sample_id, workflow_event, str(e))
            raise

def submit_with_retry(workflow_id, workflow_version, workflow_stage, sample_id, 
                      batch_id, gender, parameters, retry_count):
    """Submit workflow to HealthOmics"""
    
    # Write parameters to S3 for audit trail
    params_s3_path = write_parameters_to_s3(sample_id, workflow_stage, parameters, retry_count)
    
    start_run_params = {
        'workflowId': workflow_id,
        'roleArn': HEALTHOMICS_ROLE_ARN,
        'name': f"{ENV}-{workflow_stage}-{sample_id}-r{retry_count}",
        'parameters': parameters,
        'cacheId': cache_id,
        'cacheBehavior': 'CACHE_ALWAYS',
        'storageType': 'DYNAMIC',
        'outputUri': f"s3://{OUTPUT_BUCKET}/{OUTPUT_PREFIX}/{workflow_stage}/{sample_id}",
        'tags': {
            'Project': PROJECT,
            'Owner': OWNER,
            'Env': ENV,
            'SampleId': sample_id,
            'Event': workflow_stage,
            'batch_id': batch_id,
            'gender': gender,
            'retry_count': str(retry_count),
            'parameters_s3': params_s3_path if params_s3_path else 'none'
        }
    }
    
    if workflow_version:
        start_run_params['workflowVersionName'] = workflow_version
    
    return omics.start_run(**start_run_params)

def LEGACY_submit_with_retry(workflow_id, workflow_version, workflow_stage, sample_id, 
                      batch_id, gender, parameters, retry_count):
    """Submit workflow to HealthOmics"""
    start_run_params = {
        'workflowId': workflow_id,
        'roleArn': HEALTHOMICS_ROLE_ARN,
        'name': f"{ENV}-{workflow_stage}-{sample_id}-r{retry_count}",
        'parameters': parameters,
        'cacheId': cache_id,
        'cacheBehavior': 'CACHE_ALWAYS',
        'storageType': 'DYNAMIC',
        'outputUri': f"s3://{OUTPUT_BUCKET}/{OUTPUT_PREFIX}/{workflow_stage}/{sample_id}",
        'tags': {
            'SampleId': sample_id,
            'Event': workflow_stage,
            'batch_id': batch_id,
            'gender': gender,
            'retry_count': str(retry_count)
        }
    }
    
    if workflow_version:
        start_run_params['workflowVersionName'] = workflow_version
    
    return omics.start_run(**start_run_params)

def schedule_retry(sample_id, workflow_stage, retry_count, backoff_seconds, batch_id=None, gender=None, cram_path=None, crai_path=None):
    """Schedule retry using EventBridge Scheduler (one-time scheduled event)"""

    scheduler = boto3.client('scheduler')
    
    # Calculate retry time
    now = datetime.datetime.now(datetime.UTC)
    retry_time = now + datetime.timedelta(seconds=backoff_seconds)
    
    # Create one-time schedule
    schedule_name = f"retry-{sample_id}-{workflow_stage}-{retry_count}"
    schedule_time = retry_time.strftime('%Y-%m-%dT%H:%M:%S')
    
    # Build message for scheduled retry (will trigger workflow again)
    retry_message = {
        'SampleId': sample_id,
        'Event': workflow_stage,
        'Status': 'PENDING',
        'Timestamp': now.isoformat(),
        'Info': {
            'retry_count': retry_count,
            'scheduled_at': schedule_time
        }
    }

    if batch_id:
        retry_message['Info']['batch_id_initial'] = batch_id
    if gender:
        retry_message['Info']['gender'] = gender
    if cram_path:
        retry_message['Info']['cram_path'] = cram_path
    if crai_path:
        retry_message['Info']['cram_index_path'] = crai_path

    try:
        #ScheduleExpression=f"at({retry_time.strftime('%Y-%m-%dT%H:%M:%S')})",
        scheduler.create_schedule(
            Name=schedule_name,
            ScheduleExpression=f"at({schedule_time})",
            FlexibleTimeWindow={'Mode': 'OFF'},
            Target={
                'Arn': SNS_TOPIC_ARN,
                'RoleArn': SCHEDULER_ROLE_ARN,
                'Input': json.dumps(retry_message)
            },
            State='ENABLED'
        )
        print(f"Scheduled retry for {sample_id} at {schedule_time}")
    except scheduler.exceptions.ConflictException:
        print(f"Schedule {schedule_name} already exists")
    
    # Publish retry metadata to SNS (for DynamoDB update via updater lambda)
    timestamp = datetime.datetime.now(datetime.UTC)
    sns.publish(
        TopicArn=SNS_TOPIC_ARN,
        Message=json.dumps({
            'SampleId': sample_id,
            'Event': workflow_stage,
            'Status': 'RETRY_SCHEDULED',
            'Timestamp': timestamp.isoformat(),
            'Info': {
                'retry_count': retry_count,
                'next_retry_at': retry_time.isoformat()
            }
        }),
        MessageGroupId=sample_id,
        MessageDeduplicationId=f"{sample_id}-{workflow_stage}-retry-{retry_count}"
    )

def publish_failure(sample_id, workflow_stage, error_message, batch_id=None, gender=None, cram_path=None):
    """Publish failure notification"""
    timestamp = datetime.datetime.now(datetime.UTC)
    sns.publish(
        TopicArn=SNS_TOPIC_ARN,
        Message=json.dumps({
            'SampleId': sample_id,
            'Event': workflow_stage,
            'Status': 'FAILED',
            'Timestamp': timestamp.isoformat(),
            'Info': {
                'error_message': str(error_message)
            }
        }),
        MessageGroupId=sample_id,
        MessageDeduplicationId=f"{sample_id}-{workflow_stage}-error-{int(timestamp.timestamp())}"
    )

def write_parameters_to_s3(sample_id, workflow_stage, parameters, retry_count):
    """Write parameters JSON to S3 for audit trail"""
    bucket = OUTPUT_BUCKET
    key = f"{PARAMETER_PREFIX}/{workflow_stage}/{sample_id}/inputs/parameters-r{retry_count}.json"
    
    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(parameters, indent=2),
            ContentType='application/json',
            ServerSideEncryption='AES256'
        )
        print(f"Parameters written to s3://{bucket}/{key}")
        return f"s3://{bucket}/{key}"
    except Exception as e:
        print(f"Warning: Failed to write parameters to S3: {str(e)}")
        return None