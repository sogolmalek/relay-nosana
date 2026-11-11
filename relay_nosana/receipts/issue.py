"""Issue receipt v0.2: anchored (from a successful Nosana result) and declared (from a frontier call).

Anchored receipts are built only from a result that passed relay_verify.result.check_result; the
outputs, Merkle path and root come from the node-pinned result itself, not from local state. The job's
node, market, CIDs and times come from its post and finish transactions (relay_verify.solana.job_from_txs),
the same decoding the verifier does, because Nosana closes the job account after the job.
"""

from __future__ import annotations

from relay_verify.checks import receipt_id_of
from relay_verify.jcs import sha256_hex
from relay_verify.merkle import path as merkle_path
from relay_verify.outputs import container_times, parse_outputs, regex_result
from relay_verify.result import check_result
from relay_verify.schema import VERSION, validate

NOT_RERUN = {"rerun_job": None, "rerun_node": None, "agreed": None, "action": "none"}


def _finish(r: dict) -> dict:
    r["receipt_id"] = receipt_id_of(r)
    errs = validate(r)
    if errs:
        raise ValueError(f"receipt for {r['item_id']} fails the schema: {errs}")
    return r


def spot_check(*, rerun_job: str, rerun_node: str, output_sha256: str, rerun_output_sha256: str,
               on_disagree: str = "human_review") -> dict:
    """A re-run's verdict on one item. Agreement compares parsed outputs (output_sha256). A disagreement sends
    the item to human review, and the receipt says why (Sogol, 2026-11-10)."""
    if rerun_output_sha256 == output_sha256:
        return {"rerun_job": rerun_job, "rerun_node": rerun_node, "agreed": True, "action": "none"}
    return {"rerun_job": rerun_job, "rerun_node": rerun_node, "agreed": False, "action": on_disagree,
            "rerun_output_sha256": rerun_output_sha256,
            "reason": f"the re-run on node {rerun_node} (job {rerun_job}) returned a different output for the same "
                      f"input; the gate holds the payout for human review"}


def pending_rerun(why: str) -> dict:
    """The rule requires a re-run of this item and none has run yet: the gate holds the payout until it does."""
    return {"rerun_job": None, "rerun_node": None, "agreed": None, "action": "pending_rerun",
            "reason": f"re-run required by the rule and not run yet ({why}); the gate holds the payout until it runs"}


def anchored_from_result(*, result: dict, chain: dict, job_definition_cid: str, inputs_cid: str,
                         image: str, weights: str, items: dict[str, dict], rule: dict, rule_sha256: str,
                         work_type: str, relay_checks: dict[str, list[str]], spot_checks: dict[str, dict],
                         cost_usd_per_item: float) -> dict[str, dict]:
    """One receipt per item in `items` ({item_id: input line}) from a finished job's result.
    `chain` is relay_verify.solana.job_from_txs(...) for the job: its post and finish, decoded."""
    if chain["ipfs_job"] != job_definition_cid:
        raise ValueError(f"the post transaction lists job definition {chain['ipfs_job']}, not {job_definition_cid}")
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
                "job": chain["job"], "market": chain["market"], "node": chain["node"],
                "job_definition_cid": job_definition_cid, "result_cid": chain["ipfs_result"],
                "inputs_cid": inputs_cid, "post_tx": chain["post_tx"], "finish_tx": chain["finish_tx"],
                "listed_at": chain["listed_at"], "finished_at": chain["finished_at"],
                "container_start": cs, "container_end": ce,
            },
            "model": {"image": image, "weights": weights},
            "relay_checks": relay_checks.get(item_id, []),
            "spot_check": spot_checks.get(item_id, dict(NOT_RERUN)),
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
