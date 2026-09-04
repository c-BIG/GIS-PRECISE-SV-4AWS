#!/usr/bin/env python3
"""
Register GATK-SV WDL workflows in AWS HealthOmics and write workflow_ids.json.

Each pipeline stage is a separate HealthOmics workflow. The main WDL for each stage
is zipped together with all its imports (the entire wdl/ dir is zipped so imports resolve).

Usage:
    python3 07_register_workflows.py --wdl-dir wdl --region ap-southeast-1 --profile npm
    # Optional: --stages GatherSampleEvidence EvidenceQC  (register only specific stages)
    # Optional: --output workflow_ids.json  (default: writes to the config location)

Requires: aws cli, boto3.
"""

import argparse
import json
import os
import subprocess
import tempfile
import time

# Map of stage name -> main WDL file. These are the top-level workflows HealthOmics runs.
STAGE_MAIN_WDL = {
    "GatherSampleEvidence": "GatherSampleEvidence.wdl",
    "EvidenceQC": "EvidenceQC.wdl",
    "TrainGCNV": "TrainGCNV.wdl",
    "GatherBatchEvidence": "GatherBatchEvidence.wdl",
    "ClusterBatch": "ClusterBatch.wdl",
    "GenerateBatchMetrics": "GenerateBatchMetrics.wdl",
    "FilterBatchSites": "FilterBatchSites.wdl",
    "FilterBatchSamples": "FilterBatchSamples.wdl",
    "MergeBatchSites": "MergeBatchSites.wdl",
    "GenotypeBatch": "GenotypeBatch.wdl",
    "RegenotypeCNVs": "RegenotypeCNVs.wdl",
    "MakeCohortVcf": "MakeCohortVcf.wdl",
    "RefineComplexVariants": "RefineComplexVariants.wdl",
    "JoinRawCalls": "JoinRawCalls.wdl",
    "SVConcordance": "SVConcordance.wdl",
    "FilterGenotypes": "FilterGenotypes.wdl",
    "AnnotateVcf": "AnnotateVcf.wdl",
}

DEFAULT_OUTPUT = os.path.join(
    os.path.dirname(__file__), '..', '..',
    'gatksv_healthomics', 'shared', 'python', 'config', 'workflow_ids.json'
)


def zip_wdls(wdl_dir):
    """Zip the entire WDL directory so imports resolve. Returns path to zip."""
    fd, zip_path = tempfile.mkstemp(suffix='.zip')
    os.close(fd)
    os.remove(zip_path)
    subprocess.run(
        ['zip', '-r', '-q', zip_path, '.', '-i', '*.wdl'],
        cwd=wdl_dir, check=True
    )
    return zip_path


def register(stage, main_wdl, zip_path, region, profile):
    """Register one workflow, return its ID."""
    result = subprocess.run(
        ['aws', 'omics', 'create-workflow',
         '--name', f'GATKSV_{stage}',
         '--engine', 'WDL',
         '--definition-zip', f'fileb://{zip_path}',
         '--main', main_wdl,
         '--region', region, '--profile', profile,
         '--query', 'id', '--output', 'text'],
        check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def wait_active(workflow_id, region, profile, timeout=300):
    """Wait for workflow to become ACTIVE."""
    start = time.time()
    while time.time() - start < timeout:
        status = subprocess.run(
            ['aws', 'omics', 'get-workflow', '--id', workflow_id,
             '--region', region, '--profile', profile,
             '--query', 'status', '--output', 'text'],
            check=True, capture_output=True, text=True
        ).stdout.strip()
        if status == 'ACTIVE':
            return True
        if status == 'FAILED':
            return False
        time.sleep(10)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--wdl-dir', required=True, help='Directory containing WDL files')
    ap.add_argument('--region', default='ap-southeast-1')
    ap.add_argument('--profile', default='default')
    ap.add_argument('--stages', nargs='+', default=None,
                    help='Specific stages to register (default: all)')
    ap.add_argument('--output', default=DEFAULT_OUTPUT)
    args = ap.parse_args()

    stages = args.stages or list(STAGE_MAIN_WDL.keys())

    print(f"Zipping WDLs from {args.wdl_dir}...")
    zip_path = zip_wdls(args.wdl_dir)

    # Load existing workflow_ids.json if present (preserve non-stage entries)
    workflow_ids = {}
    if os.path.exists(args.output):
        with open(args.output) as f:
            workflow_ids = json.load(f)

    try:
        for stage in stages:
            main_wdl = STAGE_MAIN_WDL.get(stage)
            if not main_wdl:
                print(f"  SKIP {stage}: no main WDL mapping")
                continue
            print(f"Registering {stage} (main={main_wdl})...")
            wf_id = register(stage, main_wdl, zip_path, args.region, args.profile)
            print(f"  id={wf_id}, waiting for ACTIVE...")
            ok = wait_active(wf_id, args.region, args.profile)
            print(f"  {'ACTIVE' if ok else 'FAILED'}")
            workflow_ids[stage] = {"id": wf_id, "version": None}
            # Persist after each (in case of interruption)
            with open(args.output, 'w') as f:
                json.dump(workflow_ids, f, indent=2)
    finally:
        os.remove(zip_path)

    print(f"\nWorkflow IDs written to {args.output}")
    print("Next: run 09_build_config_layer.sh and sync_config_to_s3.sh")


if __name__ == '__main__':
    main()
