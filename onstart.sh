#!/bin/bash
# =============================================================================
# qwen3.8-27b serverless bridge for Vast.ai  (template runtype: ssh)
# Paste this verbatim into the template's "On Start Command".
#
# Stack:  vLLM :18020 --(socat)--> 127.0.0.1:18000 <-- PyWorker :3000 --> VAST
#
# Why each piece exists (all verified on a live boot, 2026-09-05):
#  1. runtype=ssh makes VAST replace the image's ENTRYPOINT
#     (docker/entrypoint.sh single), so NOTHING starts the model server by
#     itself -- this script must drive it.
#  2. PyWorker hardcodes its model server at 127.0.0.1:18000
#     (vast-pyworker workers/openai/core.py: MODEL_SERVER_PORT=18000), while
#     this image serves vLLM on :18020 -> socat bridges the gap.
#  3. The platform injects VLLM_API_KEY into the container env, which makes
#     vLLM enforce auth on /v1/*. PyWorker sends NO Authorization header to
#     the model server, so every request 401s and its readiness benchmark
#     fails ("No successful responses from benchmark"). Unsetting the keys
#     is safe: vLLM is only reachable over loopback; the public face is
#     PyWorker's own authenticated gateway on :3000.
#  4. PyWorker detects readiness by tailing MODEL_LOG (default
#     /var/log/portal/vllm.log) for "Application startup complete.", then
#     runs a LIVE benchmark against the model and a /health poll before
#     reporting capacity. Hence vLLM's stdout goes straight to that path.
#
# Boot timeline on a FRESH host (measured, Spain 4.8 Gbps, 2026-09-05):
#   image pull ~5 min + HF weight fetch ~19.5 GB ~5 min + engine load ~2 min
#   => ~12 min cold start, $0 while scaled to zero.
# =============================================================================

set -u
cd /app || exit 1

# --- 1) Model server ----------------------------------------------------------
# entrypoint.sh single = docker/prepare.sh (idempotent: fetches
#   dbirks/Qwen3.8-27B-W4A16-AutoRound into the /app/models volume if absent)
#                     + verify.sh --no-server (aborts boot on broken patches)
#                     + single-user/start_qwen.sh (honors CTX / PREFIX_CACHE)
mkdir -p /var/log/portal
env -u VLLM_API_KEY -u LLAMA_API_KEY \
  nohup bash docker/entrypoint.sh single </dev/null >/var/log/portal/vllm.log 2>&1 &

# --- 2) Port relay: PyWorker probes :18000, vLLM listens on :18020 -------------
# NOTE: /workspace does NOT exist yet at this point (pyworker bootstrap creates
# it later), so the relay log must live under /var/log/portal (created above).
# The loop retries for ~5 min and never stacks duplicate socats: without this,
# a slow container start means the relay never comes up and the benchmark fails
# forever against a closed :18000 (seen live 2026-09-06).
mkdir -p /workspace
command -v socat >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq socat; }
for _ in $(seq 1 150); do
  if (exec 3<>"/dev/tcp/127.0.0.1/18000") 2>/dev/null; then exec 3>&- 3<&-; break; fi
  pgrep -f "socat TCP-LISTEN:18000" >/dev/null 2>&1 || \
    nohup socat TCP-LISTEN:18000,fork,reuseaddr TCP:127.0.0.1:18020 </dev/null >>/var/log/portal/socat.log 2>&1 &
  sleep 2
done

# --- 3) PyWorker (the serverless runtime) -------------------------------------
# Registers with VAST immediately; reports capacity only AFTER the log marker,
# the live benchmark, and the healthcheck all succeed (section 4 above).
# MODEL_NAME must equal vLLM's --served-model-name or the benchmark 404s.
#
# Reproducibility pins (both honored natively by start_server.sh):
#   PYWORKER_REF -> git checkout of the cloned repo (no tags exist; SHA = main
#                   head at verification time, 2026-08-24)
#   SDK_VERSION  -> uv pip install vastai==X (requirements.txt floats >=0.3.0)
# Both values match what was verified working on the live boot-test instance.
# The bootstrap script itself is fetched at the SAME SHA (main-head is VAST's
# rolling default; freezing it removes the last moving part).
readonly PYWORKER_SHA="2207a3f94b55a0921c1641520eeb83de5a0c1611"
export HF_TOKEN="${HF_TOKEN:-1}"
export MODEL_NAME="qwen3.8-27b"
export PYWORKER_REF="$PYWORKER_SHA"
export SDK_VERSION="1.6.0"
curl -L "https://raw.githubusercontent.com/vast-ai/pyworker/${PYWORKER_SHA}/start_server.sh" | bash
