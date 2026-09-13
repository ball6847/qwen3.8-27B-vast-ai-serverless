# qwen3.8-27B — Vast.ai serverless

Runs Qwen3.8-27B (W4A16 AutoRound) on a single RTX 3090, either as a
scale-to-zero Vast.ai serverless endpoint or as a plain vLLM instance.

- **Serverless** (`SERVERLESS=1`, default) — vLLM behind Vast PyWorker on
  `:3000`. Scale-to-zero: `$0` while idle.
- **Plain instance** (`SERVERLESS=0`) — vLLM alone. No PyWorker, nothing
  registers with Vast.

Full setup, endpoint/workergroup creation and troubleshooting: [DEPLOY.md](DEPLOY.md).

## API key

The platform injects `VLLM_API_KEY` into the container. What happens to it
depends on the mode, and the difference matters:

| Mode | `VLLM_API_KEY` | Why |
|---|---|---|
| `SERVERLESS=1` (default) | **Stripped** (`env -u`) | PyWorker sends no `Authorization` header; a key would 401 every request and fail the readiness benchmark. Safe: vLLM is loopback-only behind PyWorker's authenticated `:3000` gateway. |
| `SERVERLESS=0` | **Kept** | There is no PyWorker gateway. vLLM binds `0.0.0.0` and is **unauthenticated unless a key is set** — with no key, the endpoint is open to anyone who reaches the host port. |

### On a plain instance, set a key

If you expose a `SERVERLESS=0` instance, pass a key or the model is open:

```bash
docker run -d --name qwen --gpus all --ipc=host \
  -p 18000:18000 \
  -e SERVERLESS=0 \
  -e VLLM_API_KEY=<secret> \
  ghcr.io/ball6847/qwen3.8-27b-vast-ai-serverless:latest
```

Generate one with `openssl rand -hex 24`. `onstart.sh` warns in the log if
`SERVERLESS=0` is set and no key is present:

```
onstart: SERVERLESS=0 and no VLLM_API_KEY set -- vLLM will serve
UNAUTHENTICATED on 0.0.0.0. Set VLLM_API_KEY to require a bearer token.
```

Call it with the key:

```bash
curl http://<host>:18000/v1/chat/completions \
  -H "Authorization: Bearer <secret>" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.8-27b","messages":[{"role":"user","content":"hi"}],"max_tokens":64}'
```

Leave `VLLM_API_KEY` unset only for a box you reach solely over loopback or
a trusted private network.

### On the serverless endpoint, use the Vast key

Do not use `VLLM_API_KEY` here — it is stripped. The public face is
`https://openai.vast.ai/<ENDPOINT_NAME>`, authenticated with your **Vast API
key**:

```bash
curl https://openai.vast.ai/qwen38-27b-sl/v1/chat/completions \
  -H "Authorization: Bearer $VAST_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.8-27b","messages":[{"role":"user","content":"hi"}],"max_tokens":64}'
```

Endpoint lookup is per-account: a key belonging to another account returns
`Endpoint '<name>' not found`.

## Image

```
ghcr.io/ball6847/qwen3.8-27b-vast-ai-serverless:latest
```

Extends [`ghcr.io/syv-ai/qwen38-27b-rtx3090`](https://github.com/syv-ai/qwen38-27b-rtx3090)
(vLLM 0.28.0, torch 2.13, CUDA 13.0) with three cold-start changes:

1. **vLLM on `:18000`** — the port PyWorker targets, so the socat relay the
   upstream image needs is gone.
2. **`HF_HUB_DISABLE_XET=1`** — restores the tqdm progress bar on the ~19.5 GB
   weight download, so a stalled host is visible during boot.
3. **PyWorker pre-baked at `/opt/pyw`** — code + venv + nltk corpus, so boot
   skips the clone/venv/pip install (start_server.sh skips it when the env
   already exists).

```bash
docker build -t ghcr.io/ball6847/qwen3.8-27b-vast-ai-serverless:latest .
docker push ghcr.io/ball6847/qwen3.8-27b-vast-ai-serverless:latest
```

Keep `PYWORKER_SHA` in sync between `Dockerfile` and `onstart.sh`.

### Host requirements: driver 580+ (CUDA 13)

The image ships CUDA 13.0 (`torch 2.13.0+cu130`). **CUDA 13.x requires an NVIDIA
driver of 580 or newer**, so rent hosts whose *CUDA* column reads **13.0+** —
the Vast offer list shows a `cuda_max_good` value, which tracks the host driver,
not the toolkit.

On an older host (e.g. driver 570 / `cuda_max_good: 12.8`) `torch.cuda.is_available()`
is False and the boot **restart-loops**: upstream `verify.sh` FAILs with
`torch cannot see a CUDA GPU`, `entrypoint.sh` exits 1 on that FAIL, and Vast
restarts the container — re-running `verify` each time, with no `PASS`-only exit.
If you see the same patch/model check block repeating in `vastai logs`, that is
the signature; check the `CUDA` column before blaming the image.

`scripts/create_template.py` pins `"cuda_max_good": {"gte": 13.0}` in
`extra_filters`. That only pre-filters the GUI's offer search — it does **not**
block renting an ineligible offer by hand, so confirm the column yourself.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `SERVERLESS` | `1` | Only read by `onstart.sh` (`runtype=ssh` templates). `1` = vLLM + PyWorker; `0` = vLLM only (also accepts `false`/`no`/`off`; anything unrecognized falls back to `1`). The `--plain` template ignores it entirely — it uses `runtype=args`, so the image's Docker entrypoint runs and `onstart.sh` never executes. |
| `VLLM_API_KEY` | injected by Vast | Bearer token. Stripped under `SERVERLESS=1`, kept under `SERVERLESS=0`. |
| `PORT` | `18000` | vLLM listen port (image default) |
| `CTX` | `fast` (image); template sets `long` | Context tier (`fast` / `long` / `huge`) |
| `PREFIX_CACHE` | `1` (template) | vLLM prefix caching |
| `HF_TOKEN` | `1` | Lifts HF per-IP rate limits on the weight download |

## Layout

| Path | Purpose |
|---|---|
| `Dockerfile` | Derived image (port, Xet, pre-baked PyWorker) |
| `onstart.sh` | Boot script; `SERVERLESS` toggle lives here |
| `DEPLOY.md` | Template → endpoint → workergroup → verify |
| `scripts/create_template.py` | Creates the Vast template (`--plain` for non-serverless) |
| `scripts/watch_boot.py` | Watches a boot to completion, rotates stalled hosts |
| `scripts/test_endpoint.py` | End-to-end smoke test through the proxy |

## Credits

The serving image this repo derives from — the quantized Qwen3.8-27B weights,
the vLLM patches, requant scripts and benchmarks behind the ~1,000 tok/s at 64
concurrent figure — is the work of **[syv-ai](https://github.com/syv-ai)**:

- Repository: <https://github.com/syv-ai/qwen38-27b-rtx3090>
- Base image: [`ghcr.io/syv-ai/qwen38-27b-rtx3090`](https://github.com/syv-ai/qwen38-27b-rtx3090/pkgs/container/qwen38-27b-rtx3090)
- Licence: Apache-2.0

This repo adds only the Vast.ai serverless packaging on top (port `:18000`,
Xet disabled, pre-baked PyWorker). All credit for the model serving work
belongs upstream — please star and support the original project.
