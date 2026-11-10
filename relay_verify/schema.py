"""Receipt schema v0.2 check (both grades). Standard library only; mirrors docs/receipt-schema.json.

v0.2 (2026-11-10): anchored receipts carry the job's post and finish transaction signatures, because Nosana
closes job accounts after the job; and a spot check records what it made Relay do (`action`), and why."""

from __future__ import annotations

import re

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CID = re.compile(r"^Qm[1-9A-HJ-NP-Za-km-z]{44}$")
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_SIG = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,90}$")  # a base58 Solana transaction signature (64 bytes)
VERSION = "0.2"
SPOT_ACTIONS = ("none", "human_review")

COMMON = {"receipt_version": str, "grade": str, "receipt_id": str, "item_id": str, "source": dict,
          "rule": dict, "input_sha256": str, "output": dict, "output_sha256": str,
          "relay_checks": list, "cost_usd": (int, float)}
ANCHORED = {"merkle": dict, "nosana": dict, "model": dict, "spot_check": dict}
DECLARED = {"provider": dict}
NOSANA = {"job": _B58, "market": _B58, "node": _B58, "job_definition_cid": _CID, "result_cid": _CID,
          "inputs_cid": _CID, "post_tx": _SIG, "finish_tx": _SIG,
          "listed_at": int, "finished_at": int, "container_start": int, "container_end": int}


def validate(r: dict) -> list[str]:
    """Problems found; empty means the receipt has the v0.2 shape."""
    errs: list[str] = []

    def need(obj, spec, where):
        for k, t in spec.items():
            if k not in obj:
                errs.append(f"{where}{k} missing")
            elif isinstance(t, re.Pattern):
                if not isinstance(obj[k], str) or not t.match(obj[k]):
                    errs.append(f"{where}{k} malformed")
            elif not isinstance(obj[k], t) or isinstance(obj[k], bool):
                errs.append(f"{where}{k} has the wrong type")

    need(r, COMMON, "")
    if errs:
        return errs
    if r["receipt_version"] != VERSION:
        errs.append(f"receipt_version is {r['receipt_version']!r}, not {VERSION!r}"
                    + (" (v0.1 anchored on the job account, which Nosana closes: re-issue as v0.2)"
                       if r["receipt_version"] == "0.1" else ""))
    for k in ("receipt_id", "input_sha256", "output_sha256"):
        if not _HEX64.match(r[k]):
            errs.append(f"{k} is not 64 hex chars")
    need(r["source"], {"vendor": str, "work_type": str}, "source.")
    need(r["rule"], {"id": str, "requires": str}, "rule.")
    if r["grade"] == "anchored":
        need(r, ANCHORED, "")
        if errs:
            return errs
        need(r["nosana"], NOSANA, "nosana.")
        need(r["merkle"], {"root": str, "path": list}, "merkle.")
        need(r["model"], {"image": str, "weights": str}, "model.")
        if not errs:
            if not _HEX64.match(r["merkle"]["root"]):
                errs.append("merkle.root is not 64 hex chars")
            for step in r["merkle"]["path"]:
                if not isinstance(step, dict) or step.get("side") not in ("L", "R") \
                        or not _HEX64.match(str(step.get("hash", ""))):
                    errs.append("merkle.path entry malformed")
                    break
            if "@sha256:" not in r["model"]["image"]:
                errs.append("model.image is not pinned by digest")
            if "@" not in r["model"]["weights"]:
                errs.append("model.weights has no @revision")
            errs.extend(_spot_check(r["spot_check"]))
    elif r["grade"] == "declared":
        need(r, DECLARED, "")
        if not errs:
            need(r["provider"], {"name": str, "model": str, "timestamp": str}, "provider.")
            ids = [k for k in ("request_id", "session_id") if isinstance(r["provider"].get(k), str)]
            if not ids:
                errs.append("provider has neither request_id nor session_id")
        for k in ("nosana", "merkle"):
            if k in r:
                errs.append(f"declared receipt carries {k}")
    else:
        errs.append(f"unknown grade {r['grade']!r}")
    return errs


def _spot_check(sc: dict) -> list[str]:
    """{rerun_job, rerun_node, agreed, action, reason?, rerun_output_sha256?}. A disagreement must send the item
    to human review and say why (Sogol, 2026-11-10)."""
    errs = [f"spot_check.{k} missing" for k in ("rerun_job", "rerun_node", "agreed", "action") if k not in sc]
    if errs:
        return errs
    if sc["agreed"] not in (True, False, None):
        errs.append("spot_check.agreed is not true, false or null")
    if sc["action"] not in SPOT_ACTIONS:
        errs.append(f"spot_check.action is not one of {SPOT_ACTIONS}")
    if sc["agreed"] is False:
        if sc["action"] != "human_review":
            errs.append("spot_check disagreed but action is not human_review")
        if not isinstance(sc.get("reason"), str) or not sc["reason"]:
            errs.append("spot_check disagreed but gives no reason")
        if not _HEX64.match(str(sc.get("rerun_output_sha256", ""))):
            errs.append("spot_check disagreed but has no rerun_output_sha256")
    return errs
