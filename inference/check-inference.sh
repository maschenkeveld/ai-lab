#!/usr/bin/env bash
set -euo pipefail
set -a
# shellcheck disable=SC1091
source .env
set +a
curl -fsS --max-time 8 -H "Authorization: Bearer ${VLLM_API_KEY:?}" \
  "http://127.0.0.1:${VLLM_PORT:-8000}/v1/models"
printf '\n'
# Real generation smoke test: a model listing alone does not prove inference works.
MODEL="${VLLM_MODEL:-mlx-community/Mistral-7B-Instruct-v0.3-4bit}"
curl -fsS --max-time 60 -H "Authorization: Bearer $VLLM_API_KEY" -H 'Content-Type: application/json' \
  "http://127.0.0.1:${VLLM_PORT:-8000}/v1/chat/completions" \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"system\",\"content\":\"Be brief.\"},{\"role\":\"user\",\"content\":\"Reply with the word OK.\"}],\"max_tokens\":8}" \
  | grep -q '"content"' && echo 'Generation smoke test: PASS' || { echo 'Generation smoke test: FAIL' >&2; exit 1; }
