#!/usr/bin/env python3
"""
Initialize and submit samples to GATK-SV pipeline.

Handles three modes:
  --mode hot       : CRAMs are in S3 standard storage. Sends SQS immediately to trigger GSE.
  --mode glacier   : CRAMs are in Glacier. Writes METADATA with restore_status=REGISTERED.
                     The glacier-restore-orchestrator will handle restore + SQS submission.
  --mode pending   : CRAMs don't exist yet (e.g., dragen still running on colleague's stack).
                     Writes METADATA with restore_status=AWAITING_DRAGEN.
                     External signal flips status to REGISTERED or sends SQS directly when ready.
  --mode redrive   : Resend SQS messages for failed samples. Does NOT write METADATA or
                     batch definitions to aggregates DDB. Use for retrying failed GSE runs.

In all modes:
  - Assigns gender-balanced batches
  - Writes BATCH# definitions to aggregates DDB (via SQS or direct)
  - Writes SAMPLE# METADATA records to sample DDB

CSV format (comma or tab separated):
    Required: sample_id, gender, cram, crai
    Optional: dragen_vcf, dragen_vcf_index, manta_vcf, manta_vcf_index, dragen_wgs_coverage_metrics

Usage:
    # Hot storage — send to pipeline immediately
    python3 submit_hot_samples_sqs.py --mode hot \
        --queue-url <sample-sqs-url> \
        --aggregates-queue-url <aggregates-sqs-url> \
        --csv-file samples.csv

    # Glacier — register for restore orchestrator
    python3 submit_hot_samples_sqs.py --mode glacier \
        --sample-table gatk-sv-dev-sample-BATCH \
        --aggregates-queue-url <aggregates-sqs-url> \
        --csv-file samples.csv

    # Pending dragen — register, await external signal
    python3 submit_hot_samples_sqs.py --mode pending \
        --sample-table gatk-sv-dev-sample-BATCH \
        --aggregates-queue-url <aggregates-sqs-url> \
        --csv-file samples.csv

    # Redrive failed GSE — resend SQS only, no DDB writes
    python3 submit_hot_samples_sqs.py --mode redrive \
        --queue-url <sample-sqs-url> \
        --csv-file failed_samples.csv
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
    parser = argparse.ArgumentParser(description='Initialize and submit samples to GATK-SV pipeline')
    parser.add_argument('--mode', required=True, choices=['hot', 'glacier', 'pending', 'waiting', 'redrive'],
                        help='hot=send SQS now, glacier=register for restore, pending=await dragen, waiting=write Event record with WAITING status (does not trigger GSE), redrive=resend SQS for failed samples (no METADATA write, no aggregates update)')
    parser.add_argument('--queue-url', help='SQS queue URL for sample events (required for hot/redrive mode)')
    parser.add_argument('--aggregates-queue-url', help='SQS queue URL for batch definitions (not used in redrive mode)')
    parser.add_argument('--sample-table', default='gatk-sv-dev-sample-BATCH', help='Sample DynamoDB table')
    parser.add_argument('--csv-file', required=True, help='CSV file with sample information')
    parser.add_argument('--target-batch-size', type=int, default=500, help='Target samples per batch')
    parser.add_argument('--min-batch-size', type=int, default=100, help='Minimum samples per batch')
    parser.add_argument('--batch-prefix', default='batch', help='Batch ID prefix')
    parser.add_argument('--batch-start-num', type=int, default=None, help='Starting batch number (auto-detect if not set)')
    parser.add_argument('--region', default='ap-southeast-1')
    parser.add_argument('--profile', default=None)
    parser.add_argument('--dry-run', action='store_true', help='Print actions without executing')
    args = parser.parse_args()

    if args.mode in ('hot', 'redrive') and not args.queue_url:
        parser.error("--queue-url is required for --mode hot/redrive")

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    sqs = session.client('sqs')
    dynamodb = session.resource('dynamodb')
    table = dynamodb.Table(args.sample_table)

    # Load CSV
    print(f"Loading samples from {args.csv_file}")
    samples = load_csv(args.csv_file, mode=args.mode)
    print(f"Loaded {len(samples)} samples")

    if len(samples) < args.min_batch_size:
        print(f"WARNING: Total samples ({len(samples)}) is less than min-batch-size ({args.min_batch_size})")

    # Assign batches
    start_num = args.batch_start_num if args.batch_start_num else 1
    batches = assign_batches(samples, args.target_batch_size, args.min_batch_size, args.batch_prefix, start_num)
    print(f"Assigned {len(samples)} samples to {len(batches)} batches")
    for bid, bsamples in batches.items():
        males = sum(1 for s in bsamples if s.get('gender', '').lower() in ('male', 'm', '1', 'xy'))
        females = len(bsamples) - males
        print(f"  {bid}: {len(bsamples)} samples (M:{males}, F:{females})")

    if args.dry_run:
        print(f"\n[DRY RUN] Would write {len(batches)} batch definitions and {len(samples)} sample records")
        print(f"[DRY RUN] Mode: {args.mode}")
        return

    # Redrive mode: only send SQS messages, skip METADATA and aggregates
    if args.mode == 'redrive':
        print(f"\nRedrive mode — skipping METADATA and aggregates writes")
        print(f"Sending {len(samples)} sample messages to {args.queue_url}...")
        send_sample_messages(sqs, args.queue_url, batches, status='PENDING')
        print(f"\nDone! Redrived {len(samples)} samples for GSE retry.")
        return

    # Write METADATA records to sample DDB (all modes except redrive)
    print(f"\nWriting METADATA records to {args.sample_table}...")
    write_metadata(table, batches, args.mode)

    # For glacier mode: write GLACIER_QUEUE entries for restore orchestrator
    if args.mode == 'glacier':
        print(f"Writing GLACIER_QUEUE entries...")
        write_glacier_queue(table, batches)

    # Send batch definitions to aggregates SQS
    if args.aggregates_queue_url:
        print(f"Sending batch definitions to aggregates SQS...")
        send_batch_definitions(sqs, args.aggregates_queue_url, batches)

    # For hot mode: also send sample messages to trigger GSE
    if args.mode == 'hot':
        print(f"Sending sample messages to {args.queue_url}...")
        send_sample_messages(sqs, args.queue_url, batches, status='PENDING')

    # For waiting mode: send same messages but with WAITING status (does not trigger GSE)
    if args.mode == 'waiting':
        if not args.queue_url:
            parser.error("--queue-url is required for --mode waiting")
        print(f"Sending sample messages (Status=WAITING) to {args.queue_url}...")
        send_sample_messages(sqs, args.queue_url, batches, status='WAITING')

    print(f"\nDone!")
    print(f"  Mode: {args.mode}")
    print(f"  Batches: {len(batches)}")
    print(f"  Samples: {len(samples)}")
    if args.mode == 'glacier':
        print(f"  Next: glacier-restore-orchestrator will process (runs hourly)")
    elif args.mode == 'pending':
        print(f"  Next: await external signal to flip restore_status → REGISTERED or send SQS directly")


def load_csv(csv_file, mode='hot'):
    with open(csv_file) as f:
        reader = csv.DictReader(f)
        samples = list(reader)

    if mode == 'pending':
        # Pending mode: only sample_id and gender required (files come from dragen-reanalysis later)
        required = {'sample_id', 'gender'}
        if not required.issubset(set(reader.fieldnames)):
            print(f"ERROR: CSV must have columns: {required}")
            print(f"  Got: {reader.fieldnames}")
            sys.exit(1)
        # Fill in empty cram/crai so downstream functions don't break
        for s in samples:
            s.setdefault('cram', '')
            s.setdefault('crai', '')
    else:
        required = {'sample_id', 'gender', 'cram', 'crai'}
        if not required.issubset(set(reader.fieldnames)):
            # Try alternate column names
            alt_required = {'sample_id', 'gender', 'cram_path', 'cram_index_path'}
            if alt_required.issubset(set(reader.fieldnames)):
                for s in samples:
                    s['cram'] = s.get('cram_path', s.get('cram', ''))
                    s['crai'] = s.get('cram_index_path', s.get('crai', ''))
            else:
                print(f"ERROR: CSV must have columns: {required} (or cram_path/cram_index_path)")
                print(f"  Got: {reader.fieldnames}")
                sys.exit(1)

    # Validate sample_id format: must be alphanumeric + underscores only (no hyphens, spaces, special chars)
    # Hyphens are used as delimiters in HealthOmics run names ({env}-{stage}-{sample_id}-r{retry})
    import re
    invalid_samples = [s['sample_id'] for s in samples if not re.match(r'^[A-Za-z0-9_]+$', s['sample_id'])]
    if invalid_samples:
        print(f"ERROR: sample_id must contain only alphanumeric characters and underscores (no hyphens, spaces, or special characters).")
        print(f"  Invalid sample_ids ({len(invalid_samples)}):")
        for sid in invalid_samples[:10]:
            print(f"    - '{sid}'")
        if len(invalid_samples) > 10:
            print(f"    ... and {len(invalid_samples) - 10} more")
        sys.exit(1)

    return samples


def assign_batches(samples, target_size, min_size, prefix, start_num):
    """Assign samples to gender-balanced batches with runt-batch handling.
    Samples are sorted lexicographically by sample_id first, so early batches
    correspond to early dragen completions (colleague's stack processes in lex order).
    Within each batch, gender balance is maintained by interleaving M/F.
    """

    # Sort by sample_id lexicographically
    samples_sorted = sorted(samples, key=lambda s: s['sample_id'])

    # Build batches sequentially, interleaving genders within each batch window
    raw_batches = []
    for i in range(0, len(samples_sorted), target_size):
        window = samples_sorted[i:i + target_size]
        # Interleave genders within this window for balanced distribution
        males = [s for s in window if s.get('gender', '').lower() in ('male', 'm', '1', 'xy')]
        females = [s for s in window if s.get('gender', '').lower() in ('female', 'f', '2', 'xx')]
        unknown = [s for s in window if s not in males and s not in females]
        interleaved = []
        m_idx = f_idx = u_idx = 0
        while m_idx < len(males) or f_idx < len(females) or u_idx < len(unknown):
            if m_idx < len(males):
                interleaved.append(males[m_idx])
                m_idx += 1
            if f_idx < len(females):
                interleaved.append(females[f_idx])
                f_idx += 1
            if u_idx < len(unknown):
                interleaved.append(unknown[u_idx])
                u_idx += 1
        raw_batches.append(interleaved)

    # Handle runt batch
    if len(raw_batches) > 1 and len(raw_batches[-1]) < min_size:
        # Merge last two batches and split evenly
        combined = raw_batches[-2] + raw_batches[-1]
        half = len(combined) // 2
        raw_batches[-2] = combined[:half]
        raw_batches[-1] = combined[half:]

        # Verify both are above min
        if len(raw_batches[-2]) < min_size or len(raw_batches[-1]) < min_size:
            print(f"WARNING: After rebalancing, batches are still below min-batch-size "
                  f"({len(raw_batches[-2])}, {len(raw_batches[-1])}). "
                  f"Consider reducing --min-batch-size or adding more samples.")

    # Validate gender mix
    for i, batch in enumerate(raw_batches):
        batch_males = sum(1 for s in batch if s.get('gender', '').lower() in ('male', 'm', '1', 'xy'))
        batch_females = len(batch) - batch_males
        if batch_males == 0 or batch_females == 0:
            print(f"WARNING: Batch {i + 1} is single-gender (M:{batch_males}, F:{batch_females})")

    # Assign batch IDs
    batches = {}
    for i, batch_samples in enumerate(raw_batches):
        batch_id = f"{prefix}_{start_num + i:04d}"
        batches[batch_id] = batch_samples

    return batches


def write_metadata(table, batches, mode):
    """Write METADATA records to sample DDB."""
    # Status reflects where the sample is in the full lifecycle
    status_map = {
        'hot': 'PENDING',           # Going straight to GSE
        'glacier': 'REGISTERED',    # Awaiting glacier restore
        'pending': 'REGISTERED',    # Awaiting dragen completion
        'waiting': 'REGISTERED'     # Registered, waiting for external signal
    }
    status = status_map[mode]
    timestamp = datetime.datetime.now(datetime.UTC).isoformat()
    written = 0

    for batch_id, batch_samples in batches.items():
        for sample in batch_samples:
            info = {
                'sample_id': sample['sample_id'],
            }

            # Store file paths in Info so restore_complete_handler can forward them to GSE
            if sample.get('cram'):
                info['cram_path'] = sample['cram']
            if sample.get('crai'):
                info['cram_index_path'] = sample['crai']
            if sample.get('dragen_vcf'):
                info['dragen_vcf'] = sample['dragen_vcf']
            if sample.get('dragen_vcf_index'):
                info['dragen_vcf_index'] = sample['dragen_vcf_index']
            if sample.get('manta_vcf'):
                info['manta_vcf'] = sample['manta_vcf']
            if sample.get('manta_vcf_index'):
                info['manta_vcf_index'] = sample['manta_vcf_index']
            if sample.get('dragen_wgs_coverage_metrics'):
                info['dragen_wgs_coverage_metrics'] = sample['dragen_wgs_coverage_metrics']
            if sample.get('melt_preprocess_s3_uri'):
                info['melt_preprocess_s3_uri'] = sample['melt_preprocess_s3_uri']

            item = {
                'pk': f"SAMPLE#{sample['sample_id']}",
                'sk': 'METADATA',
                'gender': sample.get('gender', ''),
                'batch_id_initial': batch_id,
                'Status': status,
                'Timestamp': timestamp,
                'Info': info,
            }

            table.put_item(Item=item)
            written += 1
            if written % 200 == 0:
                print(f"  Written {written} METADATA records...")

    print(f"  Written {written} METADATA records total")


def write_glacier_queue(table, batches):
    """Write GLACIER_QUEUE entries for restore orchestrator to pick up.
    Sort key is batch_id#sample_id so samples in the same batch are processed together.
    Orchestrator queries pk=GLACIER_QUEUE with Limit, processes in batch order, deletes after restore.
    """
    timestamp = datetime.datetime.now(datetime.UTC).isoformat()
    written = 0
    for batch_id, batch_samples in batches.items():
        for sample in batch_samples:
            table.put_item(Item={
                'pk': 'GLACIER_QUEUE',
                'sk': f"{batch_id}#{sample['sample_id']}",
                'sample_id': sample['sample_id'],
                'batch_id': batch_id,
                'Status': 'TO_RESTORE',
                'Timestamp': timestamp,
            })
            written += 1
            if written % 200 == 0:
                print(f"  Written {written} GLACIER_QUEUE entries...")
    print(f"  Written {written} GLACIER_QUEUE entries total")


def send_batch_definitions(sqs, queue_url, batches):
    """Send BATCH# definitions to aggregates SQS."""
    for batch_id, batch_samples in batches.items():
        timestamp = datetime.datetime.now(datetime.UTC).isoformat()
        msg = {
            'pk': f'BATCH#{batch_id}',
            'Event': 'GatherSampleEvidence',
            'Status': 'PENDING',
            'Timestamp': timestamp,
            'Count_Total_Entities': len(batch_samples),
            'Count_Completed_Entities': 0,
            'Count_Failed_Entities': 0,
            'Info': {
                'batch_id_initial': batch_id,
                'male_count': sum(1 for s in batch_samples if s.get('gender', '').lower() in ('male', 'm', '1', 'xy')),
                'female_count': sum(1 for s in batch_samples if s.get('gender', '').lower() in ('female', 'f', '2', 'xx')),
                'entity_member': [s['sample_id'] for s in batch_samples]
            }
        }
        sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(msg),
            MessageGroupId=f"BATCH_DEF_{batch_id}",
            MessageDeduplicationId=f"BATCH_DEF_{batch_id}_{timestamp}"
        )
    print(f"  Sent {len(batches)} batch definitions")


def send_sample_messages(sqs, queue_url, batches, status='PENDING'):
    """Send sample messages to SQS to trigger GatherSampleEvidence."""
    sent = 0
    for batch_id, batch_samples in batches.items():
        for sample in batch_samples:
            msg = build_sample_message(sample, batch_id, status)
            timestamp = datetime.datetime.now(datetime.UTC).isoformat()
            sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(msg),
                MessageGroupId=sample['sample_id'],
                MessageDeduplicationId=f"{sample['sample_id']}_{timestamp}"
            )
            sent += 1
            if sent % 200 == 0:
                print(f"  Sent {sent} sample messages...")
    print(f"  Sent {sent} sample messages total (Status={status})")


def build_sample_message(sample, batch_id, status='PENDING'):
    """Build SQS message matching LambdaUpdateDDB expected format."""
    info = {
        'batch_id_initial': batch_id,
        'cram_path': sample['cram'],
        'cram_index_path': sample['crai']
    }

    if sample.get('gender'):
        info['gender'] = sample['gender']
    if sample.get('dragen_vcf'):
        info['dragen_vcf'] = sample['dragen_vcf']
    if sample.get('dragen_vcf_index'):
        info['dragen_vcf_index'] = sample['dragen_vcf_index']
    if sample.get('manta_vcf'):
        info['manta_vcf'] = sample['manta_vcf']
    if sample.get('manta_vcf_index'):
        info['manta_vcf_index'] = sample['manta_vcf_index']
    if sample.get('dragen_wgs_coverage_metrics'):
        info['dragen_wgs_coverage_metrics'] = sample['dragen_wgs_coverage_metrics']
    if sample.get('dragen_wgs_coverage_metrics'):
        info['dragen_wgs_coverage_metrics'] = sample['dragen_wgs_coverage_metrics']
    if sample.get('melt_preprocess_s3_uri'):
        info['melt_preprocess_s3_uri'] = sample['melt_preprocess_s3_uri']

    return {
        'SampleId': sample['sample_id'],
        'Event': 'GatherSampleEvidence',
        'Status': status,
        'Timestamp': datetime.datetime.now(datetime.UTC).isoformat(),
        'Info': info,
        # Top-level promotion for monitoring/scanning (Info fields kept for legacy)
        'batch_id_initial': batch_id,
        'gender': sample.get('gender', ''),
    }


if __name__ == '__main__':
    main()
