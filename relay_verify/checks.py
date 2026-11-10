"""The relay-verify checks (receipt spec v0.1, SPEC_V2 section 7). Standard library only.

Everything is read from Solana and IPFS; nothing from Relay. IPFS bytes are accepted only if they
hash back to the CID asked for, so a gateway can't lie. Each check returns ok=True, False, or None
(couldn't check: network).
"""

from __future__ import annotations

import base64
import gzip
import json
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass

from .cid import cidv0_of_bytes
from .jcs import sha256_hex
from .merkle import leaf_hash, root as merkle_root, root_from_path
from .outputs import container_times, parse_outputs, regex_result
from .result import check_result
from .schema import validate
from .solana import DEFAULT_RPC, JobAccount, RpcError, job_from_rpc_response, rpc_call

DEFAULT_GATEWAYS = ["https://nosana.mypinata.cloud/ipfs/", "https://ipfs.io/ipfs/", "https://dweb.link/ipfs/"]
UA = "relay-verify/0.1"


class Unavailable(Exception):
    """Couldn't reach Solana or IPFS (exit code 2, not a failed check)."""


@dataclass
class Check:
    id: str
    name: str
    ok: bool | None
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


class Fetcher:
    """Solana JSON-RPC and IPFS gateways, cached per run. Tests substitute a fake."""

    def __init__(self, rpc: str = DEFAULT_RPC, gateways: list[str] | None = None, timeout: float = 60):
        self.rpc, self.gateways, self.timeout = rpc, gateways or DEFAULT_GATEWAYS, timeout
        self._acct: dict[str, JobAccount | None] = {}
        self._ipfs: dict[str, bytes] = {}

    def account(self, address: str) -> JobAccount | None:
        if address not in self._acct:
            last = None
            for attempt in range(5):
                try:
                    resp = rpc_call(self.rpc, "getAccountInfo",
                                    [address, {"encoding": "base64", "commitment": "confirmed"}], self.timeout)
                    self._acct[address] = job_from_rpc_response(address, resp)
                    break
                except (urllib.error.URLError, TimeoutError, RpcError, OSError) as e:
                    last = e
                    time.sleep(2 * (attempt + 1))
            else:
                raise Unavailable(f"Solana RPC {self.rpc}: {last}")
        return self._acct[address]

    def ipfs(self, cid: str) -> bytes:
        if cid not in self._ipfs:
            notes = []
            for gw in self.gateways:
                try:
                    req = urllib.request.Request(gw + cid, headers={"User-Agent": UA})
                    with urllib.request.urlopen(req, timeout=self.timeout) as r:
                        data = r.read()
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    notes.append(f"{gw}: {e}")
                    continue
                if cidv0_of_bytes(data) == cid:
                    self._ipfs[cid] = data
                    break
                notes.append(f"{gw}: bytes do not hash to {cid}")
            else:
                raise Unavailable(f"IPFS {cid}: " + "; ".join(notes))
        return self._ipfs[cid]


def receipt_id_of(receipt: dict) -> str:
    return sha256_hex({k: v for k, v in receipt.items() if k != "receipt_id"})


def _inputs_from_jobdef(jd: dict) -> tuple[bytes, list[dict]]:
    env = jd["ops"][0]["args"].get("env") or {}
    raw = gzip.decompress(base64.b64decode(env["RELAY_INPUTS_GZ_B64"]))
    return raw, [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


def verify(receipt: dict, f: Fetcher) -> list[Check]:
    checks: list[Check] = []

    def add(cid, name, ok, reason=""):
        checks.append(Check(cid, name, ok, reason))

    errs = validate(receipt)
    add("schema", "receipt has the v0.1 shape", not errs, "; ".join(errs))
    if errs:
        return checks
    rid = receipt_id_of(receipt)
    add("receipt_id", "receipt_id is the hash of the receipt", rid == receipt["receipt_id"],
        "" if rid == receipt["receipt_id"] else f"computed {rid}")
    if receipt["grade"] == "declared":
        add("declared", "declared: not verifiable against a public record", True,
            f"provider {receipt['provider']['name']} says model {receipt['provider']['model']} ran "
            + (f"(request {receipt['provider']['request_id']})" if "request_id" in receipt["provider"]
               else f"(session {receipt['provider']['session_id']})"))
        return checks

    n, m, mdl = receipt["nosana"], receipt["merkle"], receipt["model"]
    acct = jd = result = None

    # 1. job account exists, COMPLETED, and its result shows the container succeeded
    try:
        acct = f.account(n["job"])
        if acct is None:
            add("1", "job completed on Solana", False, f"no job account at {n['job']}")
        elif acct.state != 2:
            add("1", "job completed on Solana", False, f"job state is {acct.state_name}")
        elif not acct.ipfs_result:
            add("1", "job completed on Solana", False, "job has no result CID (stopped before finishing?)")
        else:
            result = json.loads(f.ipfs(acct.ipfs_result))
            rc = check_result(result)
            add("1", "job completed on Solana", rc["ok"], "; ".join(rc["reasons"]))
    except Unavailable as e:
        add("1", "job completed on Solana", None, str(e))

    # 2. job definition from IPFS hashes to its CID, and the job account points to it
    try:
        jd = json.loads(f.ipfs(n["job_definition_cid"]))
        if acct is None:
            c1 = next(c for c in checks if c.id == "1").ok
            add("2", "job definition is the one the job ran", None if c1 is None else False,
                "no job account to compare with")
        else:
            ok = acct.ipfs_job == n["job_definition_cid"]
            add("2", "job definition is the one the job ran", ok,
                "" if ok else f"job account ipfsJob is {acct.ipfs_job}")
    except Unavailable as e:
        add("2", "job definition is the one the job ran", None, str(e))

    # 3. image digest and model revision in the job definition match the receipt
    if jd is None:
        add("3", "image and model revision match", None, "job definition unavailable")
    else:
        args = jd["ops"][0]["args"]
        env = args.get("env") or {}
        weights = f"{env.get('MODEL')}@{env.get('REVISION')}"
        bad = []
        if args.get("image") != mdl["image"]:
            bad.append(f"job definition image is {args.get('image')}")
        if weights != mdl["weights"]:
            bad.append(f"job definition weights are {weights}")
        add("3", "image and model revision match", not bad, "; ".join(bad))

    # 4. inputs: the job definition carries inputs_cid, and the item's input hashes to input_sha256
    if jd is None:
        add("4", "input is in the job's inputs", None, "job definition unavailable")
    else:
        bad = []
        meta_cid = ((jd.get("meta") or {}).get("relay") or {}).get("inputs_cid")
        if meta_cid != n["inputs_cid"]:
            bad.append(f"job definition names inputs {meta_cid}")
        raw, lines = _inputs_from_jobdef(jd)
        if cidv0_of_bytes(raw) != n["inputs_cid"]:
            bad.append("embedded inputs do not hash to inputs_cid")
        line = next((x for x in lines if x.get("item_id") == receipt["item_id"]), None)
        if line is None:
            bad.append(f"item {receipt['item_id']} is not in the inputs")
        elif sha256_hex(line) != receipt["input_sha256"]:
            bad.append("the item's input does not hash to input_sha256")
        add("4", "input is in the job's inputs", not bad, "; ".join(bad))

    # 5. result: on-chain result CID, the item's output, its Merkle path, and the batch root
    if acct is None or result is None:
        add("5", "output is in the job's result", None if acct is None else False, "result unavailable")
    else:
        bad = []
        if acct.ipfs_result != n["result_cid"]:
            bad.append(f"job account ipfsResult is {acct.ipfs_result}")
        rows, summary = parse_outputs(result)
        row = rows.get(receipt["item_id"])
        if sha256_hex(receipt["output"]) != receipt["output_sha256"]:
            bad.append("output does not hash to output_sha256")
        if row is None:
            bad.append(f"item {receipt['item_id']} is not in the result")
        else:
            if row["output_sha256"] != receipt["output_sha256"]:
                bad.append("the result's output for this item differs")
            if row["input_sha256"] != receipt["input_sha256"]:
                bad.append("the result's input hash for this item differs")
        leaf = leaf_hash(receipt["item_id"], receipt["input_sha256"], receipt["output_sha256"])
        if root_from_path(leaf, m["path"]) != m["root"]:
            bad.append("the Merkle path does not lead to merkle.root")
        roots = {"summary": (summary or {}).get("merkle_root"), "regex": regex_result(result, "merkle_root"),
                 "recomputed": merkle_root(list(rows.values())) if rows else None}
        for k, v in roots.items():
            if v != m["root"]:
                bad.append(f"the result's {k} root is {v}")
        add("5", "output is in the job's result", not bad, "; ".join(bad))

    # 6. node, market and times match the chain and the result
    if acct is None:
        add("6", "node and times match", None, "no job account")
    else:
        bad = []
        for field, chain in (("node", acct.node), ("market", acct.market),
                             ("listed_at", acct.time_start), ("finished_at", acct.time_end)):
            if n[field] != chain:
                bad.append(f"{field}: chain says {chain}")
        if result is not None:
            cs, ce = container_times(result)
            if (n["container_start"], n["container_end"]) != (cs, ce):
                bad.append(f"container times: result says {cs}..{ce}")
        add("6", "node and times match", not bad, "; ".join(bad))
    return checks


def verdict(checks: list[Check]) -> int:
    """0 verified, 1 a check failed, 2 couldn't check."""
    if any(c.ok is False for c in checks):
        return 1
    if any(c.ok is None for c in checks):
        return 2
    return 0
