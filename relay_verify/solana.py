"""Read and decode a Nosana job account from Solana JSON-RPC. Standard library only.

Layout from @nosana/jobs-program 2.24.3 (Codama-generated), see docs/phase0-research.md.
"""

from __future__ import annotations

import base64
import json
import struct
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


def rpc_call(rpc_url: str, method: str, params: list, timeout: float = 30.0) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(rpc_url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read())
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
