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
