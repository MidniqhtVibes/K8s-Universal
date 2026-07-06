#!/usr/bin/env bash
set -euo pipefail

IPS=(
  10.200.50.145
  10.200.50.146
  10.200.50.151
  10.200.50.152
  10.200.50.153
  10.200.50.161
  10.200.50.162
)

for ip in "${IPS[@]}"; do
  echo "Warte auf SSH bei $ip ..."
  until ssh \
    -o BatchMode=yes \
    -o ConnectTimeout=5 \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    ubuntu@"$ip" "echo ok" >/dev/null 2>&1; do
      sleep 10
  done
  echo "$ip ist per SSH erreichbar."
done
