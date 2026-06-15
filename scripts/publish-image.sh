#!/usr/bin/env bash
# Build the anvil-data-qc image and push it to Google Artifact Registry (GAR).
#
# Prerequisites (one-time -- see wdl/README.md, "Publish the image to GAR"):
#   - the GAR repo exists:
#       gcloud artifacts repositories create anvil-qc --repository-format=docker \
#         --location=us-central1 --project=dsp-sa-development
#   - docker is authenticated to GAR:
#       gcloud auth configure-docker us-central1-docker.pkg.dev
#
# Usage:
#   scripts/publish-image.sh           # tag = the version in pyproject.toml
#   scripts/publish-image.sh 0.1.1     # explicit tag
set -euo pipefail

PROJECT="dsp-sa-development"
REGION="us-central1"
REPO="anvil-qc"
IMAGE="anvil-data-qc"

# Always build from the repo root (this script lives in scripts/).
cd "$(dirname "$0")/.."

TAG="${1:-$(grep -m1 '^version = ' pyproject.toml | cut -d'"' -f2)}"
REF="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${IMAGE}:${TAG}"

# Terra/Cromwell hosts are linux/amd64, so always build for that platform -- otherwise an image built
# on an Apple Silicon (arm64) machine fails in Terra with "exec format error". `buildx ... --push`
# builds and pushes atomically; a separate `docker push` can silently no-op and leave a stale tag.
echo "Building and pushing ${REF} (linux/amd64, for Terra/Cromwell) ..."
docker buildx build --platform linux/amd64 -t "${REF}" --push .

echo "Done: ${REF}"
