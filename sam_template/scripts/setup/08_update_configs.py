#!/usr/bin/env python3
"""
Generate account-specific config files from the versioned templates in this dir.

Point-of-reference templates (edit these, commit them — they hold placeholders):
  - scripts/setup/docker_images.v1.1.template.json  (<ACCOUNT>/<REGION>)
  - scripts/setup/genome_references.template.json      (<your-bucket>/<ref-prefix>)

Generated (account-filled) outputs, consumed by the runtime loader:
  - gatksv_healthomics/shared/python/config/docker_images.json
  - gatksv_healthomics/shared/python/config/genome_references.json
  - gatksv_healthomics/shared/python/config/templates/FilterGenotypes.json (gatk_docker override)
  - (optional) template.yaml : strip AllowedValues constraints on account-specific params

By default each output is (re)generated FROM its template, then placeholders are
substituted. Use --no-from-template to patch the existing output in place instead.

Usage:
    python3 08_update_configs.py \
        --account-id 123456789012 \
        --region ap-southeast-1 \
        --ref-bucket my-bucket \
        --ref-prefix genome/gatk-sv \
        [--no-from-template] \
        [--patch-template]
"""

import argparse
import json
import os
import re
import shutil

# Matches an ECR registry host in either form:
#   - public-template placeholder: <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com
#   - already-filled real values:  123456789012.dkr.ecr.ap-southeast-1.amazonaws.com
# So the setup script works on the public template (expected case) and is
# idempotent on re-runs against an already-filled file.
ECR_REGISTRY_RE = re.compile(
    r'(?:\d{12}|<ACCOUNT>)\.dkr\.ecr\.(?:[a-z0-9-]+|<REGION>)\.amazonaws\.com'
)

SETUP_DIR = os.path.dirname(__file__)
BASE = os.path.join(SETUP_DIR, '..', '..')
CONFIG_DIR = os.path.join(BASE, 'gatksv_healthomics', 'shared', 'python', 'config')
GRIDS_DIR = os.path.join(BASE, 'gatksv_healthomics', 'shared', 'python', 'config_grids', 'templates')
TEMPLATE = os.path.join(BASE, 'template.yaml')

# Point-of-reference templates (source of truth, hold placeholders).
DOCKER_TEMPLATE = os.path.join(SETUP_DIR, 'docker_images.v1.1.template.json')
GENOME_REFS_TEMPLATE = os.path.join(SETUP_DIR, 'genome_references.template.json')


def _seed_from_template(template_path, output_path, from_template):
    """If from_template and the template exists, copy it over the output so the
    output is regenerated from the versioned template before placeholder fill.
    Drops any leading '_comment' key after loading. Returns the loaded dict."""
    if from_template and os.path.exists(template_path):
        shutil.copyfile(template_path, output_path)
        print(f"  seeded {os.path.basename(output_path)} from {os.path.basename(template_path)}")
    with open(output_path) as f:
        data = json.load(f)
    data.pop('_comment', None)
    return data


def update_docker_images(account_id, region, from_template):
    path = os.path.join(CONFIG_DIR, 'docker_images.json')
    data = _seed_from_template(DOCKER_TEMPLATE, path, from_template)

    new_registry = f"{account_id}.dkr.ecr.{region}.amazonaws.com"
    changed = 0
    for key, uri in data.items():
        new_uri = ECR_REGISTRY_RE.sub(new_registry, uri)
        if new_uri != uri:
            data[key] = new_uri
            changed += 1

    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"docker_images.json: rewrote {changed} URIs -> {new_registry}")


def update_genome_references(ref_bucket, ref_prefix, from_template):
    path = os.path.join(CONFIG_DIR, 'genome_references.json')
    data = _seed_from_template(GENOME_REFS_TEMPLATE, path, from_template)

    # Template S3 paths use the placeholders s3://<your-bucket>/<ref-prefix>/...
    # Substitute the bucket and prefix placeholders directly. Also tolerate an
    # already-filled file on re-run: if the exact placeholder base isn't present,
    # fall back to swapping just the bucket segment.
    new_base = f"s3://{ref_bucket}/{ref_prefix}"
    ph_base = "s3://<your-bucket>/<ref-prefix>"

    def rewrite(val):
        if not (isinstance(val, str) and val.startswith('s3://')):
            return val, 0
        if val.startswith(ph_base):
            return val.replace(ph_base, new_base, 1), 1
        return val, 0

    def rewrite_deep(val):
        """Rewrite strings recursively through nested lists (e.g. list-of-lists
        like site_level_comparison_datasets)."""
        if isinstance(val, list):
            count = 0
            new_list = []
            for item in val:
                new_item, c = rewrite_deep(item)
                new_list.append(new_item)
                count += c
            return new_list, count
        return rewrite(val)

    changed = 0
    for key, val in data.items():
        new_val, c = rewrite_deep(val)
        if c:
            data[key] = new_val
            changed += c

    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"genome_references.json: rewrote {changed} paths -> {new_base}")


def update_filtergenotypes(account_id, region):
    # The file has lived under two layouts across versions; check both.
    candidates = [
        os.path.join(CONFIG_DIR, 'templates', 'FilterGenotypes.json'),
        os.path.join(GRIDS_DIR, 'FilterGenotypes.json'),
    ]
    path = next((p for p in candidates if os.path.exists(p)), None)
    if path is None:
        print(f"FilterGenotypes.json not found in {candidates} — skipping")
        return
    with open(path) as f:
        data = json.load(f)

    # The gatk_docker override should reference a docker_images.json key via
    # {{docker:...}} (resolved at runtime by parameter_builder), NOT a hardcoded
    # ECR URI — that keeps account/region out of this file entirely. If we find a
    # legacy hardcoded URI, migrate it to the canonical reference.
    opt = data.get('optional_params', {})
    val = opt.get('gatk_docker')
    if isinstance(val, str) and ECR_REGISTRY_RE.search(val):
        opt['gatk_docker'] = '{{docker:gq_recalibrator_docker}}'
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"FilterGenotypes.json ({path}): migrated hardcoded gatk_docker "
              f"override -> {{{{docker:gq_recalibrator_docker}}}}")
    else:
        print("FilterGenotypes.json: gatk_docker override already uses a "
              "docker-key reference (no ECR URI to rewrite)")


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
    ap.add_argument('--no-from-template', dest='from_template', action='store_false',
                    help='Patch the existing config files in place instead of '
                         'regenerating them from the setup/ templates')
    ap.add_argument('--patch-template', action='store_true',
                    help='Strip AllowedValues constraints from template.yaml')
    args = ap.parse_args()

    update_docker_images(args.account_id, args.region, args.from_template)
    update_genome_references(args.ref_bucket, args.ref_prefix, args.from_template)
    update_filtergenotypes(args.account_id, args.region)
    if args.patch_template:
        patch_template()

    print("\nConfig updated. Review the files, then run 09_build_config_layer.sh + sync_config_to_s3.sh")


if __name__ == '__main__':
    main()
