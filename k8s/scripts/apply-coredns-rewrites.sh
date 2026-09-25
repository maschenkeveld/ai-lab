#!/usr/bin/env bash
# Patches kube-system/coredns ConfigMap to add `*.lab` → `<svc>.<ns>.svc.cluster.local`
# rewrites. Idempotent: re-running replaces the managed block bounded by markers.

set -euo pipefail

MARKER_BEGIN="# BEGIN ai-lab rewrites"
MARKER_END="# END ai-lab rewrites"

# Hostname → in-cluster service DNS (one per line, two columns)
read -r -d '' MAPPINGS <<'EOF' || true
keycloak.lab            keycloak.keycloak.svc.cluster.local
kafka-ui.lab            kafka-ui.kafka.svc.cluster.local
analytics.lab           llm-analytics-ui.llm-analytics.svc.cluster.local
analytics-api.lab       llm-analytics-api.llm-analytics.svc.cluster.local
grafana.lab             otel-lgtm.otel-lgtm.svc.cluster.local
n8n.lab                 n8n.n8n.svc.cluster.local
traefik-api-gw.lab      traefik.traefik.svc.cluster.local
litellm.lab             traefik.traefik.svc.cluster.local
agentgateway.lab        agentgateway-proxy.agentgateway-system.svc.cluster.local
kafka-direct.lab        kafka-direct.kafka.svc.cluster.local
EOF

# Drop entries for services that aren't deployed locally in external mode.
if [ "${KEYCLOAK_MODE:-local}" != "local" ]; then
  MAPPINGS=$(echo "$MAPPINGS" | grep -v '^keycloak\.lab')
fi
if [ "${OBSERVABILITY_MODE:-local}" != "local" ]; then
  MAPPINGS=$(echo "$MAPPINGS" | grep -v '^grafana\.lab')
fi

# Pull the current Corefile, strip any prior block, inject the new block right after "errors".
NEW_COREFILE=$(MAPPINGS="$MAPPINGS" \
              MARKER_BEGIN="$MARKER_BEGIN" \
              MARKER_END="$MARKER_END" \
              python3 - <<'PY'
import os, re, subprocess, sys

marker_begin = os.environ["MARKER_BEGIN"]
marker_end   = os.environ["MARKER_END"]
mappings_raw = os.environ["MAPPINGS"].strip()

# Build the rewrite block
lines = ["    " + marker_begin]
for line in mappings_raw.splitlines():
    line = line.strip()
    if not line:
        continue
    short, full = line.split()
    lines.append(f"    rewrite name {short} {full}")
lines.append("    " + marker_end)
block = "\n".join(lines)

# Grab current Corefile
res = subprocess.run(
    ["kubectl", "get", "configmap", "coredns", "-n", "kube-system",
     "-o", "jsonpath={.data.Corefile}"],
    check=True, capture_output=True, text=True,
)
current = res.stdout

# Strip any existing managed block
stripped = re.sub(
    rf"^\s*{re.escape(marker_begin)}.*?{re.escape(marker_end)}\s*\n",
    "", current, flags=re.MULTILINE | re.DOTALL,
)

# Insert the new block right after the first `errors` line
out_lines = []
inserted = False
for line in stripped.splitlines():
    out_lines.append(line)
    if not inserted and line.strip() == "errors":
        out_lines.append(block)
        inserted = True

if not inserted:
    print("ERROR: could not find 'errors' line in Corefile to anchor the insertion", file=sys.stderr)
    sys.exit(1)

print("\n".join(out_lines))
PY
)

echo "→ Patching coredns ConfigMap"
kubectl create configmap coredns --from-literal=Corefile="$NEW_COREFILE" \
  -n kube-system --dry-run=client -o yaml \
  | kubectl apply -f -

echo "→ Restarting coredns pods"
kubectl rollout restart deployment/coredns -n kube-system
kubectl rollout status deployment/coredns -n kube-system --timeout=60s

echo "✓ Done. *.lab names now resolve in-cluster."
