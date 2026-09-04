#!/usr/bin/env python3
"""
Mirror GATK-SV Docker images from Broad's public registry to your account's ECR.

Targets GATK-SV v1.1.1. All images come straight from
Broad's public registry (us.gcr.io / marketplace.gcr.io / google/cloud-sdk).
No custom rebuilds needed for CRAM 3.0 data.

Source (pinned to the stable v1.1.1 tag — do NOT use main, it moves):
  https://raw.githubusercontent.com/broadinstitute/gatk-sv/v1.1.1/inputs/values/dockers.json

The script pulls each Broad image, retags for your ECR, pushes, and (optionally)
writes a docker_images.json pointing at your ECR plus the AWS-specific
manifest_reader_docker (needed by the HealthOmics ReadManifest task).

Usage:
    python3 04_mirror_dockers.py \
        --account-id 123456789012 --region ap-southeast-1 --profile npm --write-config

    # Mirror from a local list instead of Broad's v1.1.1 (e.g. re-mirroring between ECRs):
    python3 04_mirror_dockers.py --source-config path/to/dockers.json ...

    # --dry-run to preview without pulling/pushing.

Requires: docker, aws cli, boto3.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request

# Pinned to the stable release tag. Bump this only when intentionally upgrading.
BROAD_VERSION_TAG = "v1.1.1"
BROAD_DOCKERS_URL = (
    f"https://raw.githubusercontent.com/broadinstitute/gatk-sv/"
    f"{BROAD_VERSION_TAG}/inputs/values/dockers.json"
)

CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), '..', '..',
    'gatksv_healthomics', 'shared', 'python', 'config', 'docker_images.json'
)

# AWS-specific image NOT in Broad's list — required by the HealthOmics ReadManifest task.
# Build/push separately (amazonlinux + aws cli). See dockerfile/dockerfile.awscli.
MANIFEST_READER_REPO = "gatk-sv/awscli"
MANIFEST_READER_TAG = "v2.34.25"


def run(cmd, dry_run=False):
    print(f"  $ {' '.join(cmd)}")
    if not dry_run:
        subprocess.run(cmd, check=True)


def load_source(args):
    """Return the dict of docker key -> source image URI."""
    if args.source_config:
        with open(args.source_config) as f:
            data = json.load(f)
    else:
        print(f"Fetching Broad dockers.json ({BROAD_VERSION_TAG})...")
        with urllib.request.urlopen(BROAD_DOCKERS_URL) as resp:
            data = json.load(resp)
    data.pop('name', None)
    return data


def ecr_login(account_id, region, profile, dry_run):
    print("Logging in to ECR...")
    if dry_run:
        return
    pw = subprocess.run(
        ['aws', 'ecr', 'get-login-password', '--region', region, '--profile', profile],
        check=True, capture_output=True, text=True
    ).stdout.strip()
    registry = f"{account_id}.dkr.ecr.{region}.amazonaws.com"
    subprocess.run(
        ['docker', 'login', '--username', 'AWS', '--password-stdin', registry],
        input=pw, text=True, check=True
    )


def ensure_repo(repo_name, region, profile, dry_run):
    check = subprocess.run(
        ['aws', 'ecr', 'describe-repositories', '--repository-names', repo_name,
         '--region', region, '--profile', profile],
        capture_output=True
    )
    if check.returncode != 0:
        print(f"  Creating ECR repo: {repo_name}")
        if not dry_run:
            subprocess.run(
                ['aws', 'ecr', 'create-repository', '--repository-name', repo_name,
                 '--region', region, '--profile', profile],
                check=True, capture_output=True
            )


def src_to_repo_tag(src):
    """Derive an ECR repo path + tag from a source image URI.
    Preserves the gatk-sv/ namespace where present so images stay organized.
    """
    if ':' in src.rsplit('/', 1)[-1]:
        path, tag = src.rsplit(':', 1)
    else:
        path, tag = src, 'latest'
    parts = path.split('/')
    if '.' in parts[0] or parts[0] in ('marketplace.gcr.io',):
        parts = parts[1:]
    KNOWN_ORGS = {'broad-dsde-methods', 'broad-gotc-prod', 'talkowski-sv-gnomad',
                  'vjalili', 'markw', 'eph', 'tsharpe'}
    if parts and parts[0] in KNOWN_ORGS:
        parts = parts[1:]
    repo = '/'.join(parts)
    return repo, tag


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--account-id', required=True)
    ap.add_argument('--region', default='ap-southeast-1')
    ap.add_argument('--profile', default='default')
    ap.add_argument('--source-config', default=None,
                    help='Local dockers.json to mirror (default: fetch Broad v1.1.1)')
    ap.add_argument('--write-config', action='store_true',
                    help='Write docker_images.json pointing at the new ECR')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    source = load_source(args)
    target_registry = f"{args.account_id}.dkr.ecr.{args.region}.amazonaws.com"

    ecr_login(args.account_id, args.region, args.profile, args.dry_run)

    unique = sorted(set(source.values()))
    print(f"\nMirroring {len(unique)} unique images to {target_registry}\n")

    src_to_dest = {}
    failed = []
    for src in unique:
        repo, tag = src_to_repo_tag(src)
        dest = f"{target_registry}/{repo}:{tag}"
        src_to_dest[src] = dest
        print(f"[{src}]")
        try:
            ensure_repo(repo, args.region, args.profile, args.dry_run)
            run(['docker', 'pull', src], args.dry_run)
            run(['docker', 'tag', src, dest], args.dry_run)
            run(['docker', 'push', dest], args.dry_run)
            print(f"  -> {dest}\n")
        except subprocess.CalledProcessError as e:
            print(f"  FAILED: {e}\n")
            failed.append(src)

    print("=" * 60)
    print(f"Mirrored {len(unique) - len(failed)}/{len(unique)} images.")
    if failed:
        print("Failed:")
        for f in failed:
            print(f"  {f}")

    if args.write_config and not args.dry_run:
        new_config = {}
        for key, src in source.items():
            new_config[key] = src_to_dest.get(src, src)
        new_config['manifest_reader_docker'] = (
            f"{target_registry}/{MANIFEST_READER_REPO}:{MANIFEST_READER_TAG}"
        )
        with open(CONFIG_PATH, 'w') as f:
            json.dump(new_config, f, indent=2)
        print(f"\nWrote {CONFIG_PATH} ({len(new_config)} entries)")
        print("NOTE: build + push manifest_reader_docker separately "
              "(dockerfile/dockerfile.awscli).")

    if failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
