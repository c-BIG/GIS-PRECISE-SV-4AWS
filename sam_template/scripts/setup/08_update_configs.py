#!/usr/bin/env python3
"""
Update config files for a new AWS account:
  - docker_images.json    : rewrite ECR account ID in all URIs
  - genome_references.json : rewrite S3 bucket + prefix
  - config_grids/templates/FilterGenotypes.json : rewrite hardcoded gatk_docker override
  - (optional) template.yaml : strip AllowedValues constraints on account-specific params

Usage:
    python3 08_update_configs.py \
        --account-id 123456789012 \
        --region ap-southeast-1 \
        --ref-bucket my-bucket \
        --ref-prefix genome/gatk-sv \
        [--old-ref-bucket <old-bucket>] \
        [--old-ref-prefix genome/gatk-sv] \
        [--patch-template]
"""

import argparse
import json
import os
import re

BASE = os.path.join(os.path.dirname(__file__), '..', '..')
CONFIG_DIR = os.path.join(BASE, 'gatksv_healthomics', 'shared', 'python', 'config')
GRIDS_DIR = os.path.join(BASE, 'gatksv_healthomics', 'shared', 'python', 'config_grids', 'templates')
TEMPLATE = os.path.join(BASE, 'template.yaml')


def update_docker_images(account_id, region):
    path = os.path.join(CONFIG_DIR, 'docker_images.json')
    with open(path) as f:
        data = json.load(f)

    new_registry = f"{account_id}.dkr.ecr.{region}.amazonaws.com"
    changed = 0
    for key, uri in data.items():
        new_uri = re.sub(
            r'\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com',
            new_registry, uri
        )
        if new_uri != uri:
            data[key] = new_uri
            changed += 1

    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"docker_images.json: rewrote {changed} URIs -> {new_registry}")


def update_genome_references(ref_bucket, ref_prefix, old_bucket, old_prefix):
    path = os.path.join(CONFIG_DIR, 'genome_references.json')
    if not old_bucket:
        print("genome_references.json: --old-ref-bucket not provided; skipped "
              "(re-run with --old-ref-bucket <existing-bucket> to rewrite S3 paths)")
        return
    with open(path) as f:
        data = json.load(f)

    old_base = f"s3://{old_bucket}/{old_prefix}"
    new_base = f"s3://{ref_bucket}/{ref_prefix}"
    changed = 0
    for key, val in data.items():
        if not (isinstance(val, str) and val.startswith('s3://')):
            continue
        if val.startswith(old_base):
            data[key] = val.replace(old_base, new_base, 1)
            changed += 1
        elif val.startswith(f"s3://{old_bucket}/"):
            data[key] = val.replace(f"s3://{old_bucket}/", f"s3://{ref_bucket}/", 1)
            changed += 1

    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"genome_references.json: rewrote {changed} paths -> {new_base}")


def update_filtergenotypes(account_id, region):
    path = os.path.join(GRIDS_DIR, 'FilterGenotypes.json')
    if not os.path.exists(path):
        print(f"FilterGenotypes.json not found at {path} — skipping")
        return
    with open(path) as f:
        data = json.load(f)

    new_registry = f"{account_id}.dkr.ecr.{region}.amazonaws.com"
    opt = data.get('optional_params', {})
    if 'gatk_docker' in opt:
        opt['gatk_docker'] = re.sub(
            r'\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com',
            new_registry, opt['gatk_docker']
        )
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"FilterGenotypes.json: rewrote gatk_docker override -> {new_registry}")


def patch_template():
    """Strip AllowedValues blocks from account-specific parameters for portability."""
    with open(TEMPLATE) as f:
        lines = f.readlines()

    targets = {
        'DynamoDBName', 'DynamoDBArn', 'AggregatesDynamoDBName',
        'AggregatesDynamoDBArn', 'CramBucketName', 'Byob',
        'ByobOutput', 'ByobParameter', 'ByobConfig'
    }

    out = []
    i = 0
    in_target = False
    param_indent = None
    while i < len(lines):
        line = lines[i]
        m = re.match(r'^(\s+)(\w+):\s*$', line)
        if m and m.group(2) in targets:
            in_target = True
            param_indent = len(m.group(1))
            out.append(line)
            i += 1
            continue

        if in_target:
            if re.match(r'^\s+AllowedValues:\s*$', line):
                i += 1
                while i < len(lines):
                    nxt = lines[i]
                    if re.match(r'^\s+-\s', nxt):
                        i += 1
                        continue
                    break
                continue
            m2 = re.match(r'^(\s+)(\w+):', line)
            if m2 and len(m2.group(1)) <= param_indent and m2.group(2) not in ('Type', 'Description', 'Default', 'AllowedValues', 'NoEcho'):
                in_target = False
        out.append(line)
        i += 1

    backup = TEMPLATE + '.bak_allowedvalues'
    with open(backup, 'w') as f:
        f.writelines(lines)
    with open(TEMPLATE, 'w') as f:
        f.writelines(out)
    print(f"template.yaml: stripped AllowedValues from account-specific params (backup: {backup})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--account-id', required=True)
    ap.add_argument('--region', default='ap-southeast-1')
    ap.add_argument('--ref-bucket', required=True)
    ap.add_argument('--ref-prefix', default='genome/gatk-sv')
    ap.add_argument('--old-ref-bucket', default='',
                    help='Existing bucket in the config to replace (leave empty to rely on docker regex only)')
    ap.add_argument('--old-ref-prefix', default='genome/gatk-sv')
    ap.add_argument('--patch-template', action='store_true',
                    help='Strip AllowedValues constraints from template.yaml')
    args = ap.parse_args()

    update_docker_images(args.account_id, args.region)
    update_genome_references(args.ref_bucket, args.ref_prefix, args.old_ref_bucket, args.old_ref_prefix)
    update_filtergenotypes(args.account_id, args.region)
    if args.patch_template:
        patch_template()

    print("\nConfig updated. Review the files, then run 09_build_config_layer.sh + sync_config_to_s3.sh")


if __name__ == '__main__':
    main()
