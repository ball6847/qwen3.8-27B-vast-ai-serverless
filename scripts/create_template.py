#!/usr/bin/env python3
"""create_template.py - create the qwen38-27b serverless template, fully explicit.

Usage:
    create_template.py [desc_suffix]

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
NAME = "qwen38-27b-rtx3090-single-serverless"
# Derived image built from this repo's Dockerfile (FROM upstream + PORT=18000,
# Xet disabled, pyworker pre-baked). CONFIRM this namespace is where you pushed
# it before POSTing -- the upstream syv-ai image does NOT contain these changes.
IMAGE = "ghcr.io/ball6847/qwen3.8-27b-vast-ai-serverless"
TAG = "latest"
HREF = "https://github.com/vast-ai/pyworker"
REPO = "ghcr.io/ball6847/qwen3.8-27b-vast-ai-serverless"
RUNTYPE = "ssh"  # ssh runtype suppresses the image entrypoint; onstart drives all
ENV = ("-p 3000:3000 -e CTX=long -e PREFIX_CACHE=1 -e BACKEND=vllm "
       "-e SERVERLESS=true -e MODEL_NAME=qwen3.8-27b "
       "-e MODEL_HEALTH_ENDPOINT=/health")
EXTRA_FILTERS = {"verified": {"eq": True}, "external": {"eq": False},
                 "rentable": {"eq": True}, "direct_port_count": {"gte": 2}}
DESC = ("Serverless variant of qwen38-27b-rtx3090-single. Derived image serves "
        "vLLM on :18000 (the port Vast PyWorker targets, no socat relay) and "
        "bakes pyworker + venv so boot skips clone/install; Xet disabled so the "
        "weight download shows progress. ssh runtype suppresses the entrypoint "
        "so onstart drives the model server; VLLM_API_KEY is unset so the "
        "readiness benchmark can reach the model. Scale-to-zero ready: "
        "min_load=0, cold_workers=0, positive inactivity_timeout.")
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

suffix = sys.argv[1] if len(sys.argv) > 1 else ""
desc = (DESC + suffix)[-512:]  # desc cap is 512 chars
payload = {
    "name": NAME, "image": IMAGE, "tag": TAG,
    "href": HREF, "repo": REPO, "env": ENV,
    "onstart": onstart, "jup_direct": False, "ssh_direct": True,
    "use_jupyter_lab": False, "runtype": RUNTYPE, "use_ssh": True,
    "jupyter_dir": None, "docker_login_repo": None,
    "extra_filters": EXTRA_FILTERS,
    "recommended_disk_space": 50.0, "readme": None, "readme_visible": True,
    "desc": desc, "private": True,
}
res = api("/template/", payload, "POST")
print(json.dumps(res.get("msg", res))[:200])

# Verify what the server stored.
new_id = res.get("template", {}).get("id") or res.get("id")
if new_id:
    t = [x for x in api("/users/current/templates/")["templates"]
         if x["id"] == new_id][0]
    print("new id:", t["id"], "hash:", t["hash_id"])
    print("onstart identical:", t["onstart"] == onstart)
