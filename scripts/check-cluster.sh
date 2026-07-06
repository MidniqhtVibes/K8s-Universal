#!/usr/bin/env bash
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$(pwd)/kubeconfig}"

echo "== Kubernetes Nodes =="
kubectl get nodes -o wide

echo
echo "== System Pods =="
kubectl get pods -A

echo
echo "== Cluster Info =="
kubectl cluster-info

echo
echo "== API Health =="
kubectl get --raw='/readyz?verbose'