#!/usr/bin/env bash
set -euo pipefail
if [[ $(uname -s) != Darwin ]]; then
  echo 'vLLM-Metal requires macOS Apple Silicon. Point LiteLLM to a Linux/cloud vLLM instance instead.' >&2
  exit 1
fi
if [[ ! -f .env ]]; then echo 'Copy .env.example to .env and set VLLM_API_KEY first.' >&2; exit 1; fi
set -a
# shellcheck disable=SC1091
source .env
set +a
: "${VLLM_API_KEY:?}"
# Chat template and tool-call parser are model-specific. Defaults match the Mistral checkpoint;
# for others set VLLM_CHAT_TEMPLATE="" (use the model's own template) and VLLM_TOOL_PARSER.
template="${VLLM_CHAT_TEMPLATE-llm/mistral-chat-template.jinja}"
args=(--host "${VLLM_BIND_HOST:-127.0.0.1}" --port "${VLLM_PORT:-8000}" --api-key "$VLLM_API_KEY"
  --max-model-len 8192 --enable-auto-tool-choice --tool-call-parser "${VLLM_TOOL_PARSER:-mistral}")
if [[ -n "$template" ]]; then args+=(--chat-template "$template"); fi
exec vllm serve "${VLLM_MODEL:-mlx-community/Mistral-7B-Instruct-v0.3-4bit}" "${args[@]}"
