#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Apply all manifests to the current kube context (must point at your AKS
# cluster). Run AFTER the image is built/pushed and the secret is created.
#
# Prereqs:
#   1) kubectl pointed at the cluster (az aks get-credentials ...)
#   2) ./create-secret.sh   (or the Azure DevOps pipeline created it)
# ---------------------------------------------------------------------------
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)/k8s"

kubectl apply -f "$DIR/namespace.yaml"
kubectl apply -f "$DIR/secret.yaml"
kubectl apply -f "$DIR/whatsapp-deployment.yaml"
kubectl apply -f "$DIR/whatsapp-service.yaml"

echo ">> Applied. Watching rollout..."
kubectl rollout status deployment/aismb-whatsapp -n aismb-health --timeout=120s

echo ">> External IPs (may take a minute to be assigned):"
kubectl get svc -n aismb-health
