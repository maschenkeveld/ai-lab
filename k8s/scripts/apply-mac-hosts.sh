#!/usr/bin/env bash
# Writes a managed block to /etc/hosts (sudo) mapping `*.lab` hostnames
# to the pinned MetalLB LoadBalancer IPs. Idempotent: re-running replaces
# the block bounded by markers.
#
# IPs are PINNED via metallb.universe.tf/loadBalancerIPs annotations in
# the Helm values + manifests, so this script uses the same static map
# rather than querying the cluster. Works even when the cluster isn't up.

set -euo pipefail

HOSTS_FILE="/etc/hosts"
MARKER_BEGIN="# BEGIN ai-lab hostnames"
MARKER_END="# END ai-lab hostnames"

# Hostname → pinned MetalLB IP (must match the annotations in k8s/helm + k8s/manifests)
declare -a MAPPINGS=(
  "kafka-ui.lab           172.18.255.205"
  "analytics.lab          172.18.255.206"
  "analytics-api.lab      172.18.255.207"
  "n8n.lab                172.18.255.209"
  "traefik-api-gw.lab     172.18.255.210"
  "litellm.lab            172.18.255.210"
  "agentgateway.lab       172.18.255.211"
  "kafka-direct.lab       172.18.255.212"
)

# Only map hostnames for services actually deployed locally.
if [ "${KEYCLOAK_MODE:-local}" = "local" ]; then
  MAPPINGS+=("keycloak.lab           172.18.255.200")
fi
if [ "${OBSERVABILITY_MODE:-local}" = "local" ]; then
  MAPPINGS+=("grafana.lab            172.18.255.208")
fi

# Build the new block
BLOCK="${MARKER_BEGIN}"$'\n'
for m in "${MAPPINGS[@]}"; do
  host=$(echo "$m" | awk '{print $1}')
  ip=$(echo "$m" | awk '{print $2}')
  printf -v line "%-15s %s" "$ip" "$host"
  BLOCK+="$line"$'\n'
  echo "  · $host  → $ip"
done
BLOCK+="${MARKER_END}"

# Read current /etc/hosts, strip any existing block, append the new one
echo "→ Updating $HOSTS_FILE (sudo required)"
CURRENT=$(cat "$HOSTS_FILE")
STRIPPED=$(echo "$CURRENT" | awk -v b="$MARKER_BEGIN" -v e="$MARKER_END" '
  $0 == b {skip=1; next}
  $0 == e {skip=0; next}
  !skip
')

# Compose the new file (stripped + blank line + new block) and write atomically
TMP=$(mktemp)
{
  printf "%s" "$STRIPPED"
  echo ""
  echo "$BLOCK"
} > "$TMP"

sudo cp "$TMP" "$HOSTS_FILE"
rm -f "$TMP"

if [ "${KEYCLOAK_MODE:-local}" = "local" ]; then
  echo "✓ Done. Try: curl http://keycloak.lab:8080/realms/ai-lab/.well-known/openid-configuration"
else
  echo "✓ Done. KEYCLOAK_MODE=external — using ${KEYCLOAK_EXTERNAL_URL:-<KEYCLOAK_EXTERNAL_URL not set>}"
fi
