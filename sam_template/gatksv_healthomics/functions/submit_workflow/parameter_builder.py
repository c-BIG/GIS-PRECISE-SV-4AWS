"""
Parameter builder for GATK-SV sample-level workflows (GatherSampleEvidence)
Loads templates and constructs workflow parameters
"""

import json
import os
from pathlib import Path
from config_loader import load_template, get_genome_references, get_docker_images

# --- Reference data (loaded via config_loader with S3 priority) ---
GENOME_REFS = get_genome_references()
DOCKER_IMAGES = get_docker_images()


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

    return params
