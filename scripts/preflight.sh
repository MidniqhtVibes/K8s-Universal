#!/usr/bin/env bash
set -euo pipefail

command -v terraform >/dev/null || { echo "Terraform fehlt"; exit 1; }
command -v ansible >/dev/null || { echo "Ansible fehlt"; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl fehlt"; exit 1; }
command -v helm >/dev/null || { echo "Helm fehlt"; exit 1; }

ssh-add -l >/dev/null || {
  echo "Kein SSH-Key im Agent geladen. Bitte k8s-agent ausführen."
  exit 1
}

echo "Preflight OK"
