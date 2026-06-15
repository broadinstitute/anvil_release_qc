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

echo "Building ${REF} ..."
docker build -t "${REF}" .

echo "Pushing ${REF} ..."
docker push "${REF}"

echo "Done: ${REF}"
