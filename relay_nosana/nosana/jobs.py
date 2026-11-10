"""Nosana Jobs API client (credits, API key). Behaviour as measured in Phase 0 (docs/phase0-report.md).

- Balance fields are USD (assignedCredits 10 = $10).
- A job is "running" while its run account (`run` in the POST response) exists; the job account
  itself stays QUEUED until it finishes.
- Stopping a queued job delists it (account closed); stopping a running one ends it with no result.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from relay_verify.solana import rpc_call

CLIENT_MANAGER = "https://client-manager.k8s.prd.nosana.com"
INDEXER = "https://blockchain-indexer.k8s.prd.nos.ci"
UA = "relay-nosana/0.2"


def _http(method: str, url: str, headers: dict | None = None, body: bytes | None = None, timeout: float = 60,
          tries: int = 5):
    """Retries connection errors (resets, timeouts). A POST to /jobs/list carries one Idempotency-Key
    for all tries, so a retry can't list a second job."""
    req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": UA, **(headers or {})})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                status = r.status
            break
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            if attempt == tries - 1:
                raise
            time.sleep(3 * (attempt + 1))
    try:
        return status, json.loads(raw)
    except Exception:
        return status, {"_raw_text": raw.decode(errors="replace")[:2000]}


class Jobs:
    def __init__(self, key: str | None = None, base: str = CLIENT_MANAGER):
        self.key = key or os.environ["NOSANA_API_KEY"]
        self.base = base.rstrip("/")

    def _h(self, extra=None):
        return {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", **(extra or {})}

    def balance(self) -> dict:
        s, b = _http("GET", f"{self.base}/credits/balance", self._h())
        if s != 200:
            raise RuntimeError(f"GET /credits/balance: HTTP {s} {b}")
        return b

    @staticmethod
    def available_usd(bal: dict) -> float:
        return round(float(bal.get("assignedCredits", 0)) - float(bal.get("reservedCredits", 0))
                     - float(bal.get("settledCredits", 0)), 6)

    def post(self, ipfs_hash: str, market: str, timeout_s: int = 3600, node: str | None = None) -> tuple[int, dict, dict]:
        body = {"ipfsHash": ipfs_hash, "market": market, "timeout": timeout_s}
        if node:
            body["node"] = node
        s, b = _http("POST", f"{self.base}/jobs/list", self._h({"Idempotency-Key": str(uuid.uuid4())}),
                     json.dumps(body).encode())
        return s, b, body

    def stop(self, job: str) -> tuple[int, dict]:
        return _http("POST", f"{self.base}/jobs/{job}/stop", self._h(), b"{}")


def run_account_exists(rpc: str, run_address: str | None) -> bool:
    if not run_address:
        return False
    resp = rpc_call(rpc, "getAccountInfo", [run_address, {"encoding": "base64"}])
    return bool((resp.get("result") or {}).get("value"))


def indexer_jobs(market: str, state: str, limit: int = 50) -> list[dict]:
    q = urllib.parse.urlencode({"market": market, "state": state, "limit": limit})
    s, b = _http("GET", f"{INDEXER}/jobs?{q}")
    if s != 200:
        return []
    return b if isinstance(b, list) else (b.get("jobs") or b.get("data") or [])


def idle_nodes(market: str, exclude: set[str]) -> list[str]:
    """Nodes that recently completed a job in this market and aren't running one now (indexer)."""
    busy = {j.get("node") for j in indexer_jobs(market, "RUNNING")}
    seen: list[str] = []
    for j in indexer_jobs(market, "COMPLETED"):
        n = j.get("node")
        if n and n not in exclude and n not in busy and n not in seen:
            seen.append(n)
    return seen
