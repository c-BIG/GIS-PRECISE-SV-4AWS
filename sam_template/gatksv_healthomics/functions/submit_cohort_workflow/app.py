import json
import boto3
import os
import datetime
import subprocess
from botocore.exceptions import ClientError
from parameter_builder import build_parameters
from config_loader import get_workflow_id

omics = boto3.client('omics')
sns = boto3.client('sns')
s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

cache_id = os.environ.get('HEALTHOMICS_CACHE_ID', '2758152')
cache_strategy = "CACHE_ALWAYS"

AGGREGATES_TABLE_NAME = os.environ.get('AGGREGATES_TABLE_NAME', 'gatk-sv-dev-batch-cohort')
OUTPUT_BUCKET = os.environ.get('OUTPUT_BUCKET', '<your-bucket>')
OUTPUT_PREFIX = os.environ.get('OUTPUT_PREFIX', 'healthomics_test/test_output')
PARAMETER_PREFIX = os.environ.get('PARAMETER_PREFIX', 'healthomics_input')
HEALTHOMICS_ROLE_ARN = os.environ.get('HEALTHOMICS_ROLE_ARN', '')
AWS_REGION = os.environ.get('AWS_REGION', 'ap-southeast-1')
COST_TAG = os.environ.get('COST_TAG', 'gatk-sv')
OWNER = os.environ.get('OWNER', 'unknown')
ENV = os.environ.get('ENV', 'dev')


def get_cohort_data(cohort_id, cohort_record):
    """Get sample/batch data for cohort parameter building.
    batch_ids sourced from COHORT#/Event=FilterBatchSamples as authoritative batch list."""
    aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
    
    # Get batch_ids from COHORT FilterBatchSamples record (authoritative source)
    fb_resp = aggregates_table.get_item(Key={'pk': f'COHORT#{cohort_id}', 'Event': 'FilterBatchSamples'})
    fb_item = fb_resp.get('Item')
    if not fb_item:
        raise ValueError(f"No COHORT#{cohort_id}/Event=FilterBatchSamples record found — cannot proceed")
    
    batch_ids = sorted(fb_item.get('Info', {}).get('entity_member', []))
    if not batch_ids:
        raise ValueError(f"COHORT#{cohort_id}/Event=FilterBatchSamples has no Info.entity_member")
    
    # Build ddb_data using post-FilterBatchSamples sample lists (passing samples only)
    ddb_data = []
    for batch_id in batch_ids:
        resp = aggregates_table.get_item(
            Key={'pk': f'BATCH#{batch_id}', 'Event': 'FilterBatchSamples'}
        )
        item = resp.get('Item', {})
        samples = list(item.get('processed_entities', set()))
        for s in samples:
            ddb_data.append({'sample_id': s, 'batch_id': batch_id})
    
    return ddb_data, batch_ids


def submit_workflow_with_cli(cohort_id, workflow_stage, parameters, retry_count=0):
    """Submit workflow via AWS CLI to preserve exact JSON serialization."""
    workflow_id, workflow_version = get_workflow_id(workflow_stage)
    
    run_name = f"{ENV}-{workflow_stage}-{cohort_id}-r{retry_count}"
    output_uri = f"s3://{OUTPUT_BUCKET}/{OUTPUT_PREFIX}/{workflow_stage}/{cohort_id}"
    
    tags = {
        'Project': COST_TAG,
        'Owner': OWNER,
        'environment': ENV,
        'cohort_id': cohort_id,
        'workflow_stage': workflow_stage,
        'retry_count': str(retry_count),
    }
    
    # Write params to /tmp — file:// preserves exact JSON
    params_file = '/tmp/params.json'
    with open(params_file, 'w') as f:
        json.dump(parameters, f)
    
    # Upload to S3 for verification
    s3_key = f"{PARAMETER_PREFIX}/{workflow_stage}/{cohort_id}/params_r{retry_count}.json"
    s3.upload_file(params_file, OUTPUT_BUCKET, s3_key)
    print(f"Params uploaded to s3://{OUTPUT_BUCKET}/{s3_key}")
    
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
    
    print(f"CLI: start-run {run_name} workflow={workflow_id}")
    
    env = os.environ.copy()
    env['PATH'] = f"/opt/bin:{env.get('PATH', '')}"
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    
    if result.returncode != 0:
        error_msg = result.stderr.strip()
        print(f"CLI error: {error_msg}")
        raise ClientError(
            {'Error': {'Code': 'ValidationException', 'Message': error_msg}},
            'StartRun'
        )
    
    return json.loads(result.stdout)


def lambda_handler(event, context):
    """
    Submit cohort-level GATK-SV workflows to HealthOmics.
    Triggered by DDB stream events on COHORT# records with Status=PENDING.
    """
    print(f"Event: {json.dumps(event)}")
    
    detail = event['detail']
    new_image = detail['dynamodb']['NewImage']
    
    cohort_id = new_image.get('pk', {}).get('S', '').replace('COHORT#', '')
    workflow_stage = new_image['Event']['S']
    status = new_image.get('Status', {}).get('S', '')
    
    if status != 'PENDING':
        print(f"Ignoring non-PENDING status: {status}")
        return {'statusCode': 200, 'body': 'Ignored'}
    
    print(f"Processing: {workflow_stage} for cohort {cohort_id}")
    
    # Get cohort data
    ddb_data, batch_ids = get_cohort_data(cohort_id, new_image)
    print(f"Cohort {cohort_id}: {len(batch_ids)} batches, {len(ddb_data)} samples")
    
    # Get ped_file from Info or FilterBatchSamples cohort record
    info_map = new_image.get('Info', {}).get('M', {})
    ped_file = info_map.get('ped_file', {}).get('S', '')
    if not ped_file:
        agg_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
        resp = agg_table.get_item(Key={'pk': f'COHORT#{cohort_id}', 'Event': 'FilterBatchSamples'})
        ped_file = resp.get('Item', {}).get('Info', {}).get('ped_file', '')
    
    # Build run config
    run_config = {
        'batch_id': cohort_id,
        'cohort_id': cohort_id,
        'batch_ids': batch_ids,
        'ped_file': ped_file,
    }
    
    # Build parameters using template
    try:
        parameters = build_parameters(workflow_stage, run_config, ddb_data)
        print(f"Built parameters: {len(parameters)} keys, {len(json.dumps(parameters))} bytes")
    except Exception as e:
        print(f"Error building parameters: {e}")
        import traceback
        traceback.print_exc()
        raise
    
    # Submit via CLI
    try:
        response = submit_workflow_with_cli(cohort_id, workflow_stage, parameters)
        run_id = response['id']
        run_arn = response['arn']
        print(f"Started {workflow_stage} run {run_id} for cohort {cohort_id}")
        
        # Update DDB record with run info
        aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
        aggregates_table.update_item(
            Key={'pk': f'COHORT#{cohort_id}', 'Event': workflow_stage},
            UpdateExpression='SET #st = :status, Info.run_id = :rid, Info.run_arn = :arn, Info.started_at = :ts, output_uri = :uri',
            ExpressionAttributeNames={'#st': 'Status'},
            ExpressionAttributeValues={
                ':status': 'STARTED',
                ':rid': run_id,
                ':arn': run_arn,
                ':ts': datetime.datetime.now(datetime.UTC).isoformat(),
                ':uri': f"s3://{OUTPUT_BUCKET}/{OUTPUT_PREFIX}/{workflow_stage}/{cohort_id}/{run_id}"
            }
        )
        
        return {'statusCode': 200, 'body': json.dumps({'run_id': run_id})}
        
    except Exception as e:
        print(f"Error submitting {workflow_stage}: {e}")
        # Update DDB with error
        aggregates_table = dynamodb.Table(AGGREGATES_TABLE_NAME)
        aggregates_table.update_item(
            Key={'pk': f'COHORT#{cohort_id}', 'Event': workflow_stage},
            UpdateExpression='SET #st = :status, Info.error_message = :err',
            ExpressionAttributeNames={'#st': 'Status'},
            ExpressionAttributeValues={
                ':status': 'FAILED',
                ':err': str(e)
            }
        )
        raise
