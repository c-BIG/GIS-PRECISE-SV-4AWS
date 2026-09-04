"""
Configuration loader for GATK-SV workflows
Loads config from S3 (editable without redeploy) with fallback to Lambda Layer
"""
import json
import os
import boto3

CONFIG_DIR = '/opt/python/config'
S3_CONFIG_BUCKET = os.environ.get('CONFIG_BUCKET', '')
S3_CONFIG_PREFIX = os.environ.get('CONFIG_PREFIX', 'pipeline_config')

_s3 = None
_cache = {}

def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client('s3')
    return _s3

def _load_from_s3(s3_key):
    """Try loading a config file from S3. Returns None if not found."""
    if not S3_CONFIG_BUCKET:
        return None
    try:
        resp = _get_s3().get_object(Bucket=S3_CONFIG_BUCKET, Key=s3_key)
        data = json.loads(resp['Body'].read())
        print(f"Config loaded from S3: s3://{S3_CONFIG_BUCKET}/{s3_key}")
        return data
    except Exception as e:
        print(f"Config S3 fallback for {s3_key}: {e}")
        return None

def load_config(config_name):
    """Load config file. S3 takes priority over layer (allows edits without redeploy)."""
    if config_name in _cache:
        return _cache[config_name]

    # Try S3 first
    s3_key = f'{S3_CONFIG_PREFIX}/{config_name}.json'
    config = _load_from_s3(s3_key)

    # Fall back to layer
    if config is None:
        config_path = os.path.join(CONFIG_DIR, f'{config_name}.json')
        with open(config_path, 'r') as f:
            config = json.load(f)

    _cache[config_name] = config
    return config


def load_template(workflow_stage):
    """Load workflow template. S3 takes priority over layer."""
    cache_key = f'template:{workflow_stage}'
    if cache_key in _cache:
        return _cache[cache_key]

    # Try S3 first
    s3_key = f'{S3_CONFIG_PREFIX}/templates/{workflow_stage}.json'
    template = _load_from_s3(s3_key)

    # Fall back to layer
    if template is None:
        template_path = os.path.join(CONFIG_DIR, 'templates', f'{workflow_stage}.json')
        with open(template_path, 'r') as f:
            template = json.load(f)
        print(f"Config loaded from layer: {template_path}")

    _cache[cache_key] = template
    return template


def get_workflow_id(workflow_stage):
    """Get workflow ID and optional version"""
    workflow_ids = load_config('workflow_ids')

    if workflow_stage not in workflow_ids:
        raise ValueError(f"Workflow stage '{workflow_stage}' not found in workflow_ids.json")

    config = workflow_ids[workflow_stage]

    if isinstance(config, str):
        workflow_id = config
        version = None
    else:
        workflow_id = config.get('id')
        version = config.get('version')

    if workflow_id == 'PLACEHOLDER_WORKFLOW_ID' or not workflow_id:
        raise ValueError(f"Workflow ID for '{workflow_stage}' not configured")

    return workflow_id, version


def get_genome_references():
    return load_config('genome_references')


def get_docker_images():
    return load_config('docker_images')
