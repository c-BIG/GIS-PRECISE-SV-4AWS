#!/usr/bin/env python3
"""
Scan DynamoDB sample table and report lifecycle status for all samples.

Determines current state per sample by finding the latest event record (by sk).
Groups and counts by derived status.

Lifecycle states:
    REGISTERED → RESTORING → RESTORED → GSE_PENDING → GSE_RUNNING → GSE_COMPLETED → ARCHIVED
    (also: GSE_FAILED, RESTORE_FAILED)

Usage:
    # Summary only
    python3 scan_ddb_status.py --table gatk-sv-dev-sample --profile npm

    # Export all samples with their current state to CSV
    python3 scan_ddb_status.py --table gatk-sv-dev-sample --profile npm --output status.csv

    # Filter to specific states
    python3 scan_ddb_status.py --table gatk-sv-dev-sample --profile npm --status GSE_FAILED RESTORE_FAILED

    # Group by batch
    python3 scan_ddb_status.py --table gatk-sv-dev-sample --profile npm --group-by-batch
"""

import argparse
import boto3
import csv
import sys
from collections import defaultdict


def full_scan(table):
    """Scan entire table, return all items."""
    items = []
    scan_kwargs = {}
    page = 0
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get('Items', []))
        page += 1
        if page % 10 == 0:
            print(f"  Scanning... page {page}, {len(items)} items", file=sys.stderr)
        if 'LastEvaluatedKey' not in response:
            break
        scan_kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']
    print(f"  Scan complete: {len(items)} total items", file=sys.stderr)
    return items


def derive_status(sk, item):
    """Derive lifecycle status from the sort key and item attributes."""
    if sk == 'METADATA':
        return 'REGISTERED'

    prefix = sk.split('#')[0]

    if prefix in ('RESTORING', 'RESTORED', 'ARCHIVED', 'RESTORE_FAILED'):
        return prefix

    if prefix == 'GatherSampleEvidence':
        # Further split by Status attribute on the event record
        event_status = item.get('Status', 'UNKNOWN')
        return f"GSE_{event_status.upper()}"

    # Other pipeline stages (EvidenceQC, TrainGCNV, etc.)
    event_status = item.get('Status', 'UNKNOWN')
    return f"{prefix}_{event_status.upper()}" if event_status != 'UNKNOWN' else prefix


def compute_sample_states(items):
    """For each sample, determine current state from latest event record."""
    # Group by pk
    by_sample = defaultdict(list)
    metadata = {}

    for item in items:
        pk = item.get('pk', '')
        sk = item.get('sk', '')

        if not pk.startswith('SAMPLE#'):
            continue

        sample_id = pk.replace('SAMPLE#', '')

        if sk == 'METADATA':
            metadata[sample_id] = item
        else:
            by_sample[sample_id].append(item)

    # Collect all known sample_ids (from METADATA or event records)
    all_sample_ids = set(metadata.keys()) | set(by_sample.keys())

    # For each sample, find latest event (lexicographically largest sk)
    results = []
    for sample_id in all_sample_ids:
        meta = metadata.get(sample_id, {})
        events = by_sample.get(sample_id, [])

        if not events:
            # Only METADATA exists — still at REGISTERED
            results.append({
                'sample_id': sample_id,
                'current_status': 'REGISTERED',
                'latest_sk': 'METADATA',
                'batch_id': meta.get('batch_id_initial', ''),
                'gender': meta.get('gender', ''),
                'timestamp': meta.get('Timestamp', ''),
            })
        else:
            # Find latest event by sk (chronological since sk contains timestamp)
            latest = max(events, key=lambda x: x.get('sk', ''))
            status = derive_status(latest['sk'], latest)
            results.append({
                'sample_id': sample_id,
                'current_status': status,
                'latest_sk': latest['sk'],
                'batch_id': latest.get('batch_id_initial', meta.get('batch_id_initial', '')),
                'gender': meta.get('gender', ''),
                'timestamp': latest.get('Timestamp', ''),
            })

    return results


def print_summary(results, group_by_batch=False):
    """Print status breakdown."""
    if group_by_batch:
        # Group by batch then by status
        by_batch = defaultdict(lambda: defaultdict(int))
        for r in results:
            by_batch[r['batch_id']][r['current_status']] += 1

        print(f"\n{'═' * 70}", file=sys.stderr)
        print(f"  SAMPLE LIFECYCLE BY BATCH", file=sys.stderr)
        print(f"{'═' * 70}", file=sys.stderr)

        for batch_id in sorted(by_batch.keys()):
            statuses = by_batch[batch_id]
            total = sum(statuses.values())
            print(f"\n  {batch_id} ({total} samples):", file=sys.stderr)
            for status in sorted(statuses.keys()):
                count = statuses[status]
                print(f"    {status:<20} {count:>6}", file=sys.stderr)

        print(f"\n{'═' * 70}\n", file=sys.stderr)
    else:
        # Simple status counts
        counts = defaultdict(int)
        for r in results:
            counts[r['current_status']] += 1

        # Display order
        order = ['REGISTERED', 'RESTORING', 'RESTORED', 'RESTORE_FAILED',
                 'GSE_PENDING', 'GSE_RUNNING', 'GSE_COMPLETED', 'GSE_FAILED',
                 'ARCHIVED']

        print(f"\n{'═' * 55}", file=sys.stderr)
        print(f"  SAMPLE LIFECYCLE SUMMARY", file=sys.stderr)
        print(f"{'═' * 55}", file=sys.stderr)

        total = len(results)
        for status in order:
            if status in counts:
                pct = counts[status] / total * 100 if total > 0 else 0
                bar = '█' * int(pct / 2)
                print(f"  {status:<20} {counts[status]:>6}  ({pct:5.1f}%)  {bar}", file=sys.stderr)

        # Any statuses not in predefined order
        for status in sorted(counts.keys()):
            if status not in order:
                pct = counts[status] / total * 100 if total > 0 else 0
                bar = '█' * int(pct / 2)
                print(f"  {status:<20} {counts[status]:>6}  ({pct:5.1f}%)  {bar}", file=sys.stderr)

        print(f"{'─' * 55}", file=sys.stderr)
        print(f"  {'TOTAL':<20} {total:>6}", file=sys.stderr)
        print(f"{'═' * 55}\n", file=sys.stderr)


def write_csv(results, output_path, status_filter=None):
    """Write per-sample status to CSV."""
    if status_filter:
        results = [r for r in results if r['current_status'] in status_filter]

    if not results:
        print("No items to write.", file=sys.stderr)
        return

    headers = ['sample_id', 'current_status', 'batch_id', 'gender', 'latest_sk', 'timestamp']

    with open(output_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for r in sorted(results, key=lambda x: (x['batch_id'], x['sample_id'])):
            writer.writerow(r)

    print(f"Written {len(results)} rows to {output_path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Scan DDB sample table for lifecycle status")
    parser.add_argument('--table', required=True, help='DynamoDB table name')
    parser.add_argument('--profile', default='npm', help='AWS profile')
    parser.add_argument('--region', default='ap-southeast-1', help='AWS region')
    parser.add_argument('--status', nargs='+', default=None,
                        help='Filter to specific statuses (e.g., --status GSE_FAILED RESTORE_FAILED)')
    parser.add_argument('--group-by-batch', action='store_true', help='Group results by batch_id')
    parser.add_argument('--output', '-o', default=None, help='Output CSV path (omit for summary only)')
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    dynamodb = session.resource('dynamodb')
    table = dynamodb.Table(args.table)

    print(f"Scanning {args.table}...", file=sys.stderr)
    items = full_scan(table)

    print(f"Computing per-sample states...", file=sys.stderr)
    results = compute_sample_states(items)

    # Filter if requested
    if args.status:
        filtered = [r for r in results if r['current_status'] in args.status]
        print(f"Filtered to {len(filtered)} samples matching {args.status}", file=sys.stderr)
        print_summary(filtered, group_by_batch=args.group_by_batch)
    else:
        print_summary(results, group_by_batch=args.group_by_batch)

    if args.output:
        write_csv(results, args.output, status_filter=args.status)


if __name__ == '__main__':
    main()
