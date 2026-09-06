#!/usr/bin/env python3
"""create_template.py - create the qwen38-27b serverless template, fully explicit.

Usage:
    create_template.py [desc_suffix]

The complete template definition lives below as constants (no cloning from
another template id). Posts worktree onstart.sh as the on-start command.
Template PUT is broken server-side, so every change is delete + recreate;
the hash changes each time - record the printed id + hash.

Asserts pure-ASCII onstart before POST (non-ASCII delivery corrupts).
Stdlib only. Key read from ~/.config/vastai/vast_api_key.
"""
import json
import os
import sys
import urllib.request
import urllib.error

BASE = "https://console.vast.ai/api/v0"

# --- Full template definition (explicit; edit here, not via clone) ---
NAME = "qwen38-27b-rtx3090-single-serverless"
IMAGE = "ghcr.io/syv-ai/qwen38-27b-rtx3090"
TAG = "latest"
HREF = "https://github.com/vast-ai/pyworker"
REPO = "ghcr.io/syv-ai/qwen38-27b-rtx3090"
RUNTYPE = "ssh"  # ssh runtype suppresses the image entrypoint; onstart drives all
ENV = ("-p 3000:3000 -e CTX=long -e PREFIX_CACHE=1 -e BACKEND=vllm "
       "-e SERVERLESS=true -e MODEL_NAME=qwen3.8-27b "
       "-e MODEL_HEALTH_ENDPOINT=/health")
EXTRA_FILTERS = {"verified": {"eq": True}, "external": {"eq": False},
                 "rentable": {"eq": True}, "direct_port_count": {"gte": 2}}
DESC = ("Serverless variant of qwen38-27b-rtx3090-single. Drives the image's "
        "own entrypoint (ssh runtype suppresses it), relays vLLM :18020 -> "
        ":18000 for the public Vast PyWorker on :3000 (pinned pyworker SHA + "
        "SDK version), unsets the platform-injected VLLM_API_KEY so the "
        "worker's readiness benchmark can reach the model. Scale-to-zero "
        "ready: min_load=0, cold_workers=0, positive inactivity_timeout.")
ONSTART_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "onstart.sh")
KEY_PATH = os.path.expanduser("~/.config/vastai/vast_api_key")


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


KEY = open(KEY_PATH).read().strip()
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
