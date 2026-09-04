"""
Helper functions for working with HealthOmics outputs
"""

import boto3
import json
from urllib.parse import urlparse

s3_client = boto3.client('s3')

def parse_s3_uri(s3_uri):
    """Parse S3 URI into bucket and key"""
    parsed = urlparse(s3_uri)
    bucket = parsed.netloc
    key = parsed.path.lstrip('/')
    return bucket, key


def get_healthomics_outputs(output_json_uri):
    """
    Fetch and parse HealthOmics output.json
    
    Args:
        output_json_uri: S3 URI to output.json (e.g., s3://bucket/run_id/output.json)
    
    Returns:
        dict: Parsed output.json content
    
    Example:
        outputs = get_healthomics_outputs(item['output_json'])
        coverage_counts = outputs['coverage_counts']
        manta_vcf = outputs['manta_vcf']
    """
    bucket, key = parse_s3_uri(output_json_uri)
    
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        outputs = json.loads(response['Body'].read())
        return outputs
    except Exception as e:
        print(f"Error fetching output.json from {output_json_uri}: {str(e)}")
        raise


def get_output_path(output_json_uri, output_name):
    """
    Get specific output path from HealthOmics output.json
    
    Args:
        output_json_uri: S3 URI to output.json
        output_name: Name of the output (e.g., 'coverage_counts', 'manta_vcf')
    
    Returns:
        str: S3 URI of the output file
    
    Example:
        coverage_counts = get_output_path(item['output_json'], 'coverage_counts')
    """
    outputs = get_healthomics_outputs(output_json_uri)
    
    if output_name not in outputs:
        raise KeyError(f"Output '{output_name}' not found in output.json")
    
    return outputs[output_name]


def get_batch_outputs(ddb_items, output_name):
    """
    Get specific output for all samples in a batch
    
    Args:
        ddb_items: List of DynamoDB items (samples)
        output_name: Name of the output to extract
    
    Returns:
        list: List of S3 URIs in same order as ddb_items
    
    Example:
        # Get coverage_counts for all samples in batch
        samples = query_batch_samples(batch_id, 'GatherSampleEvidence')
        counts = get_batch_outputs(samples, 'coverage_counts')
    """
    output_paths = []
    
    for item in ddb_items:
        if 'output_json' not in item:
            raise ValueError(f"Sample {item['sample_id']} missing output_json")
        
        output_path = get_output_path(item['output_json'], output_name)
        output_paths.append(output_path)
    
    return output_paths
