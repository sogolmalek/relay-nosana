#!/usr/bin/env python3
"""Phase 0 day-one checks against the live Nosana Jobs API, Solana and IPFS.

Standard library only. Run from the repo root on a machine with network access:

    export NOSANA_API_KEY=...   PINATA_JWT=...
    .venv/bin/python scripts/phase0.py discover          # no credits spent
    .venv/bin/python scripts/phase0.py whoami            # debug a 401: key shape + validate-api-key
    .venv/bin/python scripts/phase0.py hello --market <MARKET>
    .venv/bin/python scripts/phase0.py rerun --market <MARKET> [--node <NODE>]
    .venv/bin/python scripts/phase0.py history <JOB_ADDRESS>
    .venv/bin/python scripts/phase0.py vllm --market <GPU_MARKET>

Every raw response is written to tests/fixtures/phase0/ (no keys are ever written).
Every posted job is stopped in a `finally` block unless it reached COMPLETED.
Cumulative Phase 0 spend is tracked in tests/fixtures/phase0/spend.json; the script refuses
to post once the $10 Phase 0 cap would be crossed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from relay_verify.cid import cidv0_of_bytes  # noqa: E402
from relay_verify.solana import (  # noqa: E402
    DEFAULT_RPC,
    decode_assign_ix,
    decode_finish_ix,
    decode_list_ix,
    get_account_raw,
    job_from_rpc_response,
    rpc_call,
)
from relay_verify.b58 import b58decode  # noqa: E402
from relay_verify.result import check_result  # noqa: E402
import re  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "phase0"
SPEND = FIX / "spend.json"
PHASE0_CAP_USD = 3.0  # lowered from $10 on 2026-11-09: about $7.20 of credit is left

CLIENT_MANAGER_URLS = ["https://client-manager.k8s.prd.nosana.com", "https://api.nosana.com"]
HOST_MANAGER = "https://host-manager.k8s.prd.nosana.com"
INDEXER = "https://blockchain-indexer.k8s.prd.nos.ci"
PINATA_API = "https://api.pinata.cloud"
# Nosana nodes fetch job definitions only via nosana.mypinata.cloud, which serves only CIDs pinned to
# Nosana's Pinata account. @nosana/kit pins with a public community key (see @nosana/ipfs defaults).
NOSANA_IPFS_DEFAULTS = ROOT / ".nosana-npm" / "node_modules" / "@nosana" / "ipfs" / "dist" / "defaults" / "index.js"
GATEWAYS = ["https://nosana.mypinata.cloud/ipfs/", "https://ipfs.io/ipfs/", "https://dweb.link/ipfs/"]
UA = "relay-nosana-phase0/0.1"
HELLO = "RELAY_PHASE0_HELLO_7f3a"


# ---------------------------------------------------------------- utilities

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def save(name: str, obj) -> Path:
    FIX.mkdir(parents=True, exist_ok=True)
    p = FIX / name
    if isinstance(obj, (bytes, bytearray)):
        p.write_bytes(obj)
    else:
        p.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    log(f"saved {p.relative_to(ROOT)}")
    return p


def canonical(obj) -> bytes:
    """Deterministic JSON (sorted keys, no whitespace). Full JCS lands in receipts/spec.py."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def http(method: str, url: str, *, headers=None, body: bytes | None = None, timeout=60):
    h = {"User-Agent": UA, **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def jbody(raw: bytes):
    try:
        return json.loads(raw)
    except Exception:
        return {"_raw_text": raw.decode(errors="replace")[:4000]}


def env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"{name} is not set (export it in this shell; never commit it)")
    return v


# ---------------------------------------------------------------- Nosana client-manager

class Nosana:
    def __init__(self, base: str, key: str):
        self.base, self.key = base.rstrip("/"), key

    def _h(self, extra=None):
        return {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", **(extra or {})}

    def balance(self):
        s, b, _ = http("GET", f"{self.base}/credits/balance", headers=self._h())
        return s, jbody(b)

    def post_job(self, ipfs_hash: str, market: str, timeout: int = 3600, node: str | None = None):
        body = {"ipfsHash": ipfs_hash, "market": market, "timeout": timeout}
        if node:
            body["node"] = node
        s, b, _ = http("POST", f"{self.base}/jobs/list", headers=self._h({"Idempotency-Key": str(uuid.uuid4())}),
                       body=json.dumps(body).encode())
        return s, jbody(b), body

    def stop_job(self, address: str):
        s, b, _ = http("POST", f"{self.base}/jobs/{address}/stop", headers=self._h(), body=b"{}")
        return s, jbody(b)

    def get_templates(self):
        s, b, _ = http("GET", f"{self.base}/templates/", headers=self._h())
        return s, jbody(b)


def pick_client(key: str) -> Nosana:
    for base in CLIENT_MANAGER_URLS:
        s, body = Nosana(base, key).balance()
        log(f"balance via {base}: HTTP {s}")
        if s == 200:
            return Nosana(base, key)
    sys.exit("no client-manager base URL accepted the key; run `discover` and read the fixtures")


def available_usd(bal: dict) -> float:
    """Credits left, in USD. Phase 0 found the balance is in dollars (e.g. settledCredits 2.804),
    not cents as docs/phase0-research.md assumed."""
    return (float(bal.get("assignedCredits", 0)) - float(bal.get("reservedCredits", 0))
            - float(bal.get("settledCredits", 0)))


def spent_so_far() -> float:
    if SPEND.exists():
        return float(json.loads(SPEND.read_text()).get("total_usd", 0.0))
    return 0.0


def record_spend(label: str, usd: float, detail: dict) -> None:
    data = json.loads(SPEND.read_text()) if SPEND.exists() else {"total_usd": 0.0, "entries": []}
    data["entries"].append({"label": label, "usd": usd, "at": int(time.time()), **detail})
    data["total_usd"] = round(sum(e["usd"] for e in data["entries"]), 4)
    save("spend.json", data)


# ---------------------------------------------------------------- IPFS

def nosana_ipfs_jwt() -> str:
    """The community Pinata key @nosana/kit pins with: NOSANA_IPFS_JWT from env, else read from the
    installed @nosana/ipfs package (npm install --prefix .nosana-npm @nosana/ipfs). Never logged or saved."""
    v = os.environ.get("NOSANA_IPFS_JWT", "").strip()
    if v:
        return v
    if NOSANA_IPFS_DEFAULTS.exists():
        m = re.search(r"jwt:\s*['\"]([^'\"]+)['\"]", NOSANA_IPFS_DEFAULTS.read_text())
        if m:
            return m.group(1)
    sys.exit("Nosana IPFS key not found: set NOSANA_IPFS_JWT or run "
             "`npm install --prefix .nosana-npm --ignore-scripts @nosana/ipfs@2.24.3`")


def pin_bytes(data: bytes, name: str, jwt: str) -> tuple[int, dict]:
    """Pin exact bytes as a single file with CIDv0 (pinFileToIPFS), so the CID is recomputable."""
    boundary = uuid.uuid4().hex
    parts = []

    def part(field, value, filename=None, ctype="application/json"):
        disp = f'form-data; name="{field}"' + (f'; filename="{filename}"' if filename else "")
        parts.append(f"--{boundary}\r\nContent-Disposition: {disp}\r\nContent-Type: {ctype}\r\n\r\n".encode()
                     + value + b"\r\n")

    part("file", data, filename=name)
    part("pinataOptions", json.dumps({"cidVersion": 0, "wrapWithDirectory": False}).encode())
    part("pinataMetadata", json.dumps({"name": name}).encode())
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    s, b, _ = http("POST", f"{PINATA_API}/pinning/pinFileToIPFS", body=body, timeout=120,
                   headers={"Authorization": f"Bearer {jwt}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
    return s, jbody(b)


def pin_json_like_kit(obj: dict, jwt: str) -> tuple[int, dict]:
    """What @nosana/kit's ipfs.pin does: POST JSON.stringify(data) to pinJSONToIPFS."""
    s, b, _ = http("POST", f"{PINATA_API}/pinning/pinJSONToIPFS", body=canonical(obj), timeout=120,
                   headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"})
    return s, jbody(b)


def pin_jobdef(tag: str, jd_bytes: bytes) -> str:
    """Pin the job definition to Nosana's Pinata account (so nodes can fetch it) and to ours (durability).
    Both must return our locally computed CIDv0, else stop and report."""
    local_cid = cidv0_of_bytes(jd_bytes)
    name = f"relay-phase0-{tag}.json"
    report = {"local_cidv0": local_cid}

    s, resp = pin_bytes(jd_bytes, name, nosana_ipfs_jwt())
    report["nosana_pinFileToIPFS"] = {"http_status": s, "body": resp}
    log(f"Nosana pinFileToIPFS: HTTP {s} cid={resp.get('IpfsHash')}")
    if s == 200:
        nosana_cid = resp.get("IpfsHash")
    else:
        s2, resp2 = pin_json_like_kit(json.loads(jd_bytes), nosana_ipfs_jwt())
        report["nosana_pinJSONToIPFS"] = {"http_status": s2, "body": resp2}
        nosana_cid = resp2.get("IpfsHash") if s2 == 200 else None
        log(f"Nosana pinJSONToIPFS: HTTP {s2} cid={nosana_cid}")
        save(f"{tag}_pin_response.json", report)
        sys.exit(f"Nosana's key refused pinFileToIPFS (HTTP {s}); pinJSONToIPFS gave CID {nosana_cid} "
                 f"vs local {local_cid}: stop and report")

    s, resp = pin_bytes(jd_bytes, name, env("PINATA_JWT"))
    report["own_pinFileToIPFS"] = {"http_status": s, "body": resp}
    own_cid = resp.get("IpfsHash") if s == 200 else None
    log(f"own Pinata pinFileToIPFS: HTTP {s} cid={own_cid}")
    save(f"{tag}_pin_response.json", report)
    if nosana_cid != local_cid or own_cid != local_cid:
        sys.exit(f"CID mismatch: local={local_cid} nosana={nosana_cid} own={own_cid}: stop and report")
    return local_cid


def fetch_ipfs(cid: str, tries: int = 10) -> tuple[bytes, str]:
    """Fetch raw bytes from any gateway and accept them only if they hash back to `cid`."""
    last = None
    for attempt in range(tries):
        for gw in GATEWAYS:
            s, b, _ = http("GET", gw + cid, timeout=60)
            if s == 200:
                if cidv0_of_bytes(b) == cid:
                    return b, gw
                last = f"{gw}: bytes do not hash to {cid}"
            else:
                last = f"{gw}: HTTP {s}"
        time.sleep(min(30, 5 * (attempt + 1)))
    raise RuntimeError(f"could not fetch {cid}: {last}")


# ---------------------------------------------------------------- job definitions

def hello_jobdef(inputs: list[dict]) -> dict:
    inputs_jsonl = b"".join(canonical(x) + b"\n" for x in inputs)
    inputs_b64 = base64.b64encode(inputs_jsonl).decode()
    script = (
        f'echo {HELLO}; '
        'echo "RELAY_INPUTS_SHA256=$(echo "$RELAY_INPUTS_B64" | base64 -d | sha256sum | cut -d" " -f1)"; '
        'echo "RELAY_INPUT_LINES=$(echo "$RELAY_INPUTS_B64" | base64 -d | wc -l)"; '
        'echo "RELAY_DATE=$(date -u +%s)"'
    )
    return {
        "version": "0.1",
        "meta": {"trigger": "api", "relay": {"phase": "0", "check": "hello",
                                             "inputs_sha256": hashlib.sha256(inputs_jsonl).hexdigest()}},
        "ops": [{
            "type": "container/run",
            "id": "relay-hello",
            "args": {
                "image": "docker.io/library/alpine:3.20",
                "cmd": ["/bin/sh", "-c", script],
                "env": {"RELAY_INPUTS_B64": inputs_b64},
                "gpu": False,
            },
            "results": {
                "hello": f"({HELLO})",
                "inputs_sha256": "RELAY_INPUTS_SHA256=([0-9a-f]{64})",
                "input_lines": "RELAY_INPUT_LINES=\\s*([0-9]+)",
            },
        }],
    }


VLLM_SCRIPT = r'''
import json, os, sys, time, hashlib
t0 = time.time()
print("RELAY_T_CONTAINER_START=%.3f" % t0, flush=True)
from vllm import LLM, SamplingParams
import vllm
t1 = time.time()
print("RELAY_T_IMPORT_S=%.1f" % (t1 - t0), flush=True)
llm = LLM(model=os.environ["MODEL"], revision=os.environ.get("REVISION") or None,
          max_model_len=4096, seed=0, enforce_eager=True)
t2 = time.time()
print("RELAY_T_LOAD_S=%.1f" % (t2 - t1), flush=True)
sp = SamplingParams(temperature=0, max_tokens=32, seed=0)
out = llm.chat([[{"role": "user", "content": "Reply with the single word: ready"}]], sp, use_tqdm=False)
t3 = time.time()
txt = out[0].outputs[0].text.strip()
print("RELAY_T_GEN_S=%.1f" % (t3 - t2), flush=True)
print("RELAY_VLLM_VERSION=%s" % vllm.__version__, flush=True)
print("RELAY_OUTPUT_SHA256=%s" % hashlib.sha256(txt.encode()).hexdigest(), flush=True)
print("RELAY_OUTPUT=%s" % json.dumps(txt), flush=True)
'''


def vllm_jobdef(image: str, model: str, revision: str) -> dict:
    return {
        "version": "0.1",
        "meta": {"trigger": "api", "relay": {"phase": "0", "check": "vllm-startup"}},
        "ops": [{
            "type": "container/run",
            "id": "relay-vllm-startup",
            "args": {
                "image": image,
                "entrypoint": ["python3", "-c"],
                "cmd": [VLLM_SCRIPT],
                "env": {"MODEL": model, "REVISION": revision, "HF_HUB_ENABLE_HF_TRANSFER": "0",
                        # vllm attempt 1 failed with "Failed to publish the job result"; keep logs small
                        "VLLM_LOGGING_LEVEL": "WARNING", "HF_HUB_DISABLE_PROGRESS_BARS": "1"},
                        # not TQDM_DISABLE: vllm 0.10.2 then divides by a zero elapsed time (attempt 3)
                "gpu": True,
                "required_vram": 20,
            },
            "results": {
                "import_s": "RELAY_T_IMPORT_S=([0-9.]+)",
                "load_s": "RELAY_T_LOAD_S=([0-9.]+)",
                "gen_s": "RELAY_T_GEN_S=([0-9.]+)",
                "vllm_version": "RELAY_VLLM_VERSION=(\\S+)",
                "output_sha256": "RELAY_OUTPUT_SHA256=([0-9a-f]{64})",
            },
        }],
    }


# ---------------------------------------------------------------- run one job end to end

def run_job(tag: str, jobdef: dict, market: str, rpc: str, node: str | None = None,
            max_usd: float = 2.0, queue_wait_s: int = 1200, run_wait_s: int = 1800) -> dict:
    key = env("NOSANA_API_KEY")
    client = pick_client(key)

    jd_bytes = canonical(jobdef)
    save(f"{tag}_jobdef.json", json.loads(jd_bytes))
    pinned_cid = pin_jobdef(tag, jd_bytes)
    log(f"job definition CID {pinned_cid} pinned to Nosana's and our Pinata accounts")

    s, bal_before = client.balance()
    save(f"{tag}_balance_before.json", bal_before)
    avail_usd = available_usd(bal_before)
    if spent_so_far() + max_usd > PHASE0_CAP_USD:
        sys.exit(f"Phase 0 cap: spent ${spent_so_far():.2f} + this job up to ${max_usd:.2f} > ${PHASE0_CAP_USD}")
    if avail_usd < max_usd:
        sys.exit(f"available credits ${avail_usd:.2f} < per-job cap ${max_usd:.2f}")

    t_post = time.time()
    s, resp, req = client.post_job(pinned_cid, market, 3600, node)
    save(f"{tag}_post_request.json", req)
    save(f"{tag}_post_response.json", {"http_status": s, "body": resp})
    if s != 200:
        sys.exit(f"POST /jobs/list failed: HTTP {s} {resp}")
    job = resp["job"]
    log(f"posted job {job} (tx {resp.get('tx')}), reserved {resp.get('credits')}")
    cost_reserved = float((resp.get("credits") or {}).get("costUSD") or 0.0)
    if cost_reserved > max_usd:
        log(f"reserved ${cost_reserved:.2f} exceeds per-job cap ${max_usd:.2f}; stopping")

    finished = False
    acct = None
    t_running = None
    try:
        if cost_reserved > max_usd:
            raise RuntimeError("over per-job cap")
        deadline_q = time.time() + queue_wait_s
        while True:
            raw = get_account_raw(rpc, job)
            acct = job_from_rpc_response(job, raw)
            state = acct.state_name if acct else "NOT_FOUND"
            # Phase 0: the job account stays QUEUED while a node runs it; the running state is the
            # separate run account (`run` in the POST response), which exists only while it runs.
            run_addr = resp.get("run")
            run_exists = bool(run_addr and (rpc_call(rpc, "getAccountInfo", [run_addr, {"encoding": "base64"}])
                                            .get("result") or {}).get("value"))
            if (run_exists or (acct and acct.state == 1)) and t_running is None:
                t_running = time.time()
                log(f"RUNNING (run account {run_addr} exists)")
            log(f"state={state} run_account={'yes' if run_exists else 'no'}")
            if acct and acct.state in (2, 3):
                save(f"{tag}_account_getAccountInfo.json", raw)
                save(f"{tag}_account_decoded.json", acct.to_dict())
                finished = acct.state == 2
                break
            if t_running is None and time.time() > deadline_q:
                raise RuntimeError(f"not picked up within {queue_wait_s}s")
            if t_running is not None and time.time() - t_running > run_wait_s:
                raise RuntimeError(f"still running after {run_wait_s}s")
            time.sleep(15)
    finally:
        if not finished:
            s2, stop = client.stop_job(job)
            save(f"{tag}_stop_response.json", {"http_status": s2, "body": stop})
            log(f"stopped job {job}: HTTP {s2}")
            record_spend(f"{tag}_stopped", cost_reserved, {"job": job, "reserved_usd": cost_reserved,
                         "note": "stopped before finishing; recorded at reservation (conservative)",
                         "t_running_seen": t_running is not None})

    t_done = time.time()
    s, bal_after = client.balance()
    save(f"{tag}_balance_after.json", bal_after)
    time.sleep(30)  # settlement may lag; read again
    s, bal_after2 = client.balance()
    save(f"{tag}_balance_after_30s.json", bal_after2)
    spent_usd = round(available_usd(bal_before) - available_usd(bal_after2), 4)
    record_spend(tag, max(spent_usd, 0.0), {"job": job, "reserved_usd": cost_reserved})

    summary = {"job": job, "tx": resp.get("tx"), "run": resp.get("run"), "market": market,
               "requested_node": node, "job_definition_cid": pinned_cid,
               "wall_post_to_running_s": round(t_running - t_post, 1) if t_running else None,
               "wall_post_to_done_s": round(t_done - t_post, 1),
               "credits_reserved_usd": cost_reserved, "credits_spent_usd": spent_usd,
               "account": acct.to_dict() if acct else None}

    if acct and acct.ipfs_job is not None:
        summary["check_ipfsJob_equals_pinned"] = acct.ipfs_job == pinned_cid
    if node and acct:
        summary["check_node_targeting"] = acct.node == node
    if acct and acct.ipfs_result:
        raw_res, gw = fetch_ipfs(acct.ipfs_result)
        save(f"{tag}_result_raw.json", raw_res)
        res = json.loads(raw_res)
        summary["result_cid"] = acct.ipfs_result
        summary["result_bytes"] = len(raw_res)
        summary["result_gateway"] = gw
        ops = res.get("opStates") or []
        logs = "".join((l.get("log") or "") for op in ops for l in (op.get("logs") or []))
        summary["result_logs_contain_hello"] = HELLO in logs
        summary["result_check"] = check_result(res)
        summary["result_op_results"] = [op.get("results") for op in ops]
        summary["result_exit_codes"] = [op.get("exitCode") for op in ops]
    save(f"{tag}_summary.json", summary)
    log(json.dumps(summary, indent=2))
    return summary


# ---------------------------------------------------------------- commands

def key_shape(key: str) -> dict:
    """Describe the key without revealing it, to debug 401s."""
    return {"length": len(key), "prefix": key[:4], "starts_with_nos_": key.startswith("nos_"),
            "has_whitespace": any(c.isspace() for c in key), "has_quotes": any(c in key for c in "\"'"),
            "looks_like_placeholder": key in {"...", "xxx", "<key>"} or key.startswith("<")}


def cmd_whoami(a) -> None:
    key = env("NOSANA_API_KEY")
    shape = key_shape(key)
    log(f"key shape: {shape}")
    out = {"key_shape": shape}
    for base in CLIENT_MANAGER_URLS:
        s, b, _ = http("POST", f"{base}/auth/validate-api-key", headers={"Content-Type": "application/json"},
                       body=json.dumps({"apiKey": key}).encode())
        body = jbody(b)
        # keep only non-personal fields in the fixture
        out[base] = {"http_status": s, "valid": body.get("valid"), "scopes": body.get("scopes"),
                     "has_user": bool(body.get("user")), "message": body.get("message")}
        log(f"validate-api-key {base}: HTTP {s} valid={body.get('valid')} scopes={body.get('scopes')} msg={body.get('message')}")
        sb, bb = Nosana(base, key).balance()
        out[base]["balance_http_status"] = sb
        out[base]["balance"] = bb if sb == 200 else bb.get("message")
        log(f"balance {base}: HTTP {sb} {bb}")
    save("whoami.json", out)


def cmd_discover(a) -> None:
    key = env("NOSANA_API_KEY")
    log(f"key shape: {key_shape(key)}")
    out = {}
    for base in CLIENT_MANAGER_URLS:
        s, body = Nosana(base, key).balance()
        out[base] = {"http_status": s, "body": body}
        log(f"balance {base}: HTTP {s}")
    save("discover_balance.json", out)
    ok = next((b for b in CLIENT_MANAGER_URLS if out[b]["http_status"] == 200), None)
    if ok:
        s, t = Nosana(ok, key).get_templates()
        save("discover_templates.json", {"http_status": s, "body": t})
    for path in ["/markets/", "/markets/remote-resources", "/markets/prices"]:
        s, b, _ = http("GET", HOST_MANAGER + path, headers={"Authorization": f"Bearer {key}"})
        save("discover_hostmanager" + path.strip("/").replace("/", "_") + ".json", {"http_status": s, "body": jbody(b)})
        log(f"host-manager {path}: HTTP {s}")
    s, b, _ = http("GET", INDEXER + "/jobs/stats")
    save("discover_indexer_jobs_stats.json", {"http_status": s, "body": jbody(b)})
    r = rpc_call(a.rpc, "getHealth", [])
    log(f"solana rpc {a.rpc}: {r.get('result')}")
    log("next: pick a cheap credit-eligible market from discover_hostmanager_markets.json and run `hello`")


def cmd_hello(a) -> None:
    inputs = [{"item_id": f"P-{i:04d}", "task": "payout_check", "amount_usd": 100 + i} for i in range(3)]
    run_job("hello", hello_jobdef(inputs), a.market, a.rpc, max_usd=a.max_usd)


def _indexer_jobs(market: str, state: str) -> list[dict]:
    q = urllib.parse.urlencode({"market": market, "state": state, "limit": 50})
    s, b, _ = http("GET", f"{INDEXER}/jobs?{q}")
    body = jbody(b)
    save(f"rerun_indexer_market_jobs_{state.lower()}.json", {"http_status": s, "body": body})
    return body if isinstance(body, list) else body.get("jobs") or body.get("data") or []


def nodes_in_market(market: str, exclude: set[str]) -> list[str]:
    """Nodes that completed jobs in this market recently and are not running one now."""
    busy = {j.get("node") for j in _indexer_jobs(market, "RUNNING") if isinstance(j, dict)}
    exclude = set(exclude) | busy
    items = _indexer_jobs(market, "COMPLETED")
    seen = []
    for j in items:
        n = j.get("node") if isinstance(j, dict) else None
        if n and n not in exclude and n not in seen:
            seen.append(n)
    return seen


def cmd_rerun(a) -> None:
    first = json.loads((FIX / "hello_summary.json").read_text())
    first_node = (first.get("account") or {}).get("node")
    node = a.node
    if not node:
        cands = nodes_in_market(a.market, {first_node} if first_node else set())
        if not cands:
            sys.exit("no other node found in this market; pass --node explicitly")
        node = cands[0]
    b58decode(node)  # validates
    log(f"first job ran on {first_node}; targeting {node}")
    inputs = [{"item_id": f"P-{i:04d}", "task": "payout_check", "amount_usd": 100 + i} for i in range(3)]
    s = run_job("rerun", hello_jobdef(inputs), a.market, a.rpc, node=node, max_usd=a.max_usd)
    same = s.get("result_op_results") == first.get("result_op_results")
    log(f"node targeting honoured: {s.get('check_node_targeting')}; same parsed results as first run: {same}")


def cmd_history(a) -> None:
    sigs = rpc_call(a.rpc, "getSignaturesForAddress", [a.job, {"limit": 50}])
    save(f"history_{a.job[:8]}_signatures.json", sigs)
    decoded = []
    for s in reversed(sigs.get("result", [])):
        tx = rpc_call(a.rpc, "getTransaction", [s["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 0}])
        res = tx.get("result") or {}
        msg = (res.get("transaction") or {}).get("message") or {}
        keys = msg.get("accountKeys") or []
        for ix in msg.get("instructions") or []:
            prog = keys[ix["programIdIndex"]] if ix.get("programIdIndex", -1) < len(keys) else None
            data = b58decode(ix.get("data", ""))
            for name, dec in (("list", decode_list_ix), ("assign", decode_assign_ix), ("finish", decode_finish_ix)):
                d = dec(data)
                if d:
                    decoded.append({"signature": s["signature"], "slot": s.get("slot"), "program": prog,
                                    "ix": name, **d})
    save(f"history_{a.job[:8]}_decoded.json", decoded)
    acct = job_from_rpc_response(a.job, get_account_raw(a.rpc, a.job))
    log(f"account still exists: {acct is not None}; decoded instructions: {json.dumps(decoded, indent=2)}")


def cmd_vllm(a) -> None:
    run_job("vllm", vllm_jobdef(a.image, a.model, a.revision), a.market, a.rpc, max_usd=a.max_usd,
            queue_wait_s=1800, run_wait_s=3300)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rpc", default=os.environ.get("SOLANA_RPC", DEFAULT_RPC))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("discover")
    sub.add_parser("whoami")
    h = sub.add_parser("hello"); h.add_argument("--market", required=True); h.add_argument("--max-usd", type=float, default=2.0)
    r = sub.add_parser("rerun"); r.add_argument("--market", required=True); r.add_argument("--node")
    r.add_argument("--max-usd", type=float, default=2.0)
    hi = sub.add_parser("history"); hi.add_argument("job")
    v = sub.add_parser("vllm"); v.add_argument("--market", required=True)
    v.add_argument("--image", default="docker.io/vllm/vllm-openai:v0.6.6")
    v.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct")
    v.add_argument("--revision", default="")
    v.add_argument("--max-usd", type=float, default=3.0)
    a = p.parse_args()
    {"discover": cmd_discover, "whoami": cmd_whoami, "hello": cmd_hello, "rerun": cmd_rerun, "history": cmd_history, "vllm": cmd_vllm}[a.cmd](a)


if __name__ == "__main__":
    main()
