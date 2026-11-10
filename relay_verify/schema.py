"""Receipt schema v0.1 check (both grades). Standard library only; mirrors docs/receipt-schema.json."""

from __future__ import annotations

import re

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CID = re.compile(r"^Qm[1-9A-HJ-NP-Za-km-z]{44}$")
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

COMMON = {"receipt_version": str, "grade": str, "receipt_id": str, "item_id": str, "source": dict,
          "rule": dict, "input_sha256": str, "output": dict, "output_sha256": str,
          "relay_checks": list, "cost_usd": (int, float)}
ANCHORED = {"merkle": dict, "nosana": dict, "model": dict, "spot_check": dict}
DECLARED = {"provider": dict}
NOSANA = {"job": _B58, "market": _B58, "node": _B58, "job_definition_cid": _CID, "result_cid": _CID,
          "inputs_cid": _CID, "listed_at": int, "finished_at": int, "container_start": int, "container_end": int}


def validate(r: dict) -> list[str]:
    """Problems found; empty means the receipt has the v0.1 shape."""
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
    if r["receipt_version"] != "0.1":
        errs.append("receipt_version is not 0.1")
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
