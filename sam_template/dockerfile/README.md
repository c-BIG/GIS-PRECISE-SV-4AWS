# dockerfile - manifest reader image

Builds the AWS CLI image used by the HealthOmics `ReadManifest` task. In the
config this is the `manifest_reader_docker` key, and it points at your own ECR
repo (e.g. `gatk-sv/awscli:<version>`).

This image is **AWS-specific** - it is not one of Broad's stock GATK-SV docker
images, so it must be built and pushed to your account, then referenced in
`docker_images.json`.

Parent stack + deployment: see [../README.md](../README.md) and [../../SETUP_GUIDE.md](../../SETUP_GUIDE.md).

---

## Why It's Needed

When a submit Lambda builds run parameters that exceed `HealthOmicsJsonSizeLimit`
(~50KB), it writes file manifests to S3 instead of inline arrays. The HealthOmics
`ReadManifest` WDL task pulls this image to read those manifests back with the AWS
CLI at workflow runtime.

## Contents

`dockerfile.awscli` is a thin wrapper over the official `amazon/aws-cli` image that
clears the entrypoint so HealthOmics can invoke arbitrary shell commands:

```dockerfile
FROM amazon/aws-cli:latest
ENTRYPOINT []
CMD ["/bin/sh"]
RUN aws --version
```

A smaller `amazonlinux:2023-minimal` + `dnf install aws-cli` alternative is included
(commented out) at the top of the file.

---

## Build & Push

Run from any machine with Docker (or Amazon Linux / CloudShell). Replace the
account ID, region, and tag to match your environment. Tag with the AWS CLI
version the base image ships (check `docker run --rm amazon/aws-cli:latest --version`)
so `docker_images.json` stays traceable - e.g. `v2.34.25`.

```bash
cd sam_template/dockerfile

REGION=ap-southeast-1
ACCOUNT=123456789012
REPO=gatk-sv/awscli
TAG=v2.34.25

# Create the ECR repo if it doesn't exist
aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$REPO" --region "$REGION"

# Log in to ECR
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin \
    "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"

# Build
docker build \
  -f dockerfile.awscli \
  -t "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:$TAG" \
  .

# Push
docker push "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:$TAG"
```

---

## Wire It Into the Config

Set the `manifest_reader_docker` key in `docker_images.json` to the pushed URI so
the submit Lambdas hand it to HealthOmics. Config loads S3-first, so editing the S3
copy is enough (takes effect on the next Lambda cold start):

```bash
aws s3 cp s3://<bucket>/<ByobConfig>/docker_images.json /tmp/docker_images.json
# edit: "manifest_reader_docker": "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/gatk-sv/awscli:v2.34.25"
aws s3 cp /tmp/docker_images.json s3://<bucket>/<ByobConfig>/docker_images.json
```

The bundled config layer copy lives at
`gatksv_healthomics/shared/python/config/docker_images.json` - update it too if you
want the layer fallback to match.

---

## Notes

- **Platform**: HealthOmics runs on x86_64. Build on an x86 machine, or pass
  `--platform linux/amd64` if building from arm64 (e.g. Apple Silicon).
- Only `ReadManifest`-style tasks use this image; it is unrelated to the removed
  AWS Batch / miniwdl backend that older versions of this README described.
