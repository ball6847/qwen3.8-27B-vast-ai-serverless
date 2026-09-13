# syntax=docker/dockerfile:1
# Derived from the upstream serving image. Adds three cold-start changes:
#   1. vLLM on :18000 (the port PyWorker expects) -> no socat relay
#   2. Xet disabled -> hf download shows a real tqdm progress bar
#   3. PyWorker code + venv baked in -> boot skips git clone + pip install
FROM ghcr.io/syv-ai/qwen38-27b-rtx3090:latest

# Re-point the OCI provenance labels at THIS repo. The upstream image sets
# org.opencontainers.image.source=https://github.com/syv-ai/qwen38-27b-rtx3090,
# and GitHub links a GHCR package to a repository by reading exactly that label.
# Without these overrides the derived image keeps the upstream source, GHCR
# cannot match it to a repo you own, and the package stays user-scoped -- which
# means it does NOT inherit repo visibility (so a public repo still yielded a
# private, anonymous-401 package). With source set here, the package links to
# this repo and tracks its visibility.
LABEL org.opencontainers.image.source="https://github.com/ball6847/qwen3.8-27B-vast-ai-serverless" \
      org.opencontainers.image.title="qwen3.8-27b serverless (RTX 3090)" \
      org.opencontainers.image.description="Derived serving image: vLLM on :18000 for Vast PyWorker, Xet disabled for visible weight-download progress, PyWorker + venv + nltk corpus pre-baked for fast cold starts." \
      org.opencontainers.image.licenses="Apache-2.0"

# 1. vLLM listens where PyWorker looks (vast-pyworker workers/openai/core.py
#    MODEL_SERVER_PORT=18000). single-user/start_qwen.sh reads PORT=${PORT:-18020},
#    so this one env var moves the listener and the socat relay becomes dead code.
ENV PORT=18000

# 2. HF_XET_HIGH_PERFORMANCE only tunes the Xet backend (more CPU/parallelism);
#    it does not restore progress output. HF_HUB_DISABLE_XET=1 is the switch that
#    falls back to classic HTTP transfer with tqdm bars. HIGH_PERFORMANCE is set
#    to 0 explicitly so nothing re-enables the fast-but-silent path.
ENV HF_XET_HIGH_PERFORMANCE=0 HF_HUB_DISABLE_XET=1

# Default to serverless mode. Set SERVERLESS=0 to boot vLLM alone (no PyWorker)
# for a plain instance -- onstart.sh reads this and skips the worker entirely.
ENV SERVERLESS=1

EXPOSE 18000

# --- 1b. Host requirement -----------------------------------------------------
# The upstream image is CUDA 13.0 (torch 2.13.0+cu130). CUDA 13.x mandates
# NVIDIA driver >= 580. On an older host torch.cuda.is_available() is False and
# verify.sh FAILs ("torch cannot see a CUDA GPU"); entrypoint.sh exits 1 on that
# FAIL and Vast restart-loops the container. Rent hosts with cuda_max_good >= 13
# (see the CUDA column in the Vast offer list). No image change can relax this --
# it is a runtime driver coupling, recorded here so the constraint is visible
# next to the version of CUDA that imposes it.

# --- 3. Pre-baked PyWorker ----------------------------------------------------
# start_server.sh skips clone/venv/pip entirely when both SERVER_DIR and ENV_PATH
# already exist (it only activates the venv). Baking them removes a git clone and
# a full dependency install from every cold start.
#
# Baked to /opt/pyw, NOT /workspace: Vast mounts /workspace as a volume on ssh
# runtype, which would shadow anything at that path.
ARG PYWORKER_SHA=2207a3f94b55a0921c1641520eeb83de5a0c1611

# git is not in the upstream image; only needed to fetch pyworker at build time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Fetch the exact commit rather than clone-then-checkout: a shallow clone of
# main only contains that one commit, so `git checkout <sha>` breaks as soon as
# upstream advances past it.
RUN set -eux; \
    mkdir -p /opt/pyw/vast-pyworker; \
    cd /opt/pyw/vast-pyworker; \
    git init -q; \
    git remote add origin https://github.com/vast-ai/pyworker; \
    git fetch -q --depth 1 origin "$PYWORKER_SHA"; \
    git checkout -q FETCH_HEAD

# uv and its managed Python both go under /opt/pyw, NOT the default $HOME
# locations: HOME is /cache, which the upstream image declares as a VOLUME and
# would shadow at runtime. This matters for the interpreter specifically --
# `uv venv` makes the venv's bin/python a SYMLINK into uv's managed-Python tree,
# so if that tree lived in /cache the venv would dangle after the volume mount.
ENV UV_INSTALL_DIR=/opt/pyw/bin \
    UV_PYTHON_INSTALL_DIR=/opt/pyw/python

RUN set -eux; \
    curl -LsSf https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh; \
    /opt/pyw/bin/uv venv --python-preference only-managed /opt/pyw/worker-env -p 3.10; \
    /opt/pyw/bin/uv pip install --python /opt/pyw/worker-env/bin/python \
        -r /opt/pyw/vast-pyworker/requirements.txt; \
    /opt/pyw/bin/uv pip install --python /opt/pyw/worker-env/bin/python \
        "vastai==1.6.0"

# workers/openai/core.py runs nltk.download("words") at import, which is a network
# call on every boot. Pre-seed it into /opt/pyw, NOT the default $HOME/nltk_data
# (= /cache/nltk_data), because /cache is a VOLUME and would shadow it at runtime.
#
# NLTK_DATA is the mechanism that works here: with it set, nltk.data.path[0] is
# /opt/pyw/nltk_data and download() writes there. Do NOT reach for
# download_dir= -- nltk's pathsec check rejects it ("Unauthorized path"), since
# the target must sit under one of the allowed roots. NLTK_DATA is also set as an
# image env var so the runtime read path matches; onstart.sh exports it too.
ENV NLTK_DATA=/opt/pyw/nltk_data

RUN set -eux; \
    mkdir -p /opt/pyw/nltk_data; \
    rm -rf /cache/nltk_data; \
    NLTK_DATA=/opt/pyw/nltk_data /opt/pyw/worker-env/bin/python -c \
        "import nltk; nltk.download('words', quiet=True)"; \
    test -f /opt/pyw/nltk_data/corpora/words/en; \
    test ! -e /cache/nltk_data
