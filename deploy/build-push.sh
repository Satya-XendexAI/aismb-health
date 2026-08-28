#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Build the container image and push it to Azure Container Registry (ACR).
# Replace ACR with your registry's login server, e.g. myacr.azurecr.io.
#
# Usage:
#   ./build-push.sh                 # tags :latest
#   ./build-push.sh v1.2.3          # tags :v1.2.3
#   ACR=other.azurecr.io ./build-push.sh
# ---------------------------------------------------------------------------
set -euo pipefail

ACR="${ACR:-hsptlagnrgstry.azurecr.io}"
IMAGE="$ACR/aismb-health"
TAG="${1:-latest}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo ">> Logging into ACR: $ACR"
az acr login --name "${ACR%%.*}"

echo ">> Building $IMAGE:$TAG"
docker build -t "$IMAGE:$TAG" "$ROOT"

echo ">> Pushing $IMAGE:$TAG"
docker push "$IMAGE:$TAG"

echo ">> Done. Update image: in whatsapp-manifest.yaml and interface-manifest.yaml"
echo "   set image: $IMAGE:$TAG"
