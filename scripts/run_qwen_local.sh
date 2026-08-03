#!/usr/bin/env bash
#
# Start the local Qwen server on the MacBook (Nibbler's third-tier provider).
#
# This script DOES NOT install anything, download anything, open a firewall
# port, or start a tunnel. If a prerequisite is missing it says so and stops.
# Every install step needs the owner's judgement (and usually a password), so
# they stay manual — see docs/QWEN_LOCAL_SETUP.md.
#
# Binds to 127.0.0.1 on purpose. The endpoint reaches the internet only through
# an authenticated tunnel; an open 0.0.0.0 bind on a laptop that joins café
# Wi-Fi is a free GPU for whoever is on the same network.
#
#   ./scripts/run_qwen_local.sh
#
set -euo pipefail

QWEN_MODEL_PATH="${QWEN_MODEL_PATH:-$HOME/models/Qwen3-14B-Q4_K_M.gguf}"
# 32768 (Qwen3-14B's native window), NOT the 8192 that looks sufficient.
# A 15-minute Wisdom deck reserves 7,980 output tokens and sends ~8.7K of
# input (≈660-token system prompt + up to 14 retrieved chunks). An 8K window
# fits neither half, and llama-server truncates the START of an over-long
# prompt — silently discarding the instructions. Must be >= the backend's
# QWEN_CONTEXT_SIZE, which validates this at startup.
QWEN_CONTEXT_SIZE="${QWEN_CONTEXT_SIZE:-32768}"
QWEN_PARALLEL="${QWEN_PARALLEL:-1}"
QWEN_SERVER_PORT="${QWEN_SERVER_PORT:-8080}"
QWEN_ALIAS="${QWEN_MODEL:-qwen3-14b}"
# Same value as QWEN_API_KEY in the backend's environment. Never hard-code it
# here: this file is committed, and .env is not.
QWEN_API_KEY="${QWEN_API_KEY:-}"

fail() { printf '\n\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

command -v llama-server >/dev/null 2>&1 || fail \
"llama-server not found.

  Install it yourself (this script will not):
      brew install llama.cpp

  See docs/QWEN_LOCAL_SETUP.md for the full walkthrough."

[ -f "$QWEN_MODEL_PATH" ] || fail \
"No model file at: $QWEN_MODEL_PATH

  Download it yourself (~9 GB — this script will not fetch it):
      mkdir -p \"\$HOME/models\"
      huggingface-cli download Qwen/Qwen3-14B-GGUF \\
          Qwen3-14B-Q4_K_M.gguf --local-dir \"\$HOME/models\"

  Or set QWEN_MODEL_PATH to where it already lives."

[ -n "$QWEN_API_KEY" ] || fail \
"QWEN_API_KEY is not set.

  The tunnel makes this endpoint reachable from the internet, so it must
  require a key. Use the SAME value the backend has in QWEN_API_KEY:
      export QWEN_API_KEY='...'"

cat <<EOF

Starting Qwen for Nibbler
  model      : $QWEN_MODEL_PATH
  served as  : $QWEN_ALIAS   (must equal QWEN_MODEL in the backend env)
  context    : $QWEN_CONTEXT_SIZE tokens
  parallel   : $QWEN_PARALLEL
  listening  : http://127.0.0.1:$QWEN_SERVER_PORT  (loopback only)

Railway cannot reach this address. Start the tunnel in a second terminal and
point QWEN_BASE_URL at the tunnel's HTTPS URL + /v1.

EOF

# --jinja       : required — Qwen3's chat template lives in the GGUF metadata.
# -ngl 99       : all layers onto Metal. A 14B at Q4_K_M is ~9 GB, comfortable
#                 inside 24 GB unified memory alongside the OS.
# -fa           : flash attention, materially cheaper KV cache.
# --host        : loopback; the tunnel is the only way in.
# --api-key     : llama-server rejects requests without this bearer token.
# --no-webui    : no browser console, so a leaked URL exposes no model
#                 management surface — only the chat endpoint behind the key.
# Sampling matches Qwen's published non-thinking recommendation.
exec llama-server \
    --model "$QWEN_MODEL_PATH" \
    --alias "$QWEN_ALIAS" \
    --host 127.0.0.1 \
    --port "$QWEN_SERVER_PORT" \
    --api-key "$QWEN_API_KEY" \
    --ctx-size "$QWEN_CONTEXT_SIZE" \
    --parallel "$QWEN_PARALLEL" \
    --n-gpu-layers 99 \
    --flash-attn \
    --jinja \
    --no-webui \
    --temp 0.7 \
    --top-k 20 \
    --top-p 0.8 \
    --min-p 0 \
    --timeout 600
