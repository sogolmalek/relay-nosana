"""Read and decode a Nosana job account from Solana JSON-RPC. Standard library only.

Layout from @nosana/jobs-program 2.24.3 (Codama-generated), see docs/phase0-research.md.
"""

from __future__ import annotations

import base64
import json
import struct
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass

from .b58 import b58encode
from .cid import digest_to_cid

NOSANA_JOBS_PROGRAM = "nosJhNRqr2bc9g1nfGDcXXTXvYUmxD4cVwy2pMWhrYM"
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"

JOB_ACCOUNT_DISCRIMINATOR = bytes([91, 16, 162, 5, 45, 210, 125, 65])
JOB_ACCOUNT_SIZE = 233
LIST_DISCRIMINATOR = bytes([54, 174, 193, 67, 17, 41, 132, 38])
FINISH_DISCRIMINATOR = bytes([67, 74, 170, 132, 125, 233, 182, 37])
# Phase 0: a job posted with `node` (Jobs API node targeting) is created by `assign`, not `list`.
# Anchor discriminator sha256("global:assign")[:8]; data ipfsJob[32], timeout i64, like `list`.
ASSIGN_DISCRIMINATOR = bytes([73, 66, 125, 203, 81, 41, 64, 135])
JOB_STATES = {0: "QUEUED", 1: "RUNNING", 2: "COMPLETED", 3: "STOPPED"}
_ZERO32 = b"\x00" * 32


@dataclass
class JobAccount:
    address: str
    ipfs_job: str | None
    ipfs_result: str | None
    market: str
    node: str
    payer: str
    price: int
    project: str
    state: int
    state_name: str
    time_end: int
    time_start: int
    timeout: int

    def to_dict(self) -> dict:
        return asdict(self)


def _cid_or_none(b: bytes) -> str | None:
    return None if b == _ZERO32 else digest_to_cid(b)


def decode_job_account(address: str, data: bytes) -> JobAccount:
    if len(data) < JOB_ACCOUNT_SIZE:
        raise ValueError(f"job account data is {len(data)} bytes, expected {JOB_ACCOUNT_SIZE}")
    if data[:8] != JOB_ACCOUNT_DISCRIMINATOR:
        raise ValueError("not a Nosana job account (discriminator mismatch)")
    o = 8
    ipfs_job = data[o:o + 32]; o += 32
    ipfs_result = data[o:o + 32]; o += 32
    market = data[o:o + 32]; o += 32
    node = data[o:o + 32]; o += 32
    payer = data[o:o + 32]; o += 32
    (price,) = struct.unpack_from("<Q", data, o); o += 8
    project = data[o:o + 32]; o += 32
    state = data[o]; o += 1
    time_end, time_start, timeout = struct.unpack_from("<qqq", data, o)
    return JobAccount(
        address=address,
        ipfs_job=_cid_or_none(ipfs_job),
        ipfs_result=_cid_or_none(ipfs_result),
        market=b58encode(market),
        node=b58encode(node),
        payer=b58encode(payer),
        price=price,
        project=b58encode(project),
        state=state,
        state_name=JOB_STATES.get(state, f"UNKNOWN({state})"),
        time_end=time_end,
        time_start=time_start,
        timeout=timeout,
    )


def decode_list_ix(data: bytes) -> dict | None:
    if data[:8] != LIST_DISCRIMINATOR or len(data) < 48:
        return None
    (timeout,) = struct.unpack_from("<q", data, 40)
    return {"ipfs_job": digest_to_cid(data[8:40]), "timeout": timeout}


def decode_assign_ix(data: bytes) -> dict | None:
    if data[:8] != ASSIGN_DISCRIMINATOR or len(data) < 48:
        return None
    (timeout,) = struct.unpack_from("<q", data, 40)
    return {"ipfs_job": digest_to_cid(data[8:40]), "timeout": timeout}


def decode_finish_ix(data: bytes) -> dict | None:
    if data[:8] != FINISH_DISCRIMINATOR or len(data) < 40:
        return None
    return {"ipfs_result": digest_to_cid(data[8:40])}


class RpcError(RuntimeError):
    pass


def rpc_call(rpc_url: str, method: str, params: list, timeout: float = 30.0, tries: int = 5) -> dict:
    """JSON-RPC call; retries connection errors and HTTP 429/5xx (public RPCs rate-limit)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    # an explicit User-Agent: some RPCs (PublicNode) refuse urllib's default one with 403
    req = urllib.request.Request(rpc_url, data=body, headers={"Content-Type": "application/json",
                                                              "User-Agent": "relay-verify/0.2"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500 or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if attempt == tries - 1:
                raise
        time.sleep(2 * (attempt + 1))
    if "error" in resp:
        raise RpcError(f"{method}: {resp['error']}")
    return resp


def get_account_raw(rpc_url: str, address: str) -> dict:
    """Raw getAccountInfo response (kept whole so it can be stored as a test fixture)."""
    return rpc_call(rpc_url, "getAccountInfo", [address, {"encoding": "base64", "commitment": "confirmed"}])


def job_from_rpc_response(address: str, resp: dict) -> JobAccount | None:
    value = resp.get("result", {}).get("value")
    if value is None:
        return None
    if value.get("owner") != NOSANA_JOBS_PROGRAM:
        raise ValueError(f"account {address} is owned by {value.get('owner')}, not the Nosana jobs program")
    return decode_job_account(address, base64.b64decode(value["data"][0]))


def fetch_job(rpc_url: str, address: str) -> JobAccount | None:
    return job_from_rpc_response(address, get_account_raw(rpc_url, address))


# ---------------------------------------------------------------- transactions (receipt v0.2)
# Nosana closes a finished job account about an hour after `finish` (a `CleanAdmin` transaction), so a
# receipt anchors on the job's two transactions, which can't be closed:
#   post   `list` or `assign`: data ipfsJob[32] + timeout; accounts [0] job (a signer: the job keypair),
#          [1] market; `assign` also [3] node. Block time = the account's timeStart (listing time).
#   finish data ipfsResult[32]; accounts [0] job, [2] market, [8] node, the transaction's signer.
#          Block time = the account's timeEnd.
# Layouts read from real transactions of every job in runs/ (Phase 0 `list`, Phase 1/1b `assign`, and their
# `finish`es); tests/fixtures/txs/ keeps them. getTransaction for an old slot needs an archival RPC.

POST_MARKET, ASSIGN_NODE = 1, 3
FINISH_MARKET, FINISH_NODE = 2, 8


def tx_keys(tx: dict) -> tuple[list[str], set[str]]:
    """All account keys of a getTransaction (encoding json) result, and the signers."""
    msg = tx["transaction"]["message"]
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys = list(msg["accountKeys"]) + list(loaded.get("writable", [])) + list(loaded.get("readonly", []))
    return keys, set(msg["accountKeys"][:msg["header"]["numRequiredSignatures"]])


def nosana_instructions(tx: dict) -> list[tuple[bytes, list[str]]]:
    """(data, accounts) of every Nosana jobs-program instruction in a transaction, top level and inner."""
    from .b58 import b58decode

    keys, _ = tx_keys(tx)
    ixs = list(tx["transaction"]["message"]["instructions"])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        ixs += inner.get("instructions") or []
    return [(b58decode(ix["data"]), [keys[i] for i in ix["accounts"]]) for ix in ixs
            if keys[ix["programIdIndex"]] == NOSANA_JOBS_PROGRAM]


class TxMismatch(ValueError):
    """The transaction isn't the job's post or finish, or it failed."""


def _tx_ok(tx: dict | None, signature: str, what: str) -> dict:
    if tx is None:
        raise TxMismatch(f"{what} transaction {signature} not found")
    if (tx["transaction"].get("signatures") or [None])[0] != signature:
        raise TxMismatch(f"the RPC returned a different transaction for {what} {signature}")
    if (tx.get("meta") or {}).get("err") is not None:
        raise TxMismatch(f"{what} transaction {signature} failed: {tx['meta']['err']}")
    return tx


def decode_post_tx(tx: dict | None, signature: str, job: str) -> dict:
    """The job's `list` or `assign`: {ix, ipfs_job, timeout, market, node (assign only), listed_at}."""
    tx = _tx_ok(tx, signature, "post")
    _, signers = tx_keys(tx)
    for data, accts in nosana_instructions(tx):
        if not accts or accts[0] != job:
            continue
        for name, dec in (("list", decode_list_ix), ("assign", decode_assign_ix)):
            d = dec(data)
            if d:
                if job not in signers:
                    raise TxMismatch(f"post transaction {signature} is not signed by the job keypair {job}")
                return {"ix": name, **d, "market": accts[POST_MARKET],
                        "node": accts[ASSIGN_NODE] if name == "assign" else None, "listed_at": tx["blockTime"]}
    raise TxMismatch(f"post transaction {signature} has no Nosana list or assign instruction for job {job}")


def decode_finish_tx(tx: dict | None, signature: str, job: str) -> dict:
    """The job's `finish`: {ipfs_result, market, node (the signer), finished_at}."""
    tx = _tx_ok(tx, signature, "finish")
    _, signers = tx_keys(tx)
    for data, accts in nosana_instructions(tx):
        if not accts or accts[0] != job:
            continue
        d = decode_finish_ix(data)
        if d:
            node = accts[FINISH_NODE]
            if node not in signers:
                raise TxMismatch(f"finish transaction {signature}: node {node} did not sign it")
            return {**d, "market": accts[FINISH_MARKET], "node": node, "finished_at": tx["blockTime"]}
    raise TxMismatch(f"finish transaction {signature} has no Nosana finish instruction for job {job}")


def job_from_txs(job: str, post_tx: str, post: dict | None, finish_tx: str, finish: dict | None) -> dict:
    """What the chain says about a job, from its two transactions (getTransaction results).
    Raises TxMismatch if they aren't this job's successful post and finish, or disagree with each other."""
    p, f = decode_post_tx(post, post_tx, job), decode_finish_tx(finish, finish_tx, job)
    if p["market"] != f["market"]:
        raise TxMismatch(f"post names market {p['market']}, finish names {f['market']}")
    if p["node"] is not None and p["node"] != f["node"]:
        raise TxMismatch(f"assign names node {p['node']}, but {f['node']} signed the finish")
    return {"job": job, "post_tx": post_tx, "finish_tx": finish_tx, "post_ix": p["ix"], "ipfs_job": p["ipfs_job"],
            "ipfs_result": f["ipfs_result"], "market": p["market"], "node": f["node"],
            "listed_at": p["listed_at"], "finished_at": f["finished_at"]}


def get_transaction(rpc_url: str, signature: str) -> dict | None:
    """getTransaction (json encoding, v0 transactions allowed); None if the RPC has no record of it."""
    return rpc_call(rpc_url, "getTransaction",
                    [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0,
                                 "commitment": "confirmed"}]).get("result")


def find_job_txs(rpc_url: str, job: str, limit: int = 100) -> dict[str, tuple[str, dict]]:
    """At issue time: the job's post and finish, found from its signature history.
    {"post": (signature, tx), "finish": (signature, tx)}; a key is missing if not found."""
    sigs = rpc_call(rpc_url, "getSignaturesForAddress", [job, {"limit": limit, "commitment": "confirmed"}])
    found: dict[str, tuple[str, dict]] = {}
    for s in reversed(sigs.get("result") or []):
        if s.get("err") is not None:
            continue
        tx = get_transaction(rpc_url, s["signature"])
        if tx is None:
            continue
        for what, dec in (("post", decode_post_tx), ("finish", decode_finish_tx)):
            if what in found:
                continue
            try:
                dec(tx, s["signature"], job)
            except TxMismatch:
                continue
            found[what] = (s["signature"], tx)
    return found
