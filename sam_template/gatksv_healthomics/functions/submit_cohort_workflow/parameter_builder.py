"""
Parameter builder for GATK-SV cohort-level workflows
Loads templates and constructs workflow parameters
"""

import json
import boto3
import os
from pathlib import Path
from config_loader import load_config, load_template, get_genome_references, get_docker_images

# --- Configuration ---
CONFIG_DIR = Path('/opt/python/config')
HEALTHOMICS_PARAM_LIMIT = 50000  # bytes

# --- Environment variables ---
STAGING_BUCKET = os.environ.get('OUTPUT_BUCKET', '<your-bucket>')
STAGING_PREFIX = os.environ.get('PARAMETER_PREFIX', 'healthomics_input')
SAMPLE_DDB_TABLE = os.environ.get('DDB_TABLE_NAME', 'gatk-sv-dev-sample')
AGGREGATES_DDB_TABLE = os.environ.get('AGGREGATES_TABLE_NAME', 'gatk-sv-dev-batch-cohort')

# --- AWS clients ---
s3_client = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')
sample_table = dynamodb.Table(SAMPLE_DDB_TABLE)
aggregates_table = dynamodb.Table(AGGREGATES_DDB_TABLE)

# --- Reference data (loaded via config_loader with S3 priority) ---
GENOME_REFS = get_genome_references()
DOCKER_IMAGES = get_docker_images()

# Module-level cache: cleared when Lambda invocation ends
_s3_listing_cache = {}
_output_uri_cache = {}



def compact_params_with_manifests(params, batch_id, workflow_stage='unknown'):
    """If params exceed 50KB, write large Array[File] params to S3 manifest files.
    Manifests written to: {STAGING_PREFIX}/{workflow_stage}/{batch_id}/manifests/{param}_manifest.txt
    """
    estimated = len(json.dumps(params, separators=(',', ':')))
    if estimated <= HEALTHOMICS_PARAM_LIMIT:
        return params

    print(f"Params {estimated} bytes exceeds limit, writing manifests")

    list_params = [(k, v) for k, v in params.items() if isinstance(v, list) and len(json.dumps(v)) > 1000]
    list_params.sort(key=lambda x: len(json.dumps(x[1])), reverse=True)

    for param_name, param_list in list_params:
        if not param_list or not isinstance(param_list[0], str) or not param_list[0].startswith('s3://'):
            continue

        manifest_key = f"{STAGING_PREFIX}/{workflow_stage}/{batch_id}/manifests/{param_name}_manifest.txt"
        s3_client.put_object(Bucket=STAGING_BUCKET, Key=manifest_key, Body='\n'.join(param_list))
        manifest_uri = f"s3://{STAGING_BUCKET}/{manifest_key}"

        del params[param_name]
        params[f"{param_name}_manifest"] = manifest_uri

        estimated = len(json.dumps(params, separators=(',', ':')))
        if estimated <= HEALTHOMICS_PARAM_LIMIT:
            break

    final_size = len(json.dumps(params, separators=(',', ':')))
    if final_size > HEALTHOMICS_PARAM_LIMIT:
        print(f"WARNING: Parameters still exceed limit after manifests ({final_size} bytes)")
    return params


def build_parameters(workflow_stage, run_config, ddb_data=None):
    """Build workflow parameters from template + run config."""
    template = load_template(workflow_stage)
    params = {}

    for param in template.get('static_params', []):
        if param in GENOME_REFS:
            params[param] = GENOME_REFS[param]

    for param in template.get('docker_params', []):
        if param in DOCKER_IMAGES and DOCKER_IMAGES[param]:
            params[param] = DOCKER_IMAGES[param]

    for key, value_template in template.get('dynamic_params', {}).items():
        if isinstance(value_template, str) and value_template.startswith('{{') and value_template.endswith('}}'):
            var_name = value_template[2:-2].strip()
            if var_name.startswith('query:ddb:'):
                result = resolve_ddb_query(var_name, ddb_data, run_config)
            else:
                result = run_config.get(var_name)
            if result is not None and result != [] and result != '':
                params[key] = result
        else:
            params[key] = value_template

    for key, default_value in template.get('optional_params', {}).items():
        if 'options' in run_config and key in run_config['options']:
            val = run_config['options'][key]
        else:
            val = default_value
        if isinstance(val, str) and val.lower() in ('true', 'false'):
            val = val.lower() == 'true'
        params[key] = val

    for key in template.get('optional_file_params', []):
        if key in run_config and run_config[key] is not None:
            params[key] = run_config[key]

    params = compact_params_with_manifests(params, run_config.get('batch_id', 'unknown'), workflow_stage)
    return params


def resolve_ddb_query(query, ddb_data, run_config=None):
    """Resolve DynamoDB query from template."""
    if not ddb_data:
        return []

    batch_id = run_config.get('batch_id') if isinstance(run_config, dict) else run_config
    parts = query.split(':')

    if parts[2] == 'samples_in_batch':
        return [item['sample_id'] for item in ddb_data]

    elif parts[2] == 'batches_in_cohort':
        return run_config.get('batch_ids', []) if isinstance(run_config, dict) else []

    elif parts[2] == 'batch_outputs':
        # query:ddb:batch_outputs:{Stage}:{folder}:{suffix}:glob
        # Returns array of files, one per batch in the cohort
        # For batch-level stages: looks up BATCH#{bid} per batch
        # For cohort-level stages: falls back to COHORT#{cohort_id} (single record, glob for per-batch files)
        if len(parts) >= 6:
            workflow_stage = parts[3]
            output_folder = parts[4]
            file_suffix = parts[5]

            batch_ids = run_config.get('batch_ids', []) if isinstance(run_config, dict) else []
            if not batch_ids:
                batch_ids = list(dict.fromkeys(
                    item.get('batch_id') or item.get('batch_id_initial') for item in ddb_data
                    if item.get('batch_id') or item.get('batch_id_initial')
                ))

            cohort_stages = {'MergeBatchSites', 'GenotypeBatch', 'RegenotypeCNVs', 'MakeCohortVcf',
                             'RefineComplexVariants', 'JoinRawCalls', 'SVConcordance', 'FilterGenotypes', 'AnnotateVcf'}

            all_outputs = []
            if workflow_stage in cohort_stages:
                # Cohort-level stage: single COHORT# record, glob output folder
                try:
                    outputs = resolve_output_paths_s3_glob(ddb_data, workflow_stage, output_folder, file_suffix, batch_id)
                    all_outputs.extend(outputs)
                except ValueError as e:
                    print(f"WARNING: batch_outputs {workflow_stage}/COHORT#{batch_id}: {e}")
            else:
                # Batch-level stage: one BATCH# record per batch
                for bid in batch_ids:
                    try:
                        outputs = resolve_output_paths_s3_glob(ddb_data, workflow_stage, output_folder, file_suffix, bid)
                        all_outputs.extend(outputs)
                    except ValueError as e:
                        print(f"WARNING: batch_outputs {workflow_stage}/{bid}: {e}")
            return all_outputs

    elif parts[2] == 'output_paths':
        if len(parts) >= 6:
            workflow_stage = parts[3]
            output_folder = parts[4]
            file_suffix = parts[5]
            modifier = parts[6] if len(parts) >= 7 else None

            if modifier == 'glob':
                result = resolve_batch_workflow_outputs(ddb_data, workflow_stage, output_folder, file_suffix, batch_id)
                if len(parts) >= 8 and parts[7] == 'single':
                    return result[0] if result else None
                return result

            result = resolve_output_paths_enhanced(ddb_data, workflow_stage, output_folder, file_suffix, batch_id)
            if modifier == 'single':
                return result[0] if result else None
            return result

    return []


def resolve_output_paths_enhanced(ddb_data, workflow_stage, output_folder, file_suffix, batch_id=None):
    """Route to sample or batch output resolver."""
    sample_workflows = ['GatherSampleEvidence']
    batch_workflows = ['EvidenceQC', 'TrainGCNV', 'GatherBatchEvidence', 'ClusterBatch',
                       'GenerateBatchMetrics', 'FilterBatchSites', 'FilterBatchSamples', 'GenotypeBatch', 'MergeBatchSites']

    if workflow_stage in sample_workflows:
        return resolve_sample_workflow_outputs(ddb_data, workflow_stage, output_folder, file_suffix)
    elif workflow_stage in batch_workflows:
        return resolve_batch_workflow_outputs(ddb_data, workflow_stage, output_folder, file_suffix, batch_id)
    else:
        raise ValueError(f"Unknown workflow stage: {workflow_stage}")


def resolve_sample_workflow_outputs(ddb_data, workflow_stage, output_folder, file_suffix):
    """Resolve outputs from sample-level workflows using cached S3 listing."""
    output_paths = []
    for item in ddb_data:
        sample_id = item['sample_id']
        cache_key = (sample_id, workflow_stage)

        if cache_key not in _s3_listing_cache:
            if cache_key not in _output_uri_cache:
                response = sample_table.query(
                    KeyConditionExpression='pk = :pk AND begins_with(sk, :sk_prefix)',
                    FilterExpression='#status = :completed',
                    ExpressionAttributeNames={'#status': 'Status'},
                    ExpressionAttributeValues={
                        ':pk': f'SAMPLE#{sample_id}',
                        ':sk_prefix': f'{workflow_stage}#',
                        ':completed': 'COMPLETED'
                    },
                    ScanIndexForward=False
                )
                items = response.get('Items', [])
                if not items:
                    raise ValueError(f"No COMPLETED {workflow_stage} record for sample {sample_id}")
                output_uri = items[0].get('OutputUrl')
                if not output_uri:
                    raise ValueError(f"No OutputUrl in COMPLETED {workflow_stage} record for {sample_id}")
                _output_uri_cache[cache_key] = output_uri

            output_uri = _output_uri_cache[cache_key]
            prefix_path = f"{output_uri.rstrip('/')}/out/"
            without_scheme = prefix_path.replace('s3://', '')
            bucket = without_scheme.split('/')[0]
            prefix = without_scheme.split('/', 1)[1]

            all_keys = []
            paginator = s3_client.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                all_keys.extend(f"s3://{bucket}/{obj['Key']}" for obj in page.get('Contents', []))
            _s3_listing_cache[cache_key] = all_keys

        all_keys = _s3_listing_cache[cache_key]
        matched = [k for k in all_keys if f'/out/{output_folder}/' in k and k.endswith(f'.{file_suffix}')]

        if not matched:
            output_paths.append(None)
        else:
            output_paths.append(matched[0])

    return [p for p in output_paths if p is not None]


def resolve_batch_workflow_outputs(ddb_data, workflow_stage, output_folder, file_suffix, batch_id=None):
    """Resolve outputs from batch-level or cohort-level workflows using S3 glob.
    For cohort stages: uses cohort_id (passed as batch_id) to look up COHORT# record.
    For batch stages: collects outputs from ALL batches in ddb_data."""
    
    cohort_stages = {'MergeBatchSites', 'GenotypeBatch', 'RegenotypeCNVs', 'MakeCohortVcf',
                     'RefineComplexVariants', 'JoinRawCalls', 'SVConcordance', 'FilterGenotypes', 'AnnotateVcf'}
    
    if workflow_stage in cohort_stages:
        # Cohort-level: single lookup using cohort_id
        return resolve_output_paths_s3_glob(ddb_data, workflow_stage, output_folder, file_suffix, batch_id)
    
    # Batch-level: collect outputs from each batch
    batch_ids = list(dict.fromkeys(
        item.get('batch_id') or item.get('batch_id_initial') for item in ddb_data if item.get('batch_id') or item.get('batch_id_initial')
    ))
    
    if not batch_ids and batch_id:
        batch_ids = [batch_id]
    
    # Collect outputs from each batch
    all_outputs = []
    for bid in batch_ids:
        try:
            outputs = resolve_output_paths_s3_glob(ddb_data, workflow_stage, output_folder, file_suffix, bid)
            all_outputs.extend(outputs)
        except ValueError as e:
            print(f"WARNING: {workflow_stage}/{bid}: {e}")
    
    return all_outputs


def resolve_output_paths_s3_glob(ddb_data, workflow_stage, output_folder, file_suffix, batch_id=None):
    """List S3 files matching suffix under output_folder (handles nested subfolders)."""
    if not ddb_data:
        return []
    if not batch_id:
        batch_id = ddb_data[0].get('batch_id') or ddb_data[0].get('batch_id_initial')
    if not batch_id:
        raise ValueError("No batch_id available for S3 glob resolution")

    # Cohort-level stages are stored under COHORT# pk, not BATCH#
    cohort_stages = {'MergeBatchSites', 'GenotypeBatch', 'RegenotypeCNVs', 'MakeCohortVcf',
                     'RefineComplexVariants', 'JoinRawCalls', 'SVConcordance', 'FilterGenotypes', 'AnnotateVcf'}
    if workflow_stage in cohort_stages:
        # batch_id passed here is actually the cohort_id for cohort workflows
        pk = f'COHORT#{batch_id}'
    else:
        pk = f'BATCH#{batch_id}'

    response = aggregates_table.get_item(Key={'pk': pk, 'Event': workflow_stage})
    if 'Item' not in response:
        raise ValueError(f"No {workflow_stage} record for {pk}")

    output_uri = response['Item'].get('output_uri') or response['Item'].get('OutputUrl')
    if not output_uri:
        raise ValueError(f"No output_uri in {workflow_stage} record for {pk}")

    prefix_path = f"{output_uri.rstrip('/')}/out/{output_folder}/"
    without_scheme = prefix_path.replace('s3://', '')
    bucket = without_scheme.split('/')[0]
    prefix = without_scheme.split('/', 1)[1]

    matched = []
    paginator = s3_client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            if obj['Key'].endswith(f'.{file_suffix}'):
                matched.append(f"s3://{bucket}/{obj['Key']}")

    matched.sort()
    print(f"  {pk}/{output_folder}/*.{file_suffix} → {len(matched)} files")
    return matched
