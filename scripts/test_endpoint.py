#!/usr/bin/env python3
"""test_endpoint.py - end-to-end smoke test for the qwen38-27b-sl serverless endpoint.

Calls the Vast OpenAI-compatible proxy exactly like an OpenAI client would:
    POST https://openai.vast.ai/<ENDPOINT_NAME>/chat/completions

Usage:
    test_endpoint.py [message] [max_tokens]

Env:
    VAST_API_KEY      Vast API key (default: read ~/.config/vastai/vast_api_key)
    ENDPOINT_NAME     default qwen38-27b-sl

Exit codes: 0 served a real completion, 1 HTTP/API error, 2 timeout.
Stdlib only.
"""
import json
import os
import sys
import urllib.request
import urllib.error

ENDPOINT = os.environ.get("ENDPOINT_NAME", "qwen38-27b-sl")
BASE = f"https://openai.vast.ai/{ENDPOINT}"
TIMEOUT = int(os.environ.get("ENDPOINT_TIMEOUT", "570"))


def get_key():
    key = os.environ.get("VAST_API_KEY")
    if key:
        return key.strip()
    return open(os.path.expanduser("~/.config/vastai/vast_api_key")).read().strip()


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, method="POST",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {get_key()}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:1000]
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"[:300]


def main():
    message = sys.argv[1] if len(sys.argv) > 1 else "Reply with exactly: ENDPOINT OK"
    max_tokens = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    attempts = int(os.environ.get("ENDPOINT_ATTEMPTS", "4"))
    payload = {"model": "qwen3.8-27b",
               "messages": [{"role": "user", "content": message}],
               "max_tokens": max_tokens, "temperature": 0}
    # A cold start takes ~12 min but the proxy gives up after ~5, so retry
    # on 504 until a worker is up.
    for attempt in range(1, attempts + 1):
        print(f"POST {BASE}/chat/completions (attempt {attempt}/{attempts})")
        status, body = post("/chat/completions", payload)
        print(f"HTTP {status}")
        print(body[:800])
        if status == 504 and attempt < attempts:
            print("cold boot in progress, waiting 4 min before retry...")
            import time
            time.sleep(240)
            continue
        if status == 200:
            try:
                msg = json.loads(body)["choices"][0]["message"]
                text = msg.get("content") or msg.get("reasoning") or ""
            except (KeyError, IndexError, ValueError):
                print("RESULT: 200 but unparseable body")
                return 1
            if text.strip():
                print("RESULT: SERVED OK")
                return 0
            print("RESULT: 200 but empty completion")
            return 1
    print("RESULT: FAILED")
    return 1 if status else 2


if __name__ == "__main__":
    sys.exit(main())
