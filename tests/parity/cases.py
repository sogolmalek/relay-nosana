"""Parity cases for the browser verifier (docs/app/verifier.js) vs relay_verify: every published receipt of
Phase 1 and run b, plus each tamper the app offers. Shared by the recorder, the pytest and the Node runner."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

from relay_verify.checks import receipt_id_of
from relay_verify.jcs import sha256_hex

ROOT = Path(__file__).resolve().parents[2]
RUNS = ["payouts-2026-11-10", "payouts-2026-11-10b"]
FIXTURES = ROOT / "tests" / "fixtures" / "parity"


def published() -> list[tuple[str, dict]]:
    out = []
    for run in RUNS:
        for p in sorted((ROOT / "runs" / run / "receipts").glob("*.json")):
            out.append((f"{run}/{p.stem}", json.loads(p.read_text())))
    return out


def _rid(r: dict) -> dict:
    r["receipt_id"] = receipt_id_of(r)
    return r


def tamper(r: dict, kind: str, other: dict | None = None) -> dict:
    """The app's tamper buttons (docs/app/app.js TAMPERS). The tamperer recomputes receipt_id, except for
    `output_only`, so the chain and IPFS have to catch the change."""
    r = copy.deepcopy(r)
    if kind == "decision":
        flip = "approve" if r["output"]["decision"] == "hold" else "hold"
        r["output"] = {"decision": flip, "reason_code": "ok" if flip == "approve" else r["output"]["reason_code"]}
        r["output_sha256"] = sha256_hex(r["output"])
    elif kind == "output_only":
        r["output"] = {"decision": "approve", "reason_code": "ok"}
        return r
    elif kind == "model":
        r["model"]["weights"] = r["model"]["weights"].split("@")[0] + "@" + "0" * 40
    elif kind == "job":
        r["nosana"]["job"] = other["nosana"]["job"]
    elif kind == "post_tx":
        r["nosana"]["post_tx"] = other["nosana"]["post_tx"]
    elif kind == "merkle":
        r["merkle"]["path"][0]["hash"] = "ab" * 32
    elif kind == "node":
        r["nosana"]["node"] = other["spot_check"]["rerun_node"] or other["nosana"]["node"]
    elif kind == "pending_rerun":  # not a forgery: the shape of a receipt whose required re-run hasn't run
        r["spot_check"] = {"rerun_job": None, "rerun_node": None, "agreed": None, "action": "pending_rerun",
                           "reason": "re-run required by the rule and not run yet"}
    elif kind == "unknown_finish":
        r["nosana"]["finish_tx"] = "1" * 88
    else:
        raise ValueError(kind)
    return _rid(r)


TAMPERS = ["decision", "output_only", "model", "job", "post_tx", "merkle", "node", "unknown_finish", "pending_rerun"]


def all_cases() -> list[tuple[str, dict]]:
    pub = published()
    by = dict(pub)
    a, b = by["payouts-2026-11-10/P-0101"], by["payouts-2026-11-10b/P-0128"]
    cases = list(pub)
    for base_name, base, other in (("p1-P-0101", a, b), ("b-P-0128", b, a)):
        for k in TAMPERS:
            cases.append((f"tamper/{base_name}/{k}", tamper(base, k, other)))
    old = subprocess.run(["git", "show", "f7340b9~1:runs/payouts-2026-11-10/receipts/P-0101.json"], cwd=ROOT,
                         capture_output=True, text=True).stdout
    if old:
        cases.append(("v0.1/P-0101", json.loads(old)))
    return cases
