"""Known-good nodes per market, from measured outcomes only (Phase 1b).

Evidence: every job row in runs/*/ledger/jobs.json (Phase 1 on) and the Phase 0 summaries in
tests/fixtures/phase0/*_summary.json. Per node and market:

- good: at least one successful job, and no hang after its last success;
- bad:  a hang (running until we stopped it), or two or more failures and no success;
- anything else is unknown (open market).

Good nodes are ordered by their best measured download + load time (nodes with none go last).
Selection: an idle good node not already tried in this run, else the open market (no node).
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _phase0(root: Path) -> list[dict]:
    out = []
    for p in sorted((root / "tests" / "fixtures" / "phase0").glob("*_summary.json")):
        s = json.loads(p.read_text())
        acct = s.get("account") or {}
        node = acct.get("node") if isinstance(acct, dict) else None
        if not node or not s.get("market"):
            continue
        rc = s.get("result_check") or {}
        load = None
        for op in s.get("result_op_results") or []:
            v = (op or {}).get("load_s")
            if isinstance(v, list) and len(v) >= 2:
                load = float(v[1])
        out.append({"source": f"phase0/{p.name}", "market": s["market"], "node": node,
                    "ok": bool(rc.get("ok")), "hang": False, "load_s": load, "ts": acct.get("time_start") or 0,
                    "error": "; ".join(rc.get("reasons") or [])})
    return out


def _ledgers(root: Path) -> list[dict]:
    out = []
    for p in sorted((root / "runs").glob("*/ledger/jobs.json")):
        for r in json.loads(p.read_text()):
            if not r.get("node") or r["node"].startswith("1111111111"):
                continue
            err = r.get("error") or ""
            out.append({"source": f"{p.parent.parent.name}/{r['tag']}_a{r['attempt']}", "market": r["market"],
                        "node": r["node"], "ok": bool(r.get("ok")), "hang": "TimeoutError" in err,
                        "load_s": r.get("load_s"), "ts": r.get("ts") or 0, "error": err})
    return out


def outcomes(root: Path = ROOT) -> list[dict]:
    return sorted(_phase0(root) + _ledgers(root), key=lambda o: o["ts"])


def known_nodes(market: str, root: Path = ROOT) -> dict:
    """{"good": [node, ...] best first, "bad": [...], "table": {node: summary}} for one market."""
    table: dict[str, dict] = {}
    for o in outcomes(root):
        if o["market"] != market:
            continue
        t = table.setdefault(o["node"], {"ok": 0, "failed": 0, "hung": 0, "best_load_s": None,
                                         "hung_after_success": False, "sources": []})
        t["sources"].append(o["source"])
        if o["ok"]:
            t["ok"] += 1
            t["hung_after_success"] = False
            if o["load_s"] is not None:
                t["best_load_s"] = o["load_s"] if t["best_load_s"] is None else min(t["best_load_s"], o["load_s"])
        elif o["hang"]:
            t["hung"] += 1
            if t["ok"]:
                t["hung_after_success"] = True
        else:
            t["failed"] += 1
    good = [n for n, t in table.items() if t["ok"] and not t["hung_after_success"]]
    bad = [n for n, t in table.items() if (t["hung"] and not t["ok"]) or t["hung_after_success"]
           or (t["failed"] >= 2 and not t["ok"])]
    good.sort(key=lambda n: (table[n]["best_load_s"] is None, table[n]["best_load_s"] or 0))
    return {"good": good, "bad": sorted(bad), "table": table}


def pick(market: str, idle: list[str], avoid: set[str], root: Path = ROOT) -> tuple[str | None, str]:
    """(node or None for the open market, why)."""
    k = known_nodes(market, root)
    for n in k["good"]:
        if n in idle and n not in avoid:
            return n, f"known-good node, idle (best load {k['table'][n]['best_load_s']} s)"
    return None, "no known-good node idle: open market"


# ---------------------------------------------------------------- the market queue, read from Solana (Phase 1c)
# A Nosana market account holds one queue: either nodes waiting for work or jobs waiting for a node. The indexer
# heuristic above (recent completions, nothing running) proved wrong on 2026-11-11: two markets it called idle
# had jobs queued and no node. The queue itself is the truth. Layout (jobs program): discriminator 8, authority
# 32, jobExpiration 8, jobPrice 8, jobTimeout 8, jobType 1, nodeAccessKey 32, nodeXnosMinimum 16, queueType 1,
# vault 32, vaultBump 1, queue Vec<Pubkey>. Entries are classified by owner: a job account is owned by the jobs
# program, a node is a wallet (system program).

_QUEUE_OFFSET = 8 + 32 + 8 + 8 + 8 + 1 + 32 + 16 + 1 + 32 + 1
SYSTEM_PROGRAM = "11111111111111111111111111111111"

# 48 GB+ premium markets with vLLM v0.10.2 cached (Phase 0 discover), USD per hour. Credit jobs: premium only.
PREMIUM_48GB_PLUS = {
    "6eMivCx49anWFYwNgg8KNJQfSJYB5nBdif8CK6z52dem": ("NVIDIA 6000 Ada", 0.6364),
    "EjryZ6XEthz3z7nnLfjXBYafyn7VyHgChfbfM47LfAao": ("NVIDIA A6000", 0.3636),
    "BLqSzPzcXMX5gseNXE4Ma45f31Eo6tNFVYoRmPG7kxP2": ("NVIDIA A40", 0.3636),
    "F3aGGSMb73XHbJbDXVbcXo7iYM9fyevvAZGQfwgrnWtB": ("NVIDIA A100 40GB", 0.5545),
    "GLJHzqRN9fKGBsvsFzmGnaQGknUtLN1dqaFR8n3YdM22": ("NVIDIA A100 80GB", 0.8181),
}


def market_queue(rpc: str, market: str) -> dict:
    """{"kind": "nodes" | "jobs" | "empty", "entries": [address, ...]} from the market account on Solana."""
    import base64
    import struct

    from relay_verify.b58 import b58encode
    from relay_verify.solana import NOSANA_JOBS_PROGRAM, rpc_call

    v = rpc_call(rpc, "getAccountInfo", [market, {"encoding": "base64"}])["result"]["value"]
    d = base64.b64decode(v["data"][0])
    (n,) = struct.unpack_from("<I", d, _QUEUE_OFFSET)
    keys = [b58encode(d[_QUEUE_OFFSET + 4 + 32 * i:_QUEUE_OFFSET + 36 + 32 * i]) for i in range(n)]
    if not keys:
        return {"kind": "empty", "entries": []}
    owners = [(x or {}).get("owner") for x in rpc_call(rpc, "getMultipleAccounts", [keys[:100], {
        "encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}])["result"]["value"]]
    if all(o == NOSANA_JOBS_PROGRAM for o in owners):
        return {"kind": "jobs", "entries": keys}
    if all(o in (SYSTEM_PROGRAM, None) for o in owners):
        return {"kind": "nodes", "entries": keys}
    return {"kind": "mixed", "entries": keys, "owners": owners}


def waiting_nodes(rpc: str, markets: dict = PREMIUM_48GB_PLUS, avoid: set[str] = frozenset(),
                  root: Path = ROOT) -> list[tuple[str, str]]:
    """(market, node) pairs with a node waiting in the queue, cheapest market first, known-bad nodes left out."""
    out = []
    for m in sorted(markets, key=lambda m: markets[m][1]):
        q = market_queue(rpc, m)
        if q["kind"] != "nodes":
            continue
        bad = set(known_nodes(m, root)["bad"])
        out += [(m, n) for n in q["entries"] if n not in avoid and n not in bad]
    return out
