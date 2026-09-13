# qwen38-27b Serverless — Deploy Guide

Reproducible setup for serving Qwen3.8-27B on Vast.ai serverless GPU workers:
template -> endpoint + workergroup -> verified serving.

Conventions: `<TPL_HASH>`, `<EP_ID>`, `<WG_ID>`, `<INSTANCE_ID>` are values
recorded during the procedure. `~` is the operator's home directory.

## 0. Overview

Four components, created in order:

1. **Template** — the machine definition: container image, Docker options
   (env), on-start script, and offer filters. Immutable once created;
   any change means delete + recreate, which yields a new id and hash.
   The hash is what instances and workergroups pin to.
2. **Endpoint** — the public routing + scaling policy: a name clients call,
   load floors, queue behavior, inactivity timeout, and worker caps.
   Holds no compute itself.
3. **Workergroup** — the fleet bound to one endpoint: pins the template hash
   and the GPU search query the autoscaler recruits from. The autoscaler
   creates, monitors, and destroys workers to match demand.
4. **Instance (worker)** — a single GPU box recruited by the autoscaler.
   Ephemeral: created on demand, stopped after idle timeout, destroyed when
   surplus or replaced. Never addressed directly by clients.

Request path: client -> OpenAI-compatible proxy
(`https://openai.vast.ai/<ENDPOINT_NAME>`) -> autoscaler queue ->
ready worker's `:3000` gateway -> vLLM `:18000` (PyWorker targets `:18000`
directly; the derived image serves vLLM there, so there is no relay).

Scaling configuration used here (set on the endpoint / workergroup):

| Setting | Value | Effect |
|---|---|---|
| `min_load` / `min_cold_load` | 0 | No provisioned floor; zero demand means zero workers desired |
| `cold_workers` | 0 | No stopped standby pool; total scale-to-zero (slower cold starts, ~$0 idle) |
| `inactivity_timeout` | 600 | After 600 s without traffic, active workers stand down |
| `max_workers` | 3 | Cap on parallel workers; bounds cost and hedging |
| `target_util` | 0.9 | Autoscaler target capacity utilization |
| `max_queue_time` / `target_queue_time` | 30 / 10 s | Queue buffering before scale-up pressure |
| GPU class | RTX 3090, 24 GB (`gpu_ram>=23`) | Only hosts able to fit the 27B weights |

Cold-start latency budget: recruit 1-2 min + boot ~8-9 min with the derived
image (image pull ~5, ~19.5 GB weights ~5, engine load ~2; PyWorker is
pre-baked and Xet is disabled, which removes the runtime clone/install and
makes the download observable). Warm requests serve in seconds.

**0b. Build and push the derived image** (required — the upstream
`syv-ai` image lacks every change below).

```bash
docker build -t ghcr.io/ball6847/qwen3.8-27b-vast-ai-serverless:latest .
docker push ghcr.io/ball6847/qwen3.8-27b-vast-ai-serverless:latest
```

The image adds: vLLM on `:18000` (no socat), `HF_HUB_DISABLE_XET=1` for a
visible download progress bar, and a pre-baked PyWorker tree at `/opt/pyw`
(code + venv + nltk corpus) so boot skips clone/install. Keep `PYWORKER_SHA`
in the Dockerfile and `onstart.sh` in sync.

## 1. Prerequisites

- Vast.ai account with CLI access (`vastai show user` succeeds;
  key in `~/.config/vastai/vast_api_key`).
- Credit balance >= ~$5, otherwise endpoint creation fails with 403
  (`You need an additional $X to create endpoint`).
- This repo checked out. `onstart.sh` is the source of truth for boot logic.
- `VAST_API_KEY` env var holding the Vast API key for client calls.
  Endpoint lookup is per-account: any other key returns
  `Endpoint 'X' not found`.

## 2. Create the template (full definition)

The complete spec lives in `scripts/create_template.py` as constants.
Key values (kept in sync with the script):

- name: `qwen38-27b-rtx3090-single-serverless`
- image: `ghcr.io/ball6847/qwen3.8-27b-vast-ai-serverless`, tag `latest`
- runtype: `ssh` (suppresses the image entrypoint; onstart drives all)
- env: `-p 3000:3000 -e CTX=long -e PREFIX_CACHE=1 -e BACKEND=vllm -e SERVERLESS=true -e MODEL_NAME=qwen3.8-27b -e MODEL_HEALTH_ENDPOINT=/health`
- extra_filters: verified + non-external + rentable + `direct_port_count >= 2`
- onstart: repo `onstart.sh` verbatim. Stack:
  vLLM `:18000` <-- PyWorker `:3000` (no relay; PyWorker targets `:18000`).

Template updates are always delete + recreate (in-place `PUT` is rejected
server-side); the hash changes every time.

Non-ASCII bytes in onstart corrupt/truncate delivery at that byte, so the
script asserts pure ASCII before POST.

```bash
# 2a. Sanity: onstart.sh must be pure ASCII
grep -nP '[^\x00-\x7F]' onstart.sh && echo "FIX NON-ASCII FIRST" || echo "ASCII OK"

# 2b. Create (posts the full spec + worktree onstart.sh)
python3 scripts/create_template.py "[optional desc suffix]"
# -> prints new template id + hash, and verifies stored onstart. RECORD BOTH.
export TPL_HASH=<printed hash>
```

## 3. Create endpoint + workergroup

```bash
vastai create endpoint --endpoint_name qwen38-27b-sl \
  --min_load 0 --min_cold_load 0 --cold_workers 0 \
  --inactivity_timeout 600 --max_workers 3 --target_util 0.9
# -> note EP_ID
vastai create workergroup --template_hash $TPL_HASH --endpoint_id <EP_ID>
# -> note WG_ID
```

## 4. Constrain the GPU class

Fresh workergroups default to `gpu_ram 8` — an 8 GB card cannot fit 27B
weights. Pin the verified class:

```bash
vastai update workergroup <WG_ID> --endpoint_id <EP_ID> --gpu_ram 24 \
  --search_params 'gpu_name=RTX_3090 num_gpus=1 verified eq true external eq false rentable eq true rented eq false direct_port_count gte 2 disk_space gte 50 gpu_ram>=23'
```

## 5. Wire the client

Use the OpenAI-compatible proxy. Direct `run.vast.ai/route/` calls queue
with zero workload and never recruit a worker.

- Base URL: `https://openai.vast.ai/<ENDPOINT_NAME>`
- Key: Vast API key (`$VAST_API_KEY`)
- `model`: any non-empty string (proxy ignores it)

Example provider entry (OpenAI-completions compatible clients):

```json
{
  "api": "openai-completions",
  "apiKey": "$VAST_API_KEY",
  "baseUrl": "https://openai.vast.ai/qwen38-27b-sl",
  "compat": {
    "supportsDeveloperRole": false,
    "supportsReasoningEffort": true,
    "maxTokensField": "max_tokens",
    "supportsStore": false
  },
  "models": [{
    "id": "qwen3.8-27b", "name": "Qwen3.8 27B (Vast.ai Serverless)",
    "contextWindow": 153600, "maxTokens": 8192, "reasoning": true,
    "input": ["text"],
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
  }]
}
```

## 6. Verify: recruit -> serve -> scale to zero

```bash
# First call triggers a cold start (~13-17 min). The proxy times out at ~5
# min: retry until served.
python3 scripts/test_endpoint.py "say hi" 100   # exit 0 = real completion
vastai get endpt-workers <EP_ID>                # loading -> idle, measured_perf ~300
# Burst, then silence. About 11 min after last traffic the worker reads stopped:
vastai get endpt-workers <EP_ID>
vastai show instances   # only parked boxes remain; ~$0/h
```

Expected results: recruit within ~2 min of first demand, single and
parallel burst requests served by one worker (~290-365 tok/s benchmark),
stand-down to stopped ~1 min after the 600 s inactivity timeout.

A standalone template check without the endpoint is available:
`scripts/watch_boot.py <INSTANCE_ID> <minutes> <TPL_HASH>` watches a
manually created instance to `BOOT COMPLETE` (vLLM healthy, benchmark
`max_perf > 0`, clean worker status) and rotates hosts on stall.

## 7. Operate

- **Cold start**: recruit 1-2 min + boot ~8-9 min (derived image). Warm
  requests serve in seconds. Clients must retry proxy 504s.
- **Hedge**: on cold start the autoscaler may boot up to `max_workers`
  boxes and cull the losers once one serves (~$0.10/cycle). Lowering
  `max_workers` removes redundancy.
- **Ghost block**: an exited (scaled-to-zero) worker record can block
  re-recruit (observed: 15+ min of queued demand, zero attempts). Fix:
  `vastai destroy instance <EXITED_ID>`; fresh recruit follows within ~2 min.
  `cold_workers=1` keeps a stopped box (~$0, cached weights) for faster
  restarts if cold starts become frequent.
- **Boundaries**: parked/prod instances are outside the workergroup and
  unaffected by scaling. Endpoints belonging to other projects must not be
  modified.

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Benchmark `Cannot connect 127.0.0.1:18000` forever, vLLM healthy | vLLM is not listening on `:18000` | Confirm the template uses the derived image (it sets `PORT=18000`); the upstream image listens on `:18020`. |
| Watchdog never declares a win on a working box | `:3000` gateway has no `/v1/models` or `/health` route (404 even when loaded) | Watchdog reads `max_perf` + `error_msg` via SSH instead. |
| Benchmark fails once at boot, never retries | Benchmark runs once per log marker; stale error blocks capacity | Requires a clean boot. Manual repair: start relay, re-append the marker line to vllm.log, restart pyworker. |
| `Endpoint 'X' not found` from proxy | Wrong API key (endpoint lookup is per-account) | Use the Vast key. |
| 403 `insufficient credit ... to create endpoint` | Balance below $5 (1st endpoint) / $10 (2nd) | Top up. |
| Demand queues, zero recruit attempts | Ghost exited worker, or raw `/route/` calls (load 0) | Destroy exited records; call via OpenAI proxy. |
| SSH `Permission denied` on fresh box | Key propagation takes 10+ min sometimes | Wait; `vastai execute` works meanwhile, including on stopped boxes. |
| Daemon-log silent mid-pull | S3 telemetry lag (minutes), not necessarily stuck | Check `status_msg` changes before rotating; a single silent window is not proof of a stuck host. |

## 9. Full teardown (start over)

```bash
vastai delete workergroup <WG_ID>
vastai delete endpoint <EP_ID>
vastai delete template --template-id <TPL_ID>
vastai show instances                 # destroy anything left over
# Then redo sections 2-6.
```
