#!/usr/bin/env python3
"""
Bulk-trigger FilterBatchSites and/or FilterBatchSamples for completed batches.

With the FilterBatch split (2026-05-20), the flow is:
  GenerateBatchMetrics → FilterBatchSites [QC gate] → FilterBatchSamples [QC gate] → MergeBatchSites

This script writes Status=PENDING to the aggregates DDB table, which fires the DDB stream 
→ EventBridge → submit Lambda → runs the actual HealthOmics/Batch workflow.

When the workflow completes:
  RunStatusRecorder marks COMPLETED → StageAdvancer handles cohort counter → MergeBatchSites

Modes:
  --stage FilterBatchSites    Trigger FilterBatchSites workflow (after GenerateBatchMetrics COMPLETED).
                              Produces SV count plots for QC review.
  
  --stage FilterBatchSamples  Trigger FilterBatchSamples workflow (after FilterBatchSites COMPLETED).
                              Requires reviewing SV count plots first (N_IQR_cutoff in template).
                              On completion, StageAdvancer updates cohort counter → MergeBatchSites.

Usage:
    # Trigger FilterBatchSites for all batches with GenerateBatchMetrics done:
    python3 bulk_trigger_filterbatch.py --stage FilterBatchSites --dry-run
    python3 bulk_trigger_filterbatch.py --stage FilterBatchSites

    # After reviewing SV count plots — trigger FilterBatchSamples:
    python3 bulk_trigger_filterbatch.py --stage FilterBatchSamples --dry-run
    python3 bulk_trigger_filterbatch.py --stage FilterBatchSamples
"""

import argparse
import boto3
import csv
import json
import datetime
import sys
import os
from botocore.exceptions import ProfileNotFound

# 1. Get the profile name from the environment, default to None if unset
profile_name = os.environ.get("AWS_PROFILE")

if not profile_name:
    raise ValueError("CRITICAL: The AWS_PROFILE environment variable is not set!")

try:
    # 2. Force boto3 to create a session bound strictly to this profile
    # setting botocore session variables prevents automatic EC2 metadata fallbacks
    session = boto3.Session(profile_name=profile_name)
    
    # 3. Use this specific session to build your DynamoDB client
    dynamodb = session.client("dynamodb")
    print(f"Successfully forced session to use profile: {profile_name}")

except ProfileNotFound:
    print(f"CRITICAL: Profile '{profile_name}' was not found in your ~/.aws/config or ~/.aws/credentials files.")
    raise

def main():
    parser = argparse.ArgumentParser(
        description="Bulk-trigger FilterBatchSites or FilterBatchSamples via DDB PENDING → submit Lambda."
    )
    parser.add_argument('--stage', type=str, required=True,
                        choices=['FilterBatchSites', 'FilterBatchSamples'],
                        help='Which stage to trigger (writes PENDING to DDB → submit Lambda runs workflow)')
    parser.add_argument('--table', type=str, default='gatk-sv-dev-batch-cohort-BATCH',
                        help='DynamoDB aggregates table name (default: gatk-sv-dev-batch-cohort-BATCH)')
    parser.add_argument('--limit', type=int, default=0,
                        help='Max number of batches to process (0 for unlimited)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print actions without modifying DynamoDB')
    parser.add_argument('--region', type=str, default='ap-southeast-1',
                        help='AWS Region')
    parser.add_argument('--batch-ids', type=str, nargs='+',
                        help='Specific batch IDs to process (default: all eligible)')

    args = parser.parse_args()

    dynamodb = boto3.resource('dynamodb', region_name=args.region)
    table = dynamodb.Table(args.table)

    print(f"Table: {args.table}")
    print(f"Stage to trigger: {args.stage}")
    if args.limit > 0:
        print(f"Limit: {args.limit}")
    if args.batch_ids:
        print(f"Specific batches: {args.batch_ids}")

    # Determine the prerequisite stage
    prereq_stage = {
        'FilterBatchSites': 'GenerateBatchMetrics',
        'FilterBatchSamples': 'FilterBatchSites',
    }[args.stage]

    # Find all batches with prerequisite COMPLETED
    scan_params = {
        'FilterExpression': '#evt = :evt AND #s = :status',
        'ExpressionAttributeNames': {'#evt': 'Event', '#s': 'Status'},
        'ExpressionAttributeValues': {':evt': prereq_stage, ':status': 'COMPLETED'}
    }

    response = table.scan(**scan_params)
    items = response['Items']
    while 'LastEvaluatedKey' in response:
        response = table.scan(ExclusiveStartKey=response['LastEvaluatedKey'], **scan_params)
        items.extend(response['Items'])

    print(f"\nFound {len(items)} batches with {prereq_stage} COMPLETED")

    # Filter to specific batch IDs if provided
    if args.batch_ids:
        items = [i for i in items if i['pk'].replace('BATCH#', '') in args.batch_ids]
        print(f"Filtered to {len(items)} matching --batch-ids")

    triggered = 0
    skipped = 0

    for item in items:
        if args.limit > 0 and triggered >= args.limit:
            print(f"\nReached limit of {args.limit}. Stopping.")
            break

        batch_id = item['pk'].replace('BATCH#', '')

        # Check if target stage already exists and is active
        existing = table.get_item(Key={'pk': f'BATCH#{batch_id}', 'Event': args.stage})
        if 'Item' in existing:
            status = existing['Item'].get('Status', '')
            if status in ('COMPLETED', 'STARTED', 'RUNNING', 'PENDING'):
                print(f"  SKIP {batch_id} — {args.stage} already {status}")
                skipped += 1
                continue

        # Write PENDING record — DDB stream fires → EventBridge → submit Lambda runs the workflow
        record = {
            'pk': f'BATCH#{batch_id}',
            'Event': args.stage,
            'Status': 'PENDING',
            'Count_Total_Entities': item.get('Count_Total_Entities', 0),
            'processed_entities': item.get('processed_entities', set()),
            'Info': {
                'triggered_by': f'bulk_trigger_filterbatch.py ({prereq_stage} COMPLETED)',
                'retry_count': 0,
                'ped_file': item.get('Info', {}).get('ped_file', ''),
            },
            'last_updated': datetime.datetime.now(datetime.UTC).isoformat(),
        }

        if args.dry_run:
            print(f"  [DRY RUN] Would write PENDING for {args.stage} / {batch_id}")
        else:
            table.put_item(Item=record)
            print(f"  ✓ Wrote PENDING for {args.stage} / {batch_id} → submit Lambda will run workflow")

        triggered += 1

    print(f"\nSummary:")
    print(f"  {triggered} batches {'would be ' if args.dry_run else ''}triggered ({args.stage}=PENDING)")
    print(f"  {skipped} batches skipped (already PENDING/STARTED/RUNNING/COMPLETED)")

    if args.dry_run:
        print("\n  ⚠ DRY RUN — no DynamoDB changes were made.")
    else:
        print(f"\n  → Submit Lambda will pick up PENDING records via DDB stream and run {args.stage} workflows.")
        if args.stage == 'FilterBatchSamples':
            print(f"  → On completion, StageAdvancer updates cohort counter → triggers MergeBatchSites")
            print(f"     when all batches in the cohort have FilterBatchSamples COMPLETED.")
        elif args.stage == 'FilterBatchSites':
            print(f"  → On completion, FilterBatchSites is a QC gate (StageAdvancer stops).")
            print(f"     Review SV count plots, then re-run with --stage FilterBatchSamples.")


if __name__ == "__main__":
    main()
