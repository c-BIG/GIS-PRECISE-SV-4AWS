#!/usr/bin/env python3
"""
Redrive failed GatherSampleEvidence samples.

Scans sample DDB table for samples whose latest GSE event is FAILED,
then sends a new PENDING message to SQS (same format as the original submission)
to retrigger the pipeline.

Usage:
    # Dry run — show which samples would be redriven
    python3 scripts/redrive_failed_gse.py --dry-run

    # Redrive all failed GSE samples
    python3 scripts/redrive_failed_gse.py

    # Redrive specific samples only
    python3 scripts/redrive_failed_gse.py --sample-ids NPM1019F1D NPM1019F9B

    # Limit to N samples
    python3 scripts/redrive_failed_gse.py --limit 10
"""

import argparse
import boto3
import csv
import json
import datetime
import sys
import os
import boto3
from botocore.exceptions import ProfileNotFound
from collections import defaultdict

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
    parser = argparse.ArgumentParser(description='Redrive failed GatherSampleEvidence samples')
    parser.add_argument('--sample-table', default='gatk-sv-prod-sample-BATCH',
                        help='Sample DynamoDB table name')
    parser.add_argument('--queue-url', 
                        default='https://sqs.<REGION>.amazonaws.com/<ACCOUNT>/precise-ilmn-prod-gatksv-sqs-events.fifo',
                        help='SQS queue URL for sample events')
    parser.add_argument('--sample-ids', nargs='+', default=None,
                        help='Specific sample IDs to redrive (default: all failed)')
    parser.add_argument('--limit', type=int, default=0,
                        help='Maximum number of samples to redrive (0=unlimited)')
    parser.add_argument('--delay', type=float, default=0,
                        help='Seconds to wait between each SQS message (default: 0, no delay)')
    parser.add_argument('--batch-delay', type=float, default=0,
                        help='Seconds to wait every N messages (use with --batch-size)')
    parser.add_argument('--batch-size', type=int, default=10,
                        help='Number of messages to send before applying --batch-delay (default: 10)')
    parser.add_argument('--profile', default='npm', help='AWS profile')
    parser.add_argument('--region', default='ap-southeast-1', help='AWS region')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be redriven without sending')
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    dynamodb = session.resource('dynamodb')
    sqs = session.client('sqs')
    table = dynamodb.Table(args.sample_table)

    # Scan for all GSE records
    print(f"Scanning {args.sample_table} for GatherSampleEvidence events...")
    
    # Group records by pk
    samples = defaultdict(list)
    scan_kwargs = {}
    total_records = 0
    
    while True:
        response = table.scan(**scan_kwargs)
        for item in response.get('Items', []):
            pk = item.get('pk', '')
            sk = item.get('sk', '')
            if pk.startswith('SAMPLE#') and sk.startswith('GatherSampleEvidence#'):
                samples[pk].append(item)
                total_records += 1
        
        if 'LastEvaluatedKey' not in response:
            break
        scan_kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']
    
    print(f"  Found {total_records} GSE records across {len(samples)} samples")

    # Find samples whose latest GSE event is FAILED
    failed_samples = []
    for pk, events in samples.items():
        # Sort by timestamp (sk = GatherSampleEvidence#<timestamp>)
        events_sorted = sorted(events, key=lambda x: x.get('sk', ''), reverse=True)
        latest = events_sorted[0]
        
        if latest.get('Status') == 'FAILED':
            sample_id = pk.replace('SAMPLE#', '')
            
            # Filter to specific samples if requested
            if args.sample_ids and sample_id not in args.sample_ids:
                continue
            
            failed_samples.append({
                'pk': pk,
                'sample_id': sample_id,
                'latest_sk': latest.get('sk', ''),
                'latest_timestamp': latest.get('Timestamp', ''),
                'info': latest.get('Info', {}),
                'batch_id_initial': latest.get('batch_id_initial', '') or latest.get('Info', {}).get('batch_id_initial', ''),
                'gender': latest.get('gender', '') or latest.get('Info', {}).get('gender', ''),
            })

    # Sort by earliest timestamp first (prioritise samples that have been waiting longest)
    failed_samples.sort(key=lambda x: x['latest_timestamp'])

    # Apply limit
    if args.limit > 0:
        failed_samples = failed_samples[:args.limit]

    print(f"\n  Failed GSE samples (latest event = FAILED): {len(failed_samples)}")
    
    if not failed_samples:
        print("  No failed samples to redrive.")
        return

    # Also look for the original PENDING event to get full Info (cram paths etc.)
    print("\n  Fetching original PENDING events for CRAM paths...")
    for sample in failed_samples:
        # Query for all events of this sample to find the original with cram paths
        resp = table.query(
            KeyConditionExpression='pk = :pk',
            ExpressionAttributeValues={':pk': sample['pk']}
        )
        items = resp.get('Items', [])
        
        # Look for METADATA record (has cram paths) or earliest PENDING GSE event
        for item in items:
            sk = item.get('sk', '')
            info = item.get('Info', {})
            
            # Get cram paths from METADATA or any record that has them
            if info.get('cram_path') or info.get('cram_s3_uri'):
                sample['cram_path'] = info.get('cram_path', '')
                sample['cram_index_path'] = info.get('cram_index_path', '')
                sample['dragen_vcf'] = info.get('dragen_vcf', '')
                sample['dragen_vcf_index'] = info.get('dragen_vcf_index', '')
                sample['cram_s3_uri'] = info.get('cram_s3_uri', '')
                sample['crai_s3_uri'] = info.get('crai_s3_uri', '')
                if not sample['batch_id_initial']:
                    sample['batch_id_initial'] = info.get('batch_id_initial', '')
                if not sample['gender']:
                    sample['gender'] = info.get('gender', '')
                break

    # Display
    print(f"\n{'='*80}")
    print(f"  Samples to redrive: {len(failed_samples)}")
    print(f"{'='*80}")
    for s in failed_samples[:20]:  # Show first 20
        print(f"  {s['sample_id']:20s} | batch: {s['batch_id_initial']:15s} | last failed: {s['latest_timestamp']}")
    if len(failed_samples) > 20:
        print(f"  ... and {len(failed_samples) - 20} more")
    print()

    if args.dry_run:
        print(f"[DRY RUN] Would send {len(failed_samples)} PENDING messages to SQS")
        return

    # Send PENDING messages to SQS
    print(f"Sending {len(failed_samples)} PENDING messages to SQS...")
    if args.delay > 0:
        print(f"  Delay: {args.delay}s between each message")
    if args.batch_delay > 0:
        print(f"  Batch delay: {args.batch_delay}s every {args.batch_size} messages")
    
    import time
    sent = 0
    for sample in failed_samples:
        timestamp = datetime.datetime.now(datetime.UTC).isoformat()
        
        info = {
            'batch_id_initial': sample.get('batch_id_initial', ''),
            'cram_path': sample.get('cram_path', ''),
            'cram_index_path': sample.get('cram_index_path', ''),
            'gender': sample.get('gender', ''),
            'redrive': True,
            'redrive_timestamp': timestamp,
        }
        
        # Include optional fields if present
        if sample.get('dragen_vcf'):
            info['dragen_vcf'] = sample['dragen_vcf']
        if sample.get('dragen_vcf_index'):
            info['dragen_vcf_index'] = sample['dragen_vcf_index']
        if sample.get('cram_s3_uri'):
            info['cram_s3_uri'] = sample['cram_s3_uri']
        if sample.get('crai_s3_uri'):
            info['crai_s3_uri'] = sample['crai_s3_uri']

        msg = {
            'SampleId': sample['sample_id'],
            'Event': 'GatherSampleEvidence',
            'Status': 'PENDING',
            'Timestamp': timestamp,
            'Info': info,
            'batch_id_initial': sample.get('batch_id_initial', ''),
            'gender': sample.get('gender', ''),
        }

        sqs.send_message(
            QueueUrl=args.queue_url,
            MessageBody=json.dumps(msg),
            MessageGroupId=sample['sample_id'],
            MessageDeduplicationId=f"{sample['sample_id']}_redrive_{timestamp}"
        )
        sent += 1
        if sent % 50 == 0:
            print(f"  Sent {sent}/{len(failed_samples)}...")
        
        # Per-message delay
        if args.delay > 0:
            time.sleep(args.delay)
        
        # Batch delay
        if args.batch_delay > 0 and sent % args.batch_size == 0:
            print(f"  Pausing {args.batch_delay}s after {sent} messages...")
            time.sleep(args.batch_delay)

    print(f"\n✓ Sent {sent} PENDING messages")
    print(f"  Samples will be picked up by the pipeline automatically.")


if __name__ == '__main__':
    main()
