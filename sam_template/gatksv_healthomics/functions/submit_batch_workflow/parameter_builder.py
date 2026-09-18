"""
Parameter builder for GATK-SV workflows
Loads templates and constructs workflow parameters
"""

import json
import boto3
import os
from pathlib import Path
from config_loader import load_template, get_genome_references, get_docker_images

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


def _resolve_docker_ref(val):
    """Resolve a '{{docker:<key>}}' reference to its image URI from docker_images.json.
    Lets a template override (e.g. optional_params.gatk_docker) point at another
    docker key by name instead of hardcoding a full ECR URI. Non-matching values
    are returned unchanged.
    """
    if isinstance(val, str) and val.startswith('{{docker:') and val.endswith('}}'):
        ref_key = val[len('{{docker:'):-2].strip()
        resolved = DOCKER_IMAGES.get(ref_key)
        if not resolved:
            raise ValueError(
                f"optional_params references unknown docker key '{ref_key}' "
                f"(via {val}); not found in docker_images.json"
            )
        return resolved
    return val

# Module-level cache: cleared when Lambda invocation ends
_s3_listing_cache = {}   # (sample_id, workflow_stage) → [s3 keys]
_output_uri_cache = {}   # (sample_id, workflow_stage) → output_uri string


def compact_params_with_manifests(params, batch_id, workflow_stage='unknown'):
    """If params exceed 50KB, write large Array[File] params to S3 manifest files.
    Manifests written to: {STAGING_PREFIX}/{workflow_stage}/{batch_id}/manifests/{param}_manifest.txt
    """
    estimated = len(json.dumps(params, separators=(',', ':')))
    if estimated <= HEALTHOMICS_PARAM_LIMIT:
        return params

    print(f"Parameters size {estimated} bytes — exceeds {HEALTHOMICS_PARAM_LIMIT}, writing manifests")

    list_params = [(k, v) for k, v in params.items() if isinstance(v, list) and len(json.dumps(v)) > 1000]
    list_params.sort(key=lambda x: len(json.dumps(x[1])), reverse=True)

    for param_name, param_list in list_params:
        if not param_list or not isinstance(param_list[0], str) or not param_list[0].startswith('s3://'):
            continue

        manifest_key = f"{STAGING_PREFIX}/{workflow_stage}/{batch_id}/manifests/{param_name}_manifest.txt"
        manifest_body = '\n'.join(param_list)
        s3_client.put_object(Bucket=STAGING_BUCKET, Key=manifest_key, Body=manifest_body)
        manifest_uri = f"s3://{STAGING_BUCKET}/{manifest_key}"

        del params[param_name]
        params[f"{param_name}_manifest"] = manifest_uri
        print(f"  Manifest {param_name} ({len(param_list)} files) → {manifest_uri}")

        estimated = len(json.dumps(params, separators=(',', ':')))
        if estimated <= HEALTHOMICS_PARAM_LIMIT:
            break

    final_size = len(json.dumps(params, separators=(',', ':')))
    if final_size > HEALTHOMICS_PARAM_LIMIT:
        print(f"WARNING: Parameters still exceed limit after manifests ({final_size} bytes)")
    return params


def build_parameters(workflow_stage, run_config, ddb_data=None):
    """
    Build workflow parameters from template + run config
    
    Args:
        workflow_stage: Workflow name (e.g., 'GatherSampleEvidence')
        run_config: User-provided configuration dict
        ddb_data: List of DynamoDB items (for batch workflows)
    
    Returns:
        Complete parameter dictionary for HealthOmics
    """
    template = load_template(workflow_stage)
    params = {}
    
    # 1. Add static genome references
    for param in template.get('static_params', []):
        if param in GENOME_REFS:
            params[param] = GENOME_REFS[param]
    
    # 2. Add docker images
    for param in template.get('docker_params', []):
        if param in DOCKER_IMAGES and DOCKER_IMAGES[param]:
            params[param] = DOCKER_IMAGES[param]
    
    # 3. Add dynamic parameters (skip empty/None results so WDL optional inputs are omitted)
    for key, value_template in template.get('dynamic_params', {}).items():
        if isinstance(value_template, str) and value_template.startswith('{{') and value_template.endswith('}}'):
            var_name = value_template[2:-2].strip()
            
            if var_name.startswith('query:ddb:'):
                result = resolve_ddb_query(var_name, ddb_data, run_config.get('batch_id'))
            else:
                result = run_config.get(var_name)
            if result is not None and result != [] and result != '':
                params[key] = result
        else:
            params[key] = value_template
    
    # 4. Add optional parameters (convert string booleans to native booleans for WDL)
    for key, default_value in template.get('optional_params', {}).items():
        if 'options' in run_config and key in run_config['options']:
            val = run_config['options'][key]
        else:
            val = default_value
        val = _resolve_docker_ref(val)
        if isinstance(val, str) and val.lower() in ('true', 'false'):
            val = val.lower() == 'true'
        params[key] = val
    
    # 5. Add optional file parameters
    for key in template.get('optional_file_params', []):
        if key in run_config and run_config[key] is not None:
            params[key] = run_config[key]

    # 6. If params exceed 50KB, write large arrays to S3 manifest files
    params = compact_params_with_manifests(params, run_config.get('batch_id', 'unknown'), workflow_stage)

    return params


def resolve_ddb_query(query, ddb_data, batch_id=None):
    """
    Resolve DynamoDB query from template with enhanced output path support
    
    Supports new format: query:ddb:output_paths:WorkflowStage:output_folder:file_suffix
    Example: query:ddb:output_paths:GatherSampleEvidence:coverage_counts:counts.tsv.gz
    
    This will:
    1. Query Sample DynamoDB for pk=sample_id, sk=GatherSampleEvidence#timestamp
    2. Extract HealthOmics output path from the record
    3. Construct full path: output_path/coverage_counts/sample_id.counts.tsv.gz
    
    Args:
        query: Query string from template
        ddb_data: List of sample data or batch data
        batch_id: Current batch ID for context
    
    Returns:
        List of values in consistent sample order
    """
    
    if not ddb_data:
        return []
    
    print(f"Query: {query}")
    parts = query.split(':')
    
    if parts[2] == 'samples_in_batch':
        # Return sample IDs in order
        return [item['sample_id'] for item in ddb_data]
    
    elif parts[2] == 'batches_in_cohort':
        # Return unique batch IDs in order
        seen = set()
        batches = []
        for item in ddb_data:
            batch_id = item.get('batch_id_qced') or item.get('batch_id_initial')
            if batch_id and batch_id not in seen:
                seen.add(batch_id)
                batches.append(batch_id)
        return batches
    
    elif parts[2] == 'output_paths':
        # Format: query:ddb:output_paths:WorkflowStage:output_folder:file_suffix[:single|glob]
        #   :single  → return first item as string
        #   :glob    → S3 list all files matching suffix under output_folder (for nested subfolders)
        if len(parts) >= 6:
            workflow_stage = parts[3]
            output_folder = parts[4]
            file_suffix = parts[5]
            modifier = parts[6] if len(parts) >= 7 else None
            
            if modifier == 'glob':
                result = resolve_output_paths_s3_glob(ddb_data, workflow_stage, output_folder, file_suffix, batch_id)
                if len(parts) >= 8 and parts[7] == 'single':
                    return result[0] if result else None
                return result
            
            result = resolve_output_paths_enhanced(ddb_data, workflow_stage, output_folder, file_suffix, batch_id)
            if modifier == 'single':
                return result[0] if result else None
            return result
    
    return []


def resolve_output_paths_enhanced(ddb_data, workflow_stage, output_folder, file_suffix, batch_id=None):
    """
    Resolve output paths using enhanced format with DynamoDB queries
    
    Handles both sample-level and batch-level workflows:
    - Sample workflows (GatherSampleEvidence): Query Sample DynamoDB
    - Batch workflows (EvidenceQC, TrainGCNV, etc.): Query Aggregates DynamoDB
    """
    sample_workflows = ['GatherSampleEvidence']
    batch_workflows = ['EvidenceQC', 'TrainGCNV', 'GatherBatchEvidence', 'ClusterBatch', 
                      'GenerateBatchMetrics', 'FilterBatch', 'GenotypeBatch', 'MergeBatchSites']
    
    if workflow_stage in sample_workflows:
        return resolve_sample_workflow_outputs(ddb_data, workflow_stage, output_folder, file_suffix)
    elif workflow_stage in batch_workflows:
        return resolve_batch_workflow_outputs(ddb_data, workflow_stage, output_folder, file_suffix, batch_id)
    else:
        raise ValueError(f"Unknown workflow stage: {workflow_stage}")


def resolve_sample_workflow_outputs(ddb_data, workflow_stage, output_folder, file_suffix):
    """Resolve outputs from sample-level workflows using cached S3 listing.
    
    First call per (sample_id, workflow_stage) does:
      1. DDB query for COMPLETED record → get OutputUrl
      2. S3 LIST of {OutputUrl}/out/ → cache all file keys
    Subsequent calls grep the cached list in-memory.
    """
    output_paths = []
    
    for item in ddb_data:
        sample_id = item['sample_id']
        cache_key = (sample_id, workflow_stage)
        
        try:
            # Get or cache the S3 file listing for this sample
            if cache_key not in _s3_listing_cache:
                # DDB query for OutputUrl (also cached)
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
                        raise ValueError(f"No COMPLETED {workflow_stage} record found for sample {sample_id}")
                    output_uri = items[0].get('OutputUrl')
                    if not output_uri:
                        raise ValueError(f"No OutputUrl in COMPLETED {workflow_stage} record for sample {sample_id}")
                    _output_uri_cache[cache_key] = output_uri
                
                # S3 LIST entire out/ folder once
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
                print(f"Cached {len(all_keys)} files for {sample_id}/{workflow_stage}")
            
            # Grep cached listing for output_folder + file_suffix
            all_keys = _s3_listing_cache[cache_key]
            matched = [k for k in all_keys if f'/out/{output_folder}/' in k and k.endswith(f'.{file_suffix}')]
            
            if not matched:
                print(f"Warning: No *.{file_suffix} in {output_folder}/ for {sample_id}, skipping")
                output_paths.append(None)
            else:
                output_paths.append(matched[0])
            
        except Exception as e:
            print(f"Error resolving {workflow_stage} output for sample {sample_id}: {str(e)}")
            raise
    
    # Filter out None entries (optional outputs not found)
    output_paths = [p for p in output_paths if p is not None]
    return output_paths


def resolve_batch_workflow_outputs(ddb_data, workflow_stage, output_folder, file_suffix, batch_id=None):
    """Resolve outputs from batch-level workflows using S3 glob (Aggregates DynamoDB)."""
    return resolve_output_paths_s3_glob(ddb_data, workflow_stage, output_folder, file_suffix, batch_id)



def resolve_output_paths_s3_glob(ddb_data, workflow_stage, output_folder, file_suffix, batch_id=None):
    """List S3 files matching suffix under output_folder (handles nested subfolders).
    
    Use for outputs like TrainGCNV gcnv_model_tars where files are in:
      {output_uri}/out/cohort_gcnv_model_tars/0000/model.tar.gz
      {output_uri}/out/cohort_gcnv_model_tars/0001/model.tar.gz
    
    Template: {{query:ddb:output_paths:TrainGCNV:cohort_gcnv_model_tars:tar.gz:glob}}
    """

    if not ddb_data:
        return []

    if not batch_id:
        raise ValueError("No batch_id provided for S3 glob resolution")

    response = aggregates_table.get_item(
        Key={'pk': f'BATCH#{batch_id}', 'Event': workflow_stage}
    )
    if 'Item' not in response:
        raise ValueError(f"No {workflow_stage} batch record found for batch {batch_id}")

    output_uri = response['Item'].get('output_uri') or response['Item'].get('OutputUrl')
    if not output_uri:
        raise ValueError(f"No output_uri in {workflow_stage} record for {batch_id}")

    # Build S3 prefix: {output_uri}/out/{output_folder}/
    prefix_path = f"{output_uri.rstrip('/')}/out/{output_folder}/"

    # Parse bucket and key from s3:// URI
    without_scheme = prefix_path.replace('s3://', '')
    bucket = without_scheme.split('/')[0]
    prefix = without_scheme.split('/', 1)[1]

    # List all objects under prefix matching suffix
    matched = []
    paginator = s3_client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            if obj['Key'].endswith(f'.{file_suffix}'):
                matched.append(f"s3://{bucket}/{obj['Key']}")

    matched.sort()
    print(f"S3 glob: {prefix} → {len(matched)} files matching *.{file_suffix}")
    return matched



