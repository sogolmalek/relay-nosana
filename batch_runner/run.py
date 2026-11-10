"""Relay batch runner: runs inside docker.io/vllm/vllm-openai@sha256:... on a Nosana node.

Standard library + vLLM only. The job definition passes this file's text to `python3 -c`, so the
exact code that ran is part of the pinned job definition (its SHA-256 is in meta.relay.runner_sha256).

Input:  env RELAY_INPUTS_GZ_B64 = base64(gzip(inputs.jsonl)); one JSON object per line:
        {"item_id", "task", "messages": [...], "params": {"temperature": 0, "max_tokens", "seed"}}
        env MODEL, REVISION, RELAY_OUTPUT_SCHEMA_B64 (base64 of the JSON Schema for guided decoding;
        base64 because the node altered a raw JSON env value in the first Phase 1 attempt).
Output: stdout lines, parsed by the verifier from the node-pinned result:
        RELAY_OUT <canonical outputs.jsonl line>     one per item, in item_id order
        RELAY_SUMMARY <canonical summary line>       {"merkle_root","count","model","revision","vllm_version"}
        RELAY_MERKLE_ROOT=<hex> RELAY_COUNT=<n> and timing lines, extracted by the job's `results` regexes.

The canonical-JSON and Merkle code below is a copy of relay_verify/jcs.py and relay_verify/merkle.py;
tests/test_receipts.py checks the copies produce identical bytes and roots.
"""

import base64
import gzip
import hashlib
import json
import math
import os
import sys
import threading
import time
from decimal import Decimal


# ---------------------------------------------------------------- canonical JSON (RFC 8785)

def _num(x):
    if isinstance(x, bool):
        raise TypeError("bool is not a number")
    if isinstance(x, int):
        if abs(x) > 2**53:
            raise ValueError("integer outside 2**53")
        return str(x)
    if not math.isfinite(x):
        raise ValueError("NaN/Infinity")
    if x == 0:
        return "0"
    if x.is_integer() and abs(x) < 1e21:
        return str(int(x))
    sign = "-" if x < 0 else ""
    t = Decimal(repr(abs(x))).as_tuple()
    digits = "".join(map(str, t.digits)).rstrip("0") or "0"
    k = len(digits)
    n = t.exponent + len(t.digits)
    if k <= n <= 21:
        s = digits + "0" * (n - k)
    elif 0 < n <= 21:
        s = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        s = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        s = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if e > 0 else "-") + str(abs(e))
    return sign + s


def _ser(v, out):
    if v is None:
        out.append("null")
    elif v is True:
        out.append("true")
    elif v is False:
        out.append("false")
    elif isinstance(v, (int, float)):
        out.append(_num(v))
    elif isinstance(v, str):
        out.append(json.dumps(v, ensure_ascii=False))
    elif isinstance(v, (list, tuple)):
        out.append("[")
        for i, item in enumerate(v):
            if i:
                out.append(",")
            _ser(item, out)
        out.append("]")
    elif isinstance(v, dict):
        out.append("{")
        for i, k in enumerate(sorted(v, key=lambda s: s.encode("utf-16-be"))):
            if i:
                out.append(",")
            out.append(json.dumps(k, ensure_ascii=False))
            out.append(":")
            _ser(v[k], out)
        out.append("}")
    else:
        raise TypeError("not JSON: " + type(v).__name__)


def canonical(obj):
    out = []
    _ser(obj, out)
    return "".join(out).encode("utf-8")


def sha256_hex(obj):
    return hashlib.sha256(canonical(obj)).hexdigest()


# ---------------------------------------------------------------- Merkle (receipt spec v0.1)

def leaf_hash(item_id, input_sha256, output_sha256):
    return hashlib.sha256(b"\x00" + item_id.encode("utf-8") + bytes.fromhex(input_sha256)
                          + bytes.fromhex(output_sha256)).digest()


def merkle_root(rows):
    level = [leaf_hash(r["item_id"], r["input_sha256"], r["output_sha256"])
             for r in sorted(rows, key=lambda r: r["item_id"])]
    if not level:
        raise ValueError("empty tree")
    while len(level) > 1:
        nxt = [hashlib.sha256(b"\x01" + level[i] + level[i + 1]).digest() for i in range(0, len(level) - 1, 2)]
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    return level[0].hex()


# ---------------------------------------------------------------- batch logic (no vLLM; testable)

def parse_inputs(gz_b64):
    raw = gzip.decompress(base64.b64decode(gz_b64))
    return raw, [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


def parse_output(text, schema):
    """Parsed output, or {"parse_error": true} when the text isn't a JSON object with the schema's keys."""
    try:
        obj = json.loads(text.strip())
    except Exception:
        return {"parse_error": True}
    if not isinstance(obj, dict):
        return {"parse_error": True}
    props = (schema or {}).get("properties") or {}
    if props:
        if set(obj) != set(props):
            return {"parse_error": True}
        for key, spec in props.items():
            if "enum" in spec and obj[key] not in spec["enum"]:
                return {"parse_error": True}
    return obj


def build_outputs(inputs, texts, schema):
    """outputs.jsonl rows (sorted by item_id) and the Merkle root, from raw model texts."""
    rows = []
    for inp, text in zip(inputs, texts):
        out = parse_output(text, schema)
        rows.append({"item_id": inp["item_id"], "input_sha256": sha256_hex(inp), "output": out,
                     "output_raw": text, "output_sha256": sha256_hex(out)})
    rows.sort(key=lambda r: r["item_id"])
    return rows, merkle_root(rows)


# ---------------------------------------------------------------- main (inside the container)

def _sampling(SamplingParams, schema, p):
    try:
        from vllm.sampling_params import GuidedDecodingParams
        return SamplingParams(temperature=p["temperature"], max_tokens=p["max_tokens"], seed=p["seed"],
                              guided_decoding=GuidedDecodingParams(json=schema)), "guided_decoding"
    except ImportError:
        from vllm.sampling_params import StructuredOutputsParams
        return SamplingParams(temperature=p["temperature"], max_tokens=p["max_tokens"], seed=p["seed"],
                              structured_outputs=StructuredOutputsParams(json=schema)), "structured_outputs"


_STAGE = {"name": "start"}


def _cache_bytes(model):
    root = os.path.join(os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface"), "hub",
                        "models--" + model.replace("/", "--"))
    total = 0
    for d, _, files in os.walk(root):
        for f in files:
            try:
                total += os.lstat(os.path.join(d, f)).st_size
            except OSError:
                pass
    return total


def _watch(t0, model, deadline_s, every_s):
    """Heartbeat (stage, elapsed, bytes downloaded) and an in-container deadline: past it, exit 3 so the
    node publishes a failed result with these logs, instead of the job being stopped with no result."""
    while True:
        time.sleep(every_s)
        el = time.time() - t0
        print("RELAY_HEARTBEAT stage=%s elapsed_s=%.0f hf_cache_bytes=%d" % (_STAGE["name"], el,
                                                                            _cache_bytes(model)), flush=True)
        if el > deadline_s:
            print("RELAY_DEADLINE stage=%s elapsed_s=%.0f" % (_STAGE["name"], el), flush=True)
            os._exit(3)


def main():
    t0 = time.time()
    print("RELAY_T_CONTAINER_START=%.3f" % t0, flush=True)
    threading.Thread(target=_watch, args=(t0, os.environ["MODEL"], float(os.environ.get("RELAY_DEADLINE_S", "2700")),
                                          float(os.environ.get("RELAY_HEARTBEAT_S", "180"))), daemon=True).start()
    raw, inputs = parse_inputs(os.environ["RELAY_INPUTS_GZ_B64"])
    schema = json.loads(base64.b64decode(os.environ["RELAY_OUTPUT_SCHEMA_B64"]))
    print("RELAY_INPUTS_SHA256=%s" % hashlib.sha256(raw).hexdigest(), flush=True)
    _STAGE["name"] = "import"
    from vllm import LLM, SamplingParams
    import vllm
    t1 = time.time()
    print("RELAY_T_IMPORT_S=%.1f" % (t1 - t0), flush=True)
    model, revision = os.environ["MODEL"], os.environ["REVISION"]
    _STAGE["name"] = "download"
    from huggingface_hub import snapshot_download
    snapshot_download(model, revision=revision)
    td = time.time()
    print("RELAY_T_DOWNLOAD_S=%.1f" % (td - t1), flush=True)
    _STAGE["name"] = "load"
    llm = LLM(model=model, revision=revision, tokenizer_revision=revision, max_model_len=4096,
              seed=0, enforce_eager=True)
    t2 = time.time()
    print("RELAY_T_LOAD_S=%.1f" % (t2 - t1), flush=True)  # download + load, as in Phase 0
    _STAGE["name"] = "generate"
    params, mode = [], None
    for inp in inputs:
        sp, mode = _sampling(SamplingParams, schema, inp["params"])
        params.append(sp)
    print("RELAY_GUIDED=%s" % mode, flush=True)
    outs = llm.chat([inp["messages"] for inp in inputs], params, use_tqdm=False)
    texts = [o.outputs[0].text for o in outs]
    t3 = time.time()
    print("RELAY_T_GEN_S=%.1f" % (t3 - t2), flush=True)
    rows, root = build_outputs(inputs, texts, schema)
    for row in rows:
        print("RELAY_OUT " + canonical(row).decode("utf-8"), flush=True)
    summary = {"merkle_root": root, "count": len(rows), "model": model, "revision": revision,
               "vllm_version": vllm.__version__}
    print("RELAY_SUMMARY " + canonical(summary).decode("utf-8"), flush=True)
    print("RELAY_VLLM_VERSION=%s" % vllm.__version__, flush=True)
    print("RELAY_COUNT=%d" % len(rows), flush=True)
    print("RELAY_MERKLE_ROOT=%s" % root, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
