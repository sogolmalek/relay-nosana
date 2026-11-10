"""The relay-verify checks (receipt spec v0.2, docs/receipt-spec.md). Standard library only.

Everything is read from Solana and IPFS; nothing from Relay. An anchored receipt is checked against the
job's post and finish transactions, which can't be closed; the job account is only a cross-check while it
still exists (Nosana closes it about an hour after the job finishes). IPFS bytes are accepted only if they
hash back to the CID asked for, so a gateway can't lie. Each check returns ok=True, False, or None
(couldn't check: network).
"""

from __future__ import annotations

import base64
import gzip
import json
import os
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
from .solana import DEFAULT_RPC, JobAccount, RpcError, TxMismatch, job_from_rpc_response, job_from_txs, rpc_call

DEFAULT_GATEWAYS = ["https://nosana.mypinata.cloud/ipfs/", "https://ipfs.io/ipfs/", "https://dweb.link/ipfs/"]
UA = "relay-verify/0.2"
RPC_ENV = "RELAY_VERIFY_RPC"


def default_rpc() -> str:
    """--rpc, else $RELAY_VERIFY_RPC, else the public mainnet RPC. Old receipts need an archival RPC: one that
    still serves getTransaction for the slots of the receipt's post_tx and finish_tx."""
    return os.environ.get(RPC_ENV) or DEFAULT_RPC


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

    def __init__(self, rpc: str | None = None, gateways: list[str] | None = None, timeout: float = 60):
        self.rpc, self.gateways, self.timeout = rpc or default_rpc(), gateways or DEFAULT_GATEWAYS, timeout
        self._acct: dict[str, JobAccount | None] = {}
        self._tx: dict[str, dict | None] = {}
        self._ipfs: dict[str, bytes] = {}

    def _rpc(self, method: str, params: list) -> dict:
        last = None
        for attempt in range(5):
            try:
                return rpc_call(self.rpc, method, params, self.timeout)
            except (urllib.error.URLError, TimeoutError, RpcError, OSError) as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise Unavailable(f"Solana RPC {self.rpc}: {last}")

    def transaction(self, signature: str) -> dict | None:
        """getTransaction result, or None if this RPC has no record of it (unknown, or older than its history)."""
        if signature not in self._tx:
            self._tx[signature] = self._rpc("getTransaction", [signature, {
                "encoding": "json", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}]).get("result")
        return self._tx[signature]

    def account(self, address: str) -> JobAccount | None:
        """The live job account, or None once Nosana has closed it (the verifier then relies on the transactions)."""
        if address not in self._acct:
            resp = self._rpc("getAccountInfo", [address, {"encoding": "base64", "commitment": "confirmed"}])
            self._acct[address] = job_from_rpc_response(address, resp)
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
    add("schema", "receipt has the v0.2 shape", not errs, "; ".join(errs))
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
    chain = jd = result = None

    # 1. the job's post and finish transactions succeeded and belong to this job; the result they point to
    #    shows the container succeeded; the live job account, if Nosana hasn't closed it yet, agrees
    name1 = "job finished on Solana"
    try:
        post, fin = f.transaction(n["post_tx"]), f.transaction(n["finish_tx"])
        missing = [w for w, t in (("post_tx", post), ("finish_tx", fin)) if t is None]
        if missing:
            add("1", name1, None, f"RPC {f.rpc} has no record of {' and '.join(missing)}. Verifying a receipt "
                "older than the RPC's history needs an archival RPC (--rpc or RELAY_VERIFY_RPC)")
        else:
            chain = job_from_txs(n["job"], n["post_tx"], post, n["finish_tx"], fin)
            result = json.loads(f.ipfs(chain["ipfs_result"]))
            rc = check_result(result)
            bad = list(rc["reasons"])
            acct = f.account(n["job"])
            if acct is None:
                note = "job account closed by Nosana after the job; checked from its transactions"
            else:
                note = "job account still open and agrees with the transactions"
                if acct.state != 2:
                    bad.append(f"job account state is {acct.state_name}")
                for k in ("ipfs_job", "ipfs_result", "node", "market"):
                    if getattr(acct, k) != chain[k]:
                        bad.append(f"job account {k} is {getattr(acct, k)}, transactions say {chain[k]}")
            add("1", name1, not bad, "; ".join(bad) if bad else note)
    except TxMismatch as e:
        add("1", name1, False, str(e))
    except Unavailable as e:
        add("1", name1, None, str(e))

    # 2. job definition from IPFS hashes to its CID, and the post transaction listed that CID
    try:
        jd = json.loads(f.ipfs(n["job_definition_cid"]))
        if chain is None:
            c1 = next(c for c in checks if c.id == "1").ok
            add("2", "job definition is the one the job ran", None if c1 is None else False,
                "no post transaction to compare with")
        else:
            ok = chain["ipfs_job"] == n["job_definition_cid"]
            add("2", "job definition is the one the job ran", ok,
                "" if ok else f"the post transaction lists ipfsJob {chain['ipfs_job']}")
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
    if chain is None or result is None:
        add("5", "output is in the job's result", None if chain is None else False, "result unavailable")
    else:
        bad = []
        if chain["ipfs_result"] != n["result_cid"]:
            bad.append(f"the finish transaction's ipfsResult is {chain['ipfs_result']}")
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

    # 6. node (the finish signer), market and block times match the transactions; container times the result
    if chain is None:
        add("6", "node and times match", None, "no transactions")
    else:
        bad = []
        for field in ("node", "market", "listed_at", "finished_at"):
            if n[field] != chain[field]:
                bad.append(f"{field}: the transactions say {chain[field]}")
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
