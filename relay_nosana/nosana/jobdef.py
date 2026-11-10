"""Build the public job definition for one batch: image by digest, model@revision, the runner's
code, the inputs (gzip+base64, so the inputs are covered by the job definition's CID) and the
`results` regexes. Job definitions are public on IPFS: no secrets, synthetic data only."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from pathlib import Path

from relay_verify.cid import cidv0_of_bytes
from relay_verify.jcs import canonical

RUNNER = Path(__file__).resolve().parents[2] / "batch_runner" / "run.py"

# Phase 0: v0.10.2 is in the 3090 market's required_images (cached on its nodes); pinned by digest.
IMAGE = "docker.io/vllm/vllm-openai@sha256:607442e407b0fea97f8a132a78b787c121a996dd4de181fa08e8da06e71ec2db"
MODEL = "Qwen/Qwen2.5-7B-Instruct"
REVISION = "a09a35458c702b33eeacc393d103063234e8bc28"
MARKET_3090 = "CA5pMpqkYFKtme7K31pNB1s62X2SdhEv1nN9RdxKCpuQ"

RESULTS = {
    "merkle_root": "RELAY_MERKLE_ROOT=([0-9a-f]{64})",
    "count": "RELAY_COUNT=([0-9]+)",
    "vllm_version": "RELAY_VLLM_VERSION=(\\S+)",
    "download_s": "RELAY_T_DOWNLOAD_S=([0-9.]+)",
    "deadline": "RELAY_DEADLINE (stage=\\S+ elapsed_s=[0-9]+)",
    "import_s": "RELAY_T_IMPORT_S=([0-9.]+)",
    "load_s": "RELAY_T_LOAD_S=([0-9.]+)",
    "gen_s": "RELAY_T_GEN_S=([0-9.]+)",
    "inputs_sha256": "RELAY_INPUTS_SHA256=([0-9a-f]{64})",
}


def inputs_bytes(lines: list[dict]) -> bytes:
    return b"".join(canonical(x) + b"\n" for x in lines)


def build(lines: list[dict], schema: dict, run_id: str, tag: str, task: str,
          image: str = IMAGE, model: str = MODEL, revision: str = REVISION, *, phase: str = "1",
          keep_schema_order: bool = False, output_keys: list[str] | None = None,
          deadline_s: int = 2700, runner_path: Path = RUNNER, results: dict | None = None,
          extra_env: dict | None = None) -> tuple[dict, bytes, str]:
    """(job definition, inputs.jsonl bytes, inputs CID).
    keep_schema_order: pass the schema with its key order as written (guided decoding generates keys in
    schema order; JCS would sort `reasoning` after `decision`). output_keys: what the receipts hash."""
    schema_bytes = (json.dumps(schema, separators=(",", ":"), ensure_ascii=False).encode()
                    if keep_schema_order else canonical(schema))
    raw = inputs_bytes(lines)
    inputs_cid = cidv0_of_bytes(raw)
    runner = runner_path.read_text()
    jd = {
        "version": "0.1",
        "meta": {"trigger": "api", "relay": {
            "phase": phase, "run_id": run_id, "tag": tag, "task": task, "count": len(lines),
            "inputs_cid": inputs_cid, "inputs_sha256": hashlib.sha256(raw).hexdigest(),
            "runner_sha256": hashlib.sha256(runner.encode()).hexdigest(),
            "data": "synthetic"}},
        "ops": [{
            "type": "container/run",
            "id": f"relay-{tag}",
            "args": {
                "image": image,
                "entrypoint": ["python3", "-c"],
                "cmd": [runner],
                "env": {
                    "MODEL": model, "REVISION": revision,
                    "RELAY_INPUTS_GZ_B64": base64.b64encode(gzip.compress(raw, mtime=0)).decode(),
                    # base64: a raw JSON env value reached the container altered (Phase 1, batch attempt 1)
                    "RELAY_OUTPUT_SCHEMA_B64": base64.b64encode(schema_bytes).decode(),
                    "HF_HUB_ENABLE_HF_TRANSFER": "0", "HF_HUB_DISABLE_PROGRESS_BARS": "1",
                    "VLLM_LOGGING_LEVEL": "WARNING",
                    "RELAY_DEADLINE_S": str(deadline_s), "RELAY_HEARTBEAT_S": "180",
                    **({"RELAY_OUTPUT_KEYS": ",".join(output_keys)} if output_keys else {}),
                    **(extra_env or {}),
                },
                "gpu": True,
                "required_vram": 20,
            },
            "results": RESULTS if results is None else results,
        }],
    }
    if len(canonical(jd)) > 262144:
        raise ValueError("job definition over 256 KiB: split the batch")
    return jd, raw, inputs_cid
