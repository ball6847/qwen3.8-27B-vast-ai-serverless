#!/usr/bin/env python3
"""create_template.py - create a qwen38-27b template, fully explicit.

Usage:
    create_template.py [--plain] [desc_suffix]

  (no flag)  serverless template: vLLM + PyWorker on :3000, scale-to-zero
  --plain    non-serverless template: vLLM alone on :18000, no PyWorker

Serverless posts the worktree onstart.sh with runtype=ssh. Plain does NOT:
there is no PyWorker to feed, and runtype=ssh makes Vast replace the image's
ENTRYPOINT, so whatever onstart starts is detached from the container log.
Plain therefore uses runtype=args with an EMPTY onstart, letting the image's
native Docker entrypoint run `docker/entrypoint.sh single` in the foreground.
vLLM's stdout then lands in the container log, so `vastai logs` shows boot
progress (the ~19.5 GB weight fetch, then engine load). That is the whole
point of plain mode being usable -- do not "unify" it back to ssh.

The complete template definition lives below as constants (no cloning from
another template id). Posts worktree onstart.sh as the on-start command.
Template PUT is broken server-side, so every change is delete + recreate;
the hash changes each time - record the printed id + hash.

Asserts pure-ASCII onstart before POST (non-ASCII delivery corrupts).
Stdlib only. Key read from $VAST_API_KEY, else
~/.config/vastai/vast_api_key (override the path with VAST_API_KEY_PATH).
"""
import json
import os
import sys
import urllib.request
import urllib.error

BASE = "https://console.vast.ai/api/v0"

# --- Full template definition (explicit; edit here, not via clone) ---
# Derived image built from this repo's Dockerfile (FROM upstream + PORT=18000,
# Xet disabled, pyworker pre-baked). CONFIRM this namespace is where you pushed
# it before POSTing -- the upstream syv-ai image does NOT contain these changes.
IMAGE = "ghcr.io/ball6847/qwen3.8-27b-vast-ai-serverless"
TAG = "latest"
RUNTYPE_SSH = "ssh"  # ssh runtype suppresses the image entrypoint; onstart drives all
# cuda_max_good is the HIGHEST CUDA version the host driver supports (it tracks
# the driver, not the toolkit). The image is CUDA 13.0 (torch 2.13.0+cu130), and
# CUDA 13.x mandates driver >= 580 -- so without this clause Vast will happily
# schedule onto a 570/12.8 box where `torch.cuda.is_available()` is False.
# verify.sh then FAILs ("torch cannot see a CUDA GPU"), entrypoint.sh exits 1,
# and the container restart-loops forever, re-running verify on every boot.
#
# NOTE: extra_filters only pre-filters the offer list the GUI searches; it does
# NOT hard-block renting an ineligible offer by hand. Confirm CUDA >= 13.0 if
# you pick an offer manually.
EXTRA_FILTERS = {"verified": {"eq": True}, "external": {"eq": False},
                 "rentable": {"eq": True}, "direct_port_count": {"gte": 2},
                 "cuda_max_good": {"gte": 13.0}}

# Per-mode overrides. SERVERLESS is read by onstart.sh, not by the image; the
# -p flag publishes the port the mode actually listens on. The image sets
# PORT=18000, so plain mode maps 18000:18000 with no PORT override needed.
# (For upstream's 18020 convention instead, set PORT=18020 and -p 18020:18020.)
MODES = {
    "serverless": {
        "name": "qwen38-27b-rtx3090-single-serverless",
        "runtype": "ssh", "onstart": True,
        "use_ssh": True, "ssh_direct": True,
        "disk": 50.0,
        "href": "https://github.com/vast-ai/pyworker",
        "repo": "ghcr.io/ball6847/qwen3.8-27b-vast-ai-serverless",
        "env": ("-p 3000:3000 -e CTX=long -e PREFIX_CACHE=1 -e BACKEND=vllm "
                "-e SERVERLESS=true -e MODEL_NAME=qwen3.8-27b "
                "-e MODEL_HEALTH_ENDPOINT=/health"),
        "desc": (
            "Serverless variant of qwen38-27b-rtx3090-single. Derived image "
            "serves vLLM on :18000 (the port Vast PyWorker targets, no socat "
            "relay) and bakes pyworker + venv so boot skips clone/install; Xet "
            "disabled so the weight download shows progress. ssh runtype "
            "suppresses the entrypoint so onstart drives the model server; "
            "VLLM_API_KEY is unset so the readiness benchmark can reach the "
            "model. Scale-to-zero ready: min_load=0, cold_workers=0, positive "
            "inactivity_timeout."),
    },
    "plain": {
        "name": "qwen38-27b-rtx3090-single-plain",
        # runtype=args + no onstart == the image's own ENTRYPOINT runs.
        # ENTRYPOINT is `bash docker/entrypoint.sh single`, which reads the -e
        # vars above (CTX, PREFIX_CACHE, PORT) and execs
        # single-user/start_qwen.sh in the FOREGROUND with no redirection, so
        # vLLM's stdout goes to the container log and `vastai logs` works.
        # Do NOT set runtype=ssh here: Vast would replace the entrypoint and
        # onstart would have to start vLLM itself, detaching it from the log.
        "runtype": "args", "onstart": False,
        "use_ssh": False, "ssh_direct": False,
        # 50 GB. prepare.sh needs ~22 GB of weights (19.5 base + ~1 fast
        # variant + ~1 DFlash2 drafter) plus its requant outputs, on top of the
        # ~10 GB image; 50 leaves ample headroom.
        "disk": 50.0,
        "href": "https://github.com/ball6847/qwen3.8-27b-vast-ai-serverless",
        "repo": "ghcr.io/ball6847/qwen3.8-27b-vast-ai-serverless",
        # No :3000 and no PyWorker env: nothing registers with VAST, so this is
        # just a vLLM box. Reach it on the host port mapped to :18000.
        "env": ("-p 18000:18000 -e CTX=long -e PREFIX_CACHE=1"),
        "desc": (
            "Non-serverless variant: vLLM alone on :18000, no PyWorker, no "
            ":3000 gateway, nothing registered with VAST. Derived image adds "
            "PORT=18000, Xet disabled (visible weight-download progress) and a "
            "pre-baked PyWorker tree. runtype=args with no onstart, so the "
            "image's native Docker entrypoint runs vLLM in the foreground and "
            "its stdout reaches the container log -- boot progress is "
            "visible via vastai logs. VLLM_API_KEY is KEPT from the "
            "platform env if set, else vLLM serves unauthenticated."),
    },
}
ONSTART_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "onstart.sh")
KEY_PATH = os.path.expanduser(os.environ.get("VAST_API_KEY_PATH",
                                             "~/.config/vastai/vast_api_key"))


def get_key():
    """$VAST_API_KEY wins, then VAST_API_KEY_PATH / the default key file."""
    key = os.environ.get("VAST_API_KEY")
    if key:
        return key.strip()
    try:
        return open(KEY_PATH).read().strip()
    except OSError:
        sys.exit(f"No Vast API key: set $VAST_API_KEY or write {KEY_PATH}")


def api(path, data=None, method="GET"):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Bearer {KEY}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()[:300]}")
        sys.exit(1)


KEY = get_key()
onstart = open(ONSTART_PATH).read()
assert onstart.isascii(), "onstart.sh must be pure ASCII - fix before POST"

args = [a for a in sys.argv[1:]]
mode = "serverless"
if "--plain" in args:
    args.remove("--plain")
    mode = "plain"
cfg = MODES[mode]

suffix = args[0] if args else ""
desc = (cfg["desc"] + suffix)[-512:]  # desc cap is 512 chars
payload = {
    "name": cfg["name"], "image": IMAGE, "tag": TAG,
    "href": cfg["href"], "repo": cfg["repo"], "env": cfg["env"],
    "onstart": onstart if cfg["onstart"] else "",
    "jup_direct": False, "ssh_direct": cfg["ssh_direct"],
    "use_jupyter_lab": False, "runtype": cfg["runtype"],
    "use_ssh": cfg["use_ssh"],
    "jupyter_dir": None, "docker_login_repo": None,
    "extra_filters": EXTRA_FILTERS,
    "recommended_disk_space": cfg["disk"], "readme": None, "readme_visible": True,
    "desc": desc, "private": True,
}
print(f"mode: {mode}  name: {cfg['name']}  image: {IMAGE}:{TAG}  runtype: {cfg['runtype']}")
res = api("/template/", payload, "POST")
print(json.dumps(res.get("msg", res))[:200])

# Verify what the server stored.
new_id = res.get("template", {}).get("id") or res.get("id")
if new_id:
    t = [x for x in api("/users/current/templates/")["templates"]
         if x["id"] == new_id][0]
    print("new id:", t["id"], "hash:", t["hash_id"])
    print("stored runtype/use_ssh:", t["runtype"], t["use_ssh"])
    print("stored env:", t["env"])
    print("stored image:", f"{t['image']}:{t['tag']}")
    if cfg["onstart"]:
        print("onstart identical:", t["onstart"] == onstart)
    else:
        print("onstart empty (image ENTRYPOINT drives boot):",
              not t["onstart"])
