# syntax=docker/dockerfile:1
# Derived from the upstream serving image. Adds three cold-start changes:
#   1. vLLM on :18000 (the port PyWorker expects) -> no socat relay
#   2. Xet disabled -> hf download shows a real tqdm progress bar
#   3. PyWorker code + venv baked in -> boot skips git clone + pip install
FROM ghcr.io/syv-ai/qwen38-27b-rtx3090:latest

# 1. vLLM listens where PyWorker looks (vast-pyworker workers/openai/core.py
#    MODEL_SERVER_PORT=18000). single-user/start_qwen.sh reads PORT=${PORT:-18020},
#    so this one env var moves the listener and the socat relay becomes dead code.
ENV PORT=18000

# 2. HF_XET_HIGH_PERFORMANCE only tunes the Xet backend (more CPU/parallelism);
#    it does not restore progress output. HF_HUB_DISABLE_XET=1 is the switch that
#    falls back to classic HTTP transfer with tqdm bars. HIGH_PERFORMANCE is set
#    to 0 explicitly so nothing re-enables the fast-but-silent path.
ENV HF_XET_HIGH_PERFORMANCE=0 HF_HUB_DISABLE_XET=1

EXPOSE 18000

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
# call on every boot. Pre-seed it; NLTK_DATA is exported at boot by onstart.sh.
RUN set -eux; \
    NLTK_DATA=/opt/pyw/nltk_data /opt/pyw/worker-env/bin/python -c \
        "import nltk; nltk.download('words', quiet=True)"
