#!/bin/bash
# =============================================================================
# qwen3.8-27b boot script for Vast.ai  (template runtype: ssh)
# Paste this verbatim into the template's "On Start Command".
#
# Two modes, selected by SERVERLESS:
#   SERVERLESS=1 (default) -- serverless worker: vLLM + PyWorker on :3000.
#   SERVERLESS=0           -- plain instance: vLLM only, no PyWorker.
#
# Stack (SERVERLESS=1):  vLLM :18000 <-- PyWorker :3000 --> VAST
# Stack (SERVERLESS=0):  vLLM :18000   (reach it on the mapped host port)
#
# Why each piece exists (all verified on a live boot, 2026-09-05):
#  1. runtype=ssh makes VAST replace the image's ENTRYPOINT
#     (docker/entrypoint.sh single), so NOTHING starts the model server by
#     itself -- this script must drive it.
#  2. PyWorker hardcodes its model server at 127.0.0.1:18000
#     (vast-pyworker workers/openai/core.py: MODEL_SERVER_PORT=18000). The
#     image sets PORT=18000, so vLLM listens there directly -- no socat relay.
#  3. The platform injects VLLM_API_KEY into the container env, which makes
#     vLLM enforce auth on /v1/*. PyWorker sends NO Authorization header to
#     the model server, so every request 401s and its readiness benchmark
#     fails ("No successful responses from benchmark"). Unsetting the keys
#     is safe under SERVERLESS=1: vLLM is only reachable over loopback; the
#     public face is PyWorker's own authenticated gateway on :3000.
#     Under SERVERLESS=0 there is no such gateway, so vLLM IS the public
#     face -- see the auth note in section 3.
#  4. PyWorker detects readiness by tailing MODEL_LOG (default
#     /var/log/portal/vllm.log) for "Application startup complete.", then
#     runs a LIVE benchmark against the model and a /health poll before
#     reporting capacity. Hence vLLM's stdout goes straight to that path.
#  5. PyWorker's own setup (git clone + venv + pip install + nltk download)
#     normally runs here on every boot. The image bakes all of it to /opt/pyw,
#     so the exports below point PyWorker at what is already on disk and its
#     install branch is skipped. WORKSPACE_DIR must move with it: it is not
#     volume-shadowed at /opt/pyw the way /workspace can be.
#
# Boot timeline on a FRESH host (measured before the image changes below):
#   image pull ~5 min + HF weight fetch ~19.5 GB ~5 min + engine load ~2 min
#   => ~12 min cold start, $0 while scaled to zero. Pre-baking PyWorker and
#   disabling Xet takes ~3-4 min off that and makes the download observable.
# =============================================================================

set -u
cd /app || exit 1

# SERVERLESS=1 (default) runs PyWorker; SERVERLESS=0 serves vLLM alone.
# Accepts 0/1, and the usual falsey spellings so an explicit "false" works.
case "${SERVERLESS:-1}" in
  0|no|false|off|FALSE|NO|OFF) SERVERLESS=0 ;;
  *)                           SERVERLESS=1 ;;
esac
export SERVERLESS

# --- 1) Model server ----------------------------------------------------------
# entrypoint.sh single = docker/prepare.sh (idempotent: fetches
#   dbirks/Qwen3.8-27B-W4A16-AutoRound into the /app/models volume if absent)
#                     + verify.sh --no-server (aborts boot on broken patches)
#                     + single-user/start_qwen.sh (honors CTX / PREFIX_CACHE)
mkdir -p /var/log/portal

if [ "$SERVERLESS" = "1" ]; then
  # PyWorker talks to vLLM with no Authorization header, so the
  # platform-injected keys must be stripped or the benchmark 401s forever.
  env -u VLLM_API_KEY -u LLAMA_API_KEY \
    nohup bash docker/entrypoint.sh single </dev/null >/var/log/portal/vllm.log 2>&1 &
else
  # Plain instance: keep the platform-injected VLLM_API_KEY so the server
  # enforces auth. Upstream README: vLLM binds 0.0.0.0 and is UNAUTHENTICATED
  # unless a key is set -- dropping it here would expose an open, keyless
  # endpoint on whatever port the host maps to :18000.
  if [ -z "${VLLM_API_KEY:-}" ]; then
    echo "onstart: SERVERLESS=0 and no VLLM_API_KEY set -- vLLM will serve" \
         "UNAUTHENTICATED on 0.0.0.0. Set VLLM_API_KEY to require a bearer token."
  fi
  nohup bash docker/entrypoint.sh single </dev/null >/var/log/portal/vllm.log 2>&1 &
fi

# --- 2) Port relay: REMOVED.
# vLLM listens on :18000 natively via PORT=18000 in the image, which is the
# port PyWorker already targets -- there is nothing left to bridge.

# --- 3) PyWorker (the serverless runtime) -------------------------------------
if [ "$SERVERLESS" = "0" ]; then
  echo "onstart: SERVERLESS=0 -- skipping PyWorker; vLLM alone on :18000."
  exit 0
fi

# Registers with VAST immediately; reports capacity only AFTER the log marker,
# the live benchmark, and the healthcheck all succeed (section 4 above).
# MODEL_NAME must equal vLLM's --served-model-name or the benchmark 404s.
#
# Reproducibility pins (both honored natively by start_server.sh):
#   PYWORKER_REF -> git checkout of the cloned repo (no tags exist; SHA = main
#                   head at verification time, 2026-08-24)
#   SDK_VERSION  -> uv pip install vastai==X (requirements.txt floats >=0.3.0)
# Both values match what was verified working on the live boot-test instance,
# and both are baked into the image at build time.
#
# WORKSPACE_DIR/ENV_PATH/NLTK_DATA point at the pre-baked tree in the image
# (see the Dockerfile). start_server.sh skips clone + venv + pip when both
# SERVER_DIR and ENV_PATH already exist, and core.py's import-time
# nltk.download("words") finds the corpus already present.
readonly PYWORKER_SHA="2207a3f94b55a0921c1641520eeb83de5a0c1611"
export HF_TOKEN="${HF_TOKEN:-1}"
export MODEL_NAME="qwen3.8-27b"
export PYWORKER_REF="$PYWORKER_SHA"
export SDK_VERSION="1.6.0"
export WORKSPACE_DIR="/opt/pyw"
export ENV_PATH="/opt/pyw/worker-env"
export NLTK_DATA="/opt/pyw/nltk_data"
curl -L "https://raw.githubusercontent.com/vast-ai/pyworker/${PYWORKER_SHA}/start_server.sh" | bash
