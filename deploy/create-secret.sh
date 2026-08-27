#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Create / update the aismb-secrets Kubernetes Secret from a local .env file.
# Only active (non-commented) KEY=VALUE lines are included. Lines starting
# with `#`, blank lines, inline ` # comments`, and a leading `export ` are
# all handled. Your .env stays local and is never committed.
#
# Usage:
#   ./create-secret.sh            # reads ../.env
#   ./create-secret.sh /path/.env # custom env file
# ---------------------------------------------------------------------------
set -euo pipefail

NAMESPACE=aismb-health
SECRET_NAME=aismb-secrets
ENV_FILE="${1:-../.env}"

# Ensure the namespace exists.
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

ARGS=()
while IFS= read -r line || [ -n "$line" ]; do
  # Trim leading/trailing whitespace.
  line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  # Skip blank lines and full-line comments.
  [ -z "$line" ] && continue
  [ "${line#\#}" != "$line" ] && continue
  # Strip a trailing inline comment ("key=val # note").
  line="${line%%[[:space:]]#*}"
  # Drop a leading "export " if present.
  line="${line#export }"
  line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

  case "$line" in
    *=*)
      key="${line%%=*}"
      val="${line#*=}"
      val="$(echo "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
      ARGS+=(--from-literal="$key=$val")
      ;;
  esac
done < "$ENV_FILE"

if [ "${#ARGS[@]}" -eq 0 ]; then
  echo "No KEY=VALUE pairs found in $ENV_FILE — aborting." >&2
  exit 1
fi

kubectl create secret generic "$SECRET_NAME" -n "$NAMESPACE" \
  "${ARGS[@]}" --dry-run=client -o yaml | kubectl apply -f -

echo "Secret '$SECRET_NAME' created/updated in namespace '$NAMESPACE' (${#ARGS[@]} keys)."
