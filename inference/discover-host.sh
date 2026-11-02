#!/usr/bin/env bash
set -euo pipefail
if [[ -n "${VLLM_HOST:-}" ]]; then
  printf '%s\n' "$VLLM_HOST"
  exit 0
fi
if [[ $(uname -s) == Darwin ]]; then
  # host.lima.internal is a Lima host alias; resolve it from within Colima.
  host_ip=$(colima ssh -- getent ahostsv4 host.lima.internal 2>/dev/null | awk 'NR==1 { print $1 }')
  if [[ -n "$host_ip" ]]; then printf '%s\n' "$host_ip"; exit 0; fi
fi
echo 'Cannot determine native inference address. Set VLLM_HOST in .env to an IP reachable from Kind.' >&2
exit 1
