#!/usr/bin/env bash
set -euo pipefail

if (( $# == 0 )); then
  echo "Verwendung: $0 IP [IP ...]" >&2
  exit 2
fi

ssh_user="${SSH_USER:-ubuntu}"
ssh_port="${SSH_PORT:-22}"
timeout_seconds="${SSH_WAIT_TIMEOUT:-600}"
deadline=$((SECONDS + timeout_seconds))

for ip in "$@"; do
  echo "Warte auf SSH bei ${ip}:${ssh_port} ..."
  until ssh -p "$ssh_port" \
    -o BatchMode=yes \
    -o ConnectTimeout=5 \
    -o StrictHostKeyChecking=accept-new \
    "${ssh_user}@${ip}" "echo ok" >/dev/null 2>&1; do
      if (( SECONDS >= deadline )); then
        echo "SSH-Timeout bei ${ip}" >&2
        exit 1
      fi
      sleep 5
  done
  echo "$ip ist per SSH erreichbar."
done
