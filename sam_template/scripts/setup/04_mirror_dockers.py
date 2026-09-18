#!/usr/bin/env python3
"""
Mirror GATK-SV Docker images from Broad's public registry to your account's ECR.

Targets GATK-SV v1.1. All images come straight from
Broad's public registry (us.gcr.io / marketplace.gcr.io / google/cloud-sdk).
No custom rebuilds needed for CRAM 3.0 data.

Source (pinned to the stable v1.1 tag — do NOT use main, it moves):
  https://raw.githubusercontent.com/broadinstitute/gatk-sv/v1.1/inputs/values/dockers.json

The script pulls each Broad image, retags for your ECR, pushes, and (optionally)
writes a docker_images.json pointing at your ECR plus the AWS-specific
manifest_reader_docker (needed by the HealthOmics ReadManifest task).

Usage:
    python3 04_mirror_dockers.py \
        --account-id 123456789012 --region ap-southeast-1 --profile npm --write-config

    # Mirror from a local list instead of Broad's v1.1 (e.g. re-mirroring between ECRs):
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
# NOTE: this repo targets v1.1 (NOT v1.1.1). v1.1.1 = v1.1 + a BND/END
# representation change (Broad PR #835); this deployment stays on v1.1.
BROAD_VERSION_TAG = "v1.1"
BROAD_DOCKERS_URL = (
    f"https://raw.githubusercontent.com/broadinstitute/gatk-sv/"
    f"{BROAD_VERSION_TAG}/inputs/values/dockers.json"
)

CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), '..', '..',
    'gatksv_healthomics', 'shared', 'python', 'config', 'docker_images.json'
)

# Point-of-reference template (placeholders), regenerated alongside the filled
# config so it never drifts from what was actually mirrored. Consumed by
# 08_update_configs.py to re-point account/region without re-mirroring.
TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), 'docker_images.v1.1.template.json'
)

# AWS-specific image NOT in Broad's list — required by the HealthOmics ReadManifest task.
# Build/push separately (amazonlinux + aws cli). See dockerfile/dockerfile.awscli.
MANIFEST_READER_REPO = "gatk-sv/awscli"
MANIFEST_READER_TAG = "v2.34.25"


# Images known to be unavailable without special access (e.g. licensing).
# Failures on these are expected and downgraded to warnings in the summary.
# Match is a substring test against the source URI.
KNOWN_RESTRICTED = {
    'talkowski-sv-gnomad/melt': (
        'MELT is not freely redistributable due to licensing. '
        'Obtain access to the source image or build/host it yourself, '
        'then re-mirror with --source-config.'
    ),
}


def restricted_reason(src):
    """Return a human note if src is a known-restricted image, else None."""
    for needle, note in KNOWN_RESTRICTED.items():
        if needle in src:
            return note
    return None


# Docker keys present in Broad's dockers.json but not referenced by any WDL input
# template ({{ dockers.<key> }}) or WDL task. Verified against
# inputs/templates/*.tmpl and wdl/*.wdl in the gatk-sv repo. Dropped from the
# mirror by default so we don't push images nothing uses.
UNUSED_DOCKER_KEYS = {
    'cnmops-virtual-env',
    'samtools-cloud-virtual-env',
    'sv-base-virtual-env',
    'sv-pipeline-virtual-env',
    'sv-utils-env',
    'str',
    'denovo',
    'sv-shell',
}


def run(cmd, dry_run=False):
    print(f"  $ {' '.join(cmd)}")
    if not dry_run:
        # Let stdout stream to the terminal (so docker pull/push progress is
        # visible on long operations) but capture stderr so a failure can be
        # summarized with context in the final report.
        proc = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, cmd, stderr=proc.stderr
            )


def cleanup_local_images(image_refs):
    """Best-effort removal of local docker images to bound disk usage between
    mirrors (layers are already safely in ECR after push). Removes the given
    tags, then prunes dangling layers. Never raises — cleanup failures must not
    abort the mirror run.
    """
    for ref in image_refs:
        if not ref:
            continue
        subprocess.run(['docker', 'rmi', '-f', ref],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Drop now-dangling layers freed by the rmi calls above.
    subprocess.run(['docker', 'image', 'prune', '-f'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("  (cleaned up local images to free disk)")


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
    # Drop docker keys that Broad defines but that no WDL input template or task
    # actually references (verified against inputs/templates/*.tmpl and wdl/*.wdl).
    # These are vestigial entries; mirroring them wastes pull/push time and ECR
    # storage. Pass --keep-unused to mirror the full Broad set anyway.
    if not args.keep_unused:
        dropped = [k for k in UNUSED_DOCKER_KEYS if k in data]
        for k in dropped:
            data.pop(k, None)
        if dropped:
            print(f"Skipping {len(dropped)} unused docker key(s): {', '.join(sorted(dropped))}")
            print("  (pass --keep-unused to mirror them anyway)")
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
    # Ensure the AWS HealthOmics service principal can pull images from this repo.
    # HealthOmics pulls container images via the omics.amazonaws.com service
    # principal (not only via the run's execution role), so each private ECR repo
    # needs this resource-based policy. Applied on every run (idempotent).
    apply_omics_repo_policy(repo_name, region, profile, dry_run)


# Repository policy granting AWS HealthOmics read/pull access. Required for
# private ECR images used by HealthOmics runs.
OMICS_REPO_POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "omics workflow access",
            "Effect": "Allow",
            "Principal": {"Service": "omics.amazonaws.com"},
            "Action": [
                "ecr:GetDownloadUrlForLayer",
                "ecr:BatchGetImage",
                "ecr:BatchCheckLayerAvailability",
            ],
        }
    ],
})


def apply_omics_repo_policy(repo_name, region, profile, dry_run):
    print(f"  Setting omics repository policy on {repo_name}")
    if dry_run:
        return
    subprocess.run(
        ['aws', 'ecr', 'set-repository-policy',
         '--repository-name', repo_name,
         '--policy-text', OMICS_REPO_POLICY,
         '--region', region, '--profile', profile],
        check=True, capture_output=True
    )


def fix_all_repo_policies(region, profile, dry_run):
    """Apply the HealthOmics repo policy to every existing gatk-sv/* ECR repo.
    Use to retrofit repos created before the policy was added."""
    print("Listing ECR repositories...")
    out = subprocess.run(
        ['aws', 'ecr', 'describe-repositories',
         '--query', 'repositories[].repositoryName', '--output', 'json',
         '--region', region, '--profile', profile],
        check=True, capture_output=True, text=True
    ).stdout
    repos = [r for r in json.loads(out) if r.startswith('gatk-sv/')]
    print(f"Applying omics policy to {len(repos)} gatk-sv/* repos\n")
    for repo in sorted(repos):
        print(f"[{repo}]")
        apply_omics_repo_policy(repo, region, profile, dry_run)
    print(f"\nDone ({len(repos)} repos).")


def src_to_repo_tag(src):
    """Derive an ECR repo path + tag from a source image URI.
    All images are namespaced under gatk-sv/ in ECR so they're easy to find
    and manage together (e.g. gatk-sv/cnmops, gatk-sv/gatk, gatk-sv/ubuntu1804).
    """
    if ':' in src.rsplit('/', 1)[-1]:
        path, tag = src.rsplit(':', 1)
    else:
        path, tag = src, 'latest'
    parts = path.split('/')
    if '.' in parts[0] or parts[0] in ('marketplace.gcr.io',):
        parts = parts[1:]
    KNOWN_ORGS = {'broad-dsde-methods', 'broad-gotc-prod', 'talkowski-sv-gnomad',
                  'vjalili', 'markw', 'eph', 'tsharpe', 'google'}
    # Strip all leading org/registry-owner segments (some URIs nest two, e.g.
    # broad-dsde-methods/tsharpe/gatk) so images flatten under one namespace.
    while parts and parts[0] in KNOWN_ORGS:
        parts = parts[1:]
    # Drop an existing leading 'gatk-sv' so we don't double it up, then always
    # prepend a single gatk-sv/ namespace.
    if parts and parts[0] == 'gatk-sv':
        parts = parts[1:]
    repo = 'gatk-sv/' + '/'.join(parts)
    return repo, tag


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--account-id', required=True)
    ap.add_argument('--region', default='ap-southeast-1')
    ap.add_argument('--profile', default='default')
    ap.add_argument('--source-config', default=None,
                    help='Local dockers.json to mirror (default: fetch Broad v1.1)')
    ap.add_argument('--write-config', action='store_true',
                    help='Write docker_images.json pointing at the new ECR')
    ap.add_argument('--keep-unused', action='store_true',
                    help='Mirror the full Broad set, including docker keys not '
                         'referenced by any WDL (default: skip unused keys)')
    ap.add_argument('--strict', action='store_true',
                    help='Exit non-zero if any image fails to mirror '
                         '(default: roll over failures and exit 0, '
                         'reporting them at the end)')
    ap.add_argument('--fix-policies', action='store_true',
                    help='Do not mirror; just (re)apply the HealthOmics ECR '
                         'repository policy to all existing gatk-sv/* repos')
    ap.add_argument('--no-cleanup', action='store_true',
                    help='Do NOT remove each local docker image after pushing '
                         '(default: remove + prune after each image to bound '
                         'local disk usage — useful on low-disk machines)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if args.fix_policies:
        fix_all_repo_policies(args.region, args.profile, args.dry_run)
        return

    source = load_source(args)
    target_registry = f"{args.account_id}.dkr.ecr.{args.region}.amazonaws.com"

    ecr_login(args.account_id, args.region, args.profile, args.dry_run)

    unique = sorted(set(source.values()))
    print(f"\nMirroring {len(unique)} unique images to {target_registry}\n")

    src_to_dest = {}
    failed = []  # list of (src, reason, restricted_note_or_None)
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
            # Prefer the command's stderr for an actionable message.
            reason = (e.stderr or '').strip() or str(e)
            note = restricted_reason(src)
            if note:
                print(f"  SKIPPED (restricted): {note}\n")
            else:
                print(f"  FAILED: {reason}\n")
            failed.append((src, reason, note))
        finally:
            # Free local disk before the next image. The layers are safely in ECR
            # after push; on failure we still drop whatever was pulled so a
            # partial image doesn't accumulate. Disabled with --no-cleanup.
            if not args.no_cleanup and not args.dry_run:
                cleanup_local_images([dest, src])

    succeeded = [s for s in unique if s not in {f[0] for f in failed}]
    restricted = [f for f in failed if f[2]]
    unexpected = [f for f in failed if not f[2]]

    print("=" * 60)
    print(f"Mirrored {len(succeeded)}/{len(unique)} images.")
    if restricted:
        print(f"\nSkipped {len(restricted)} restricted image(s) "
              f"(expected — action needed to obtain them):")
        for src, _reason, note in restricted:
            print(f"  - {src}")
            print(f"      {note}")
    if unexpected:
        print(f"\nFailed {len(unexpected)} image(s) — investigate:")
        for src, reason, _note in unexpected:
            # Show a compact one-line reason; full output was printed above.
            first_line = reason.splitlines()[0] if reason else '(no error output)'
            print(f"  - {src}")
            print(f"      {first_line}")

    if args.write_config and not args.dry_run:
        failed_srcs = {f[0] for f in failed}
        new_config = {}
        kept_source_uris = []  # keys whose image wasn't mirrored
        for key, src in source.items():
            if src in failed_srcs:
                # Image was never pushed to ECR; keep the original source URI
                # so the config doesn't silently point at a non-existent repo.
                new_config[key] = src
                kept_source_uris.append((key, src))
            else:
                new_config[key] = src_to_dest.get(src, src)
        new_config['manifest_reader_docker'] = (
            f"{target_registry}/{MANIFEST_READER_REPO}:{MANIFEST_READER_TAG}"
        )
        with open(CONFIG_PATH, 'w') as f:
            json.dump(new_config, f, indent=2)
        print(f"\nWrote {CONFIG_PATH} ({len(new_config)} entries)")
        if kept_source_uris:
            print(f"  WARNING: {len(kept_source_uris)} entry(ies) still point at "
                  f"the original (un-mirrored) source URI because the mirror failed:")
            for key, src in kept_source_uris:
                print(f"    {key} -> {src}")
            print("  Mirror these before running workflows that need them.")

        # Also (re)generate the placeholder template so it stays in lockstep with
        # the mirrored set. Broad's dockers.json (pinned tag) remains the single
        # source of truth; this template is a derived, committed artifact.
        placeholder_registry = "<ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com"
        template = {
            "_comment": (
                f"GENERATED by 04_mirror_dockers.py from Broad dockers.json "
                f"({BROAD_VERSION_TAG}). Do not hand-edit; re-run 04 --write-config "
                f"to regenerate. Point of reference for 08_update_configs.py. "
                f"Contains only images referenced by GATK-SV WDL (vestigial Broad "
                f"keys dropped) plus the AWS-specific manifest_reader_docker. "
                f"Replace <ACCOUNT>/<REGION> via 08. All images namespaced under "
                f"gatk-sv/ in ECR."
            )
        }
        for key, dest in new_config.items():
            # Swap the concrete registry for placeholders. Failed (un-mirrored)
            # entries kept their source URI, which has no ECR registry to swap —
            # for those, emit the placeholder ECR path we *would* mirror to.
            if dest.startswith(target_registry):
                template[key] = dest.replace(target_registry, placeholder_registry, 1)
            else:
                repo, tag = src_to_repo_tag(dest)
                template[key] = f"{placeholder_registry}/{repo}:{tag}"
        with open(TEMPLATE_PATH, 'w') as f:
            json.dump(template, f, indent=2)
        print(f"Wrote {TEMPLATE_PATH} ({len(template) - 1} entries, placeholders)")

        print("NOTE: build + push manifest_reader_docker separately "
              "(dockerfile/dockerfile.awscli).")

    if failed:
        # By default, roll over failures (exit 0) so expected gaps like the
        # licensed MELT image don't fail the whole setup. Use --strict to fail.
        if args.strict:
            sys.exit(1)
        if unexpected:
            # Even in non-strict mode, surface a soft nonzero-ish hint via message
            # but keep exit 0 so downstream automation can continue.
            print("\n(Continuing despite failures — rerun with --strict to "
                  "treat failures as fatal.)")


if __name__ == '__main__':
    main()
