#!/usr/bin/env bash
set -euo pipefail

if command -v helm >/dev/null 2>&1; then
  echo "Helm ist bereits installiert:"
  helm version
  exit 0
fi

echo "Installiere Helm über offizielles Helm-Script..."

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

curl -fsSL -o "$tmpdir/get_helm.sh" https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3
chmod 700 "$tmpdir/get_helm.sh"
"$tmpdir/get_helm.sh"

helm version
