"""Issue receipt v0.1: anchored (from a successful Nosana result) and declared (from a frontier call).

Anchored receipts are built only from a result that passed relay_verify.result.check_result; the
outputs, Merkle path and root come from the node-pinned result itself, not from local state.
"""

from __future__ import annotations

from relay_verify.checks import receipt_id_of
from relay_verify.jcs import sha256_hex
from relay_verify.merkle import path as merkle_path
from relay_verify.outputs import container_times, parse_outputs, regex_result
from relay_verify.result import check_result
from relay_verify.schema import validate

VERSION = "0.1"


def _finish(r: dict) -> dict:
    r["receipt_id"] = receipt_id_of(r)
    errs = validate(r)
    if errs:
        raise ValueError(f"receipt for {r['item_id']} fails the schema: {errs}")
    return r


def anchored_from_result(*, result: dict, account: dict, job_definition_cid: str, inputs_cid: str,
                         image: str, weights: str, items: dict[str, dict], rule: dict, rule_sha256: str,
                         work_type: str, relay_checks: dict[str, list[str]], spot_checks: dict[str, dict],
                         cost_usd_per_item: float) -> dict[str, dict]:
    """One receipt per item in `items` ({item_id: input line}) from a finished job's result.
    `account` is the decoded on-chain job account (relay_verify.solana.JobAccount.to_dict())."""
    rc = check_result(result)
    if not rc["ok"]:
        raise ValueError(f"no receipts from a failed result: {rc['reasons']}")
    rows, summary = parse_outputs(result)
    root = summary["merkle_root"]
    if regex_result(result, "merkle_root") != root:
        raise ValueError("the result's regex root and summary root differ")
    cs, ce = container_times(result)
    all_rows = list(rows.values())
    out = {}
    for item_id, line in items.items():
        row = rows[item_id]
        if row["input_sha256"] != sha256_hex(line):
            raise ValueError(f"{item_id}: the result's input hash differs from our input line")
        out[item_id] = _finish({
            "receipt_version": VERSION,
            "grade": "anchored",
            "item_id": item_id,
            "source": {"vendor": "relay-batch", "work_type": work_type},
            "rule": {"id": rule["id"], "requires": rule["requires"], "snapshot_sha256": rule_sha256},
            "input_sha256": row["input_sha256"],
            "output": row["output"],
            "output_sha256": row["output_sha256"],
            "merkle": {"root": root, "path": merkle_path(all_rows, item_id)},
            "nosana": {
                "job": account["address"], "market": account["market"], "node": account["node"],
                "job_definition_cid": job_definition_cid, "result_cid": account["ipfs_result"],
                "inputs_cid": inputs_cid, "listed_at": account["time_start"], "finished_at": account["time_end"],
                "container_start": cs, "container_end": ce,
            },
            "model": {"image": image, "weights": weights},
            "relay_checks": relay_checks.get(item_id, []),
            "spot_check": spot_checks.get(item_id, {"rerun_job": None, "rerun_node": None, "agreed": None}),
            "cost_usd": round(cost_usd_per_item, 6),
        })
    return out


def declared(*, item_id: str, input_line: dict, output: dict, provider: dict, rule: dict, rule_sha256: str,
             work_type: str, vendor: str, relay_checks: list[str], cost_usd: float) -> dict:
    return _finish({
        "receipt_version": VERSION,
        "grade": "declared",
        "item_id": item_id,
        "source": {"vendor": vendor, "work_type": work_type},
        "rule": {"id": rule["id"], "requires": rule["requires"], "snapshot_sha256": rule_sha256},
        "input_sha256": sha256_hex(input_line),
        "output": output,
        "output_sha256": sha256_hex(output),
        "provider": provider,
        "relay_checks": relay_checks,
        "cost_usd": round(cost_usd, 6),
    })
