#!/usr/bin/env python3
"""watch_boot.py — monitor a Vast.ai instance through its full serverless boot.

Usage:
    watch_boot.py <instance_id> [max_minutes] [template_hash]

Environment overrides:
    VAST_API_KEY      Vast API key (takes precedence)
    VAST_API_KEY_PATH  default ~/.config/vastai/vast_api_key
    WATCH_EXCLUDE_FILE default /tmp/excluded_hosts.txt  (host/machine ids never to reuse)

Strategy
  Phase A (no container yet): the one-line status_msg must change every <=5 min,
                              else destroy + rotate to next offer.
  Phase B (container exists): progress = daemon-log growth (Extra Debug Log API:
                              PUT /api/v0/instances/request_logs/<id>/ -> result_url)
                              UNION vllm.log byte-size growth read over SSH. The
                              latter matters because onstart redirects vLLM output
                              to /var/log/portal/vllm.log, so the long HF weight
                              download (~19.5 GB on a fresh host) is invisible in
                              the daemon log and would otherwise look like a stall.
                              With Xet disabled in the derived image, that log now
                              also carries the hf download progress bar.
  WIN  : PRIMARY — SSH internal probe: vLLM http://127.0.0.1:18000/health == 200
         AND pyworker benchmark max_perf > 0 AND latest error_msg empty
         (probed INSIDE the instance; external :3000 is often firewalled by
         hosts, and the gateway has no /v1/models or /health route at all).
         FALLBACK — daemon-log milestones (vllm ready + pyworker running) when
         SSH is unavailable, plus any 200 on external https://<ip>:3000/health.
  FAIL : OOM / traceback markers => exit 4 immediately.
  ROTATE: live offer scan gated on driver>=580, downlink>=800Mbps, reliability>=0.98,
          excluding every host burned so far (never reuse a failed host).

Exit codes: 0 booted, 1 timeout, 2 no offers left, 3 create gave up, 4 hard failure marker.
Stdlib only; assumes the `vastai` CLI is on PATH.
"""
import json, os, re, socket, ssl, subprocess, sys, time, urllib.request

# ---------------------------------------------------------------- config
ID_ = int(sys.argv[1])
MAXMIN = float(sys.argv[2] if len(sys.argv) > 2 else 15)
TPL_HASH = sys.argv[3] if len(sys.argv) > 3 else os.environ.get('WATCH_TEMPLATE_HASH', '')
KEY_PATH = os.path.expanduser(os.environ.get('VAST_API_KEY_PATH', '~/.config/vastai/vast_api_key'))


def get_key():
    """$VAST_API_KEY wins, then VAST_API_KEY_PATH / the default key file."""
    key = os.environ.get('VAST_API_KEY')
    if key:
        return key.strip()
    try:
        return open(KEY_PATH).read().strip()
    except OSError:
        sys.exit(f"No Vast API key: set $VAST_API_KEY or write {KEY_PATH}")
EXCL_FILE = os.environ.get('WATCH_EXCLUDE_FILE', '/tmp/excluded_hosts.txt')
STALL_LIMIT = 480                      # seconds of zero progress before rotating
OPENAI_BASE = 'https://console.vast.ai'
LABEL = 'sl-boot-test4'
DISK_GB = '50'

KEY = get_key()
SSH_OPTS = ['-o', 'StrictHostKeyChecking=accept-new', '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=15', '-o', 'LogLevel=SILENT']
# One round trip inside the instance: vLLM health, pyworker gateway, vllm.log size.
REMOTE_PROBE = (
    'h=$(curl -s -o /dev/null -w %{http_code} --max-time 3 '
    'http://127.0.0.1:18000/health 2>/dev/null || echo 000); '
    'p=$(grep -a -o \'"max_perf": [0-9.]*\' /opt/pyw/pyworker.log 2>/dev/null | tail -1 | grep -a -o \'[0-9.]*$\'); '
    'e=$(grep -a -o \'"error_msg": "[^"]*"\' /opt/pyw/pyworker.log 2>/dev/null | tail -1); '
    'if [ "$e" = \'"error_msg": ""\' ]; then e=1; else e=0; fi; '
    's=$(wc -c </var/log/portal/vllm.log 2>/dev/null || echo 0); '
    'echo "H=$h P=${p:-0} E=$e S=$s"'
)
AUTH = {'Authorization': f'Bearer {KEY}', 'Content-Type': 'application/json'}
_CTX = ssl._create_unverified_context()

def tcp_open(host, port, timeout=5):
    """Plain TCP connect test (no TLS). Used as a liveness signal: PyWorker
    binds :3000 early in boot, long before it answers HTTP."""
    if not host:
        return None
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False

def ts(): return time.strftime('%H:%M:%S')
def log(m): print(f'[{ts()}] {m}', flush=True)

def sh(args):
    return subprocess.run(args, capture_output=True, text=True).stdout

def http(url, method='GET', data=None, hdrs=None, timeout=15):
    """Returns (status_code_or_None, body_text). Never raises."""
    req = urllib.request.Request(url, method=method,
                                 data=data.encode() if isinstance(data, str) else data)
    for k, v in (hdrs or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            return r.status, r.read().decode(errors='replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors='replace')
    except Exception:
        return None, ''

# ---------------------------------------------------------------- API helpers
def inst_raw():
    out = sh(['vastai', 'show', 'instance', str(ID_), '--raw'])
    try:
        return json.loads(out)
    except Exception:
        return {}

def exclude_inst(rec):
    ids = [str(rec[k]) for k in ('host_id', 'machine_id') if rec.get(k)]
    if not ids:
        return
    cur = set(l.strip() for l in open(EXCL_FILE) if l.strip())
    cur.update(ids)
    open(EXCL_FILE, 'w').write('\n'.join(sorted(cur)))
    log(f'excluding host/machine ids {ids}')

def load_excl():
    try:
        return set(l.strip() for l in open(EXCL_FILE) if l.strip())
    except FileNotFoundError:
        return set()

def daemon_log():
    """Fetch the Extra Debug Log: request it, get presigned S3 url, download text."""
    url = f'{OPENAI_BASE}/api/v0/instances/request_logs/{ID_}/?daemon_logs=true'
    _, resp = http(url, 'PUT', json.dumps({'tail': 5000, 'daemon_logs': 'true', 'filter': ''}), AUTH)
    m = re.search(r'"result_url":\s*"([^"]+)"', resp or '')
    if not m:
        return ''
    time.sleep(4)   # backend needs a moment to publish the file
    _, txt = http(m.group(1), 'GET', timeout=20)
    return txt

def health_probe(ip):
    """External probe; many hosts firewall inbound to published ports."""
    if not ip:
        return None
    code, body = http(f'https://{ip}:3000/health', 'GET', timeout=8)
    return (code, body[:200]) if code is not None else None

_SSH_TARGETS = {}
def ssh_target(iid):
    """Parse `vastai ssh-url` into (user, host, port); memoized per instance."""
    if iid not in _SSH_TARGETS:
        try:
            u = sh(['vastai', 'ssh-url', str(iid)]).strip()
            m = re.match(r'ssh://([^@]+)@([^:]+):(\d+)', u)
            _SSH_TARGETS[iid] = tuple(m.groups()) if m else None
        except Exception:
            _SSH_TARGETS[iid] = None
    return _SSH_TARGETS[iid]

def internal_probe(iid):
    """(vllm_health_code, pyworker_models_code, vllm_log_bytes) from INSIDE the
    instance, or None when SSH is not usable yet. Authoritative readiness:"""
    t = ssh_target(iid)
    if not t:
        return None
    user, host, port = t
    try:
        out = subprocess.run(['ssh'] + SSH_OPTS + ['-p', port, f'{user}@{host}', REMOTE_PROBE],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        mm = dict(re.findall(r'([HPES])=(\S+)', out))
        if len(mm) == 4:
            try:
                perf = float(mm['P'] or 0)
            except ValueError:
                perf = 0.0
            return mm['H'], perf, mm['E'], int(mm['S'] or 0)
    except Exception:
        pass
    return None

def pick_next_offer(excl):
    q = ('gpu_name=RTX_3090 num_gpus=1 verified eq true external eq false '
         'rentable eq true direct_port_count gte 2 disk_space gte 50')
    out = sh(['vastai', 'search', 'offers', q, '--raw'])
    try:
        offers = json.loads(out)
    except Exception:
        return None, []
    cands = []
    for o in offers:
        drv = int(str(o.get('driver_version') or '0').split('.')[0] or 0)
        if (drv >= 580 and int(o.get('inet_down') or 0) >= 800
                and (o.get('reliability') or 0) >= 0.98
                and str(o.get('host_id') or '') not in excl
                and str(o.get('machine_id') or '') not in excl):
            cands.append(o)
    def cost(o):
        for f in ('discounted_hourly', 'dph_total_adj', 'dph_total'):
            if o.get(f):
                return o[f]
        return 99
    cands.sort(key=lambda o: (-int(o.get('inet_down') or 0), cost(o)))
    top = [(o['id'], round(cost(o), 4), int(o.get('inet_down') or 0), o.get('geolocation'))
           for o in cands[:5]]
    return (cands[0]['id'] if cands else None), top

def create_on(offer_id):
    out = sh(['vastai', 'create', 'instance', str(offer_id), '--template_hash', TPL_HASH,
              '--disk', DISK_GB, '--label', LABEL]).strip()
    m = re.search(r"'new_contract': (\d+)", out)
    ok = bool(re.search(r"'success': True", out))
    return (m.group(1) if m else None), ok, out

# ---------------------------------------------------------------- milestones
GOOD_MARKERS = {
    'vllm_ready':       re.compile(r'Application startup complete', re.I),
    'pyworker_running': re.compile(r'(?:Running|Starting)[^\n]*workers\.vllm\.worker', re.I),
}
BAD_MARKERS = {
    'oom':      re.compile(r'Out of memory|oom-kill', re.I),
    'traceback': re.compile(r'Traceback \(most recent call last\)', re.I),
}
INFORMATIONAL = [
    ('Already exists', 'layers_cached'),
    ('Pull complete', 'pull_complete'),
    ('Successfully pulled', 'image_pulled'),
    ('Uvicorn running', 'uvicorn_up'),
    ('served_model_name=', 'model_name_logged'),
]
seen_info = set()

def scan_markers(text, good_found):
    global seen_info
    for pat, name in INFORMATIONAL:
        rx = re.compile(pat, re.I)
        if name not in seen_info and rx.search(text):
            seen_info.add(name)
            m = rx.findall(text)
            extra = f'  value={m[-1]}' if name == 'model_name_logged' else ''
            log(f'MILESTONE: {name}{extra}')
    for name, rx in GOOD_MARKERS.items():
        if rx.search(text):
            good_found[name] = True
    for name, rx in BAD_MARKERS.items():
        if rx.search(text):
            log(f'HARD FAILURE MARKER: {name}')
            sys.exit(4)

# ---------------------------------------------------------------- main loop
if not TPL_HASH:
    log('WARNING: no template hash given (arg3 or WATCH_TEMPLATE_HASH); rotation will fail')

excl = load_excl()
log(f'watching instance {ID_} up to {MAXMIN} min | excluded hosts: {sorted(excl)}')
deadline = time.time() + MAXMIN * 60
last_sig = '__init__'; stall_start = time.time()
phase = 'A'; offset = 0
good_found = {}
ever_ran = False

while time.time() < deadline:
    rec = inst_raw()
    status = rec.get('actual_status') or '?'
    msg = rec.get('status_msg') or ''
    ever_ran = ever_ran or status == 'running'

    # --- win check 1 (PRIMARY): internal probe, probed inside the instance.
    # NOTE: the :3000 gateway has NO /v1/models or /health route (both 404 even
    # on a fully loaded worker, verified 2026-09-06), so readiness is vLLM
    # healthy + benchmark max_perf>0 + empty error_msg in the worker status.
    pr = internal_probe(ID_)
    if pr:
        vh, perf, eclean, vsz = pr
        if vh == '200' and perf > 0 and eclean == '1':
            log(f'>>> BOOT COMPLETE (internal): vLLM /health=200, pyworker max_perf={perf}, no error')
            open('/tmp/pull_winners', 'w').write(
                json.dumps({'instance_id': ID_, 'host_id': rec.get('host_id'),
                            'internal': True}) + '\n')
            sys.exit(0)

    # --- win check 2: external 200 (bonus; hosts often firewall :3000)
    h = health_probe(rec.get('public_ipaddr') or '')
    if h:
        log(f'>>> EXTERNAL HEALTH {h[0]} :: {h[1][:120]}')
        if h[0] == 200:
            open('/tmp/pull_winners', 'w').write(
                json.dumps({'instance_id': ID_, 'host_id': rec.get('host_id'),
                            'internal': False}) + '\n')
            sys.exit(0)

    # --- collect progress signal
    if status != 'loading':
        phase = 'B'
    if phase == 'A':
        sig = 'sm|' + msg
    else:
        txt = daemon_log()
        nb = len(txt.encode())
        if nb > offset:
            scan_markers(txt[offset:], good_found)
            lines = [l for l in txt.splitlines() if l.strip()]
            log(f'daemon-log grew ({nb}B); last: {(lines[-1] if lines else "(empty)")[:110]}')
            offset = nb
        vl = pr[3] if pr else 0   # vllm.log bytes: heartbeat during weight download
        ext = 'E?' if rec.get('public_ipaddr') is None else ('EO' if tcp_open(rec.get('public_ipaddr'), 3000) else 'EC')
        sig = f'dl|{nb}|vl|{vl}|{ext}'

    # --- win check 3 (fallback when SSH unavailable): both daemon-log milestones
    if ever_ran and pr is None and good_found and all(good_found.values()):
        log('>>> BOOT COMPLETE (milestone fallback: vllm_ready + pyworker_running)')
        open('/tmp/pull_winners', 'w').write(
            json.dumps({'instance_id': ID_, 'host_id': rec.get('host_id'),
                        'internal': False, 'milestones_only': True}) + '\n')
        sys.exit(0)

    # --- stall detection
    now = time.time()
    if sig != last_sig:
        last_sig, stall_start = sig, now
        if phase == 'A':
            log(f'[A] {msg[:110]}')
    elif now - stall_start >= STALL_LIMIT:
        elapsed = int(now - stall_start)
        log(f'!!! STALLED {elapsed}s (phase {phase}): {msg[:100]}')
        exclude_inst(inst_raw())
        sh(['vastai', 'destroy', 'instance', str(ID_), '-y'])
        time.sleep(12)
        offer, top = pick_next_offer(load_excl())
        if not offer:
            log('NO QUALIFIED NON-EXCLUDED OFFERS LEFT'); sys.exit(2)
        log(f'candidates: {top}')
        newid, ok, _ = create_on(offer)
        log(f'rotate -> offer {offer} success:{ok} id:{newid}')
        if not (ok and newid):
            exclude_inst(inst_raw())
            time.sleep(60)
            offer, top = pick_next_offer(load_excl())
            if not offer:
                log('NO QUALIFIED NON-EXCLUDED OFFERS LEFT'); sys.exit(2)
            newid, ok, _ = create_on(offer)
            if not newid:
                log('GIVING UP'); sys.exit(3)
            log(f'rotate2 -> offer {offer} id:{newid}')
        ID_ = int(newid)
        phase = 'A'; offset = 0; last_sig = '__reset__'; stall_start = time.time()
        continue
    time.sleep(60)

log(f'TIMEOUT {MAXMIN}min; id={ID_} phase={phase} status={status} msg={msg[:100]}')
sys.exit(1)
