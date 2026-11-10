"""The payout gate (docs/POC_PAYOUTS.md step 4) and the human review queue it feeds.

The gate pays only on a receipt that verifies, has the grade the rule requires, and leaves no doubt. Items
it can't settle go to the human review queue with the reasons, never to a silent fallback:

- refuse        the receipt doesn't verify, or its grade is below what the rule requires
- human_review  a re-run on a second node disagreed (the receipt's spot_check says so and why), or the open
                model hasn't cleared the rule's bar (on_no_open_model: human_review)
- hold          the decision is hold
- pay           otherwise

The devnet payout executor (Phase 2) calls `decide` with relay-verify's verdict for each receipt.
"""

from __future__ import annotations

import json
from pathlib import Path

GRADE_RANK = {"none": 0, "declared": 1, "anchored": 2}
VERIFIED = 0  # relay_verify.checks.verdict


def decide(receipt: dict, verify_verdict: int | None, rule: dict) -> dict:
    """{"action": refuse | human_review | hold | pay, "reasons": [...]}. verify_verdict: relay-verify's exit
    code for this receipt (0 verified, 1 failed, 2 couldn't check), or None when not verified yet: the queue
    is built at issue time, and nothing is paid until the receipt verifies."""
    if receipt["rule"]["id"] != rule["id"]:
        return {"action": "refuse", "reasons": [f"receipt is under rule {receipt['rule']['id']}, not {rule['id']}"]}
    if GRADE_RANK[receipt["grade"]] < GRADE_RANK[rule["requires"]]:
        return {"action": "refuse",
                "reasons": [f"rule {rule['id']} requires an {rule['requires']} receipt; this one is {receipt['grade']}"]}
    if verify_verdict is not None and verify_verdict != VERIFIED:
        return {"action": "refuse", "reasons": ["receipt failed relay-verify" if verify_verdict == 1
                                                else "relay-verify couldn't check the receipt"]}
    review = []
    sc = receipt.get("spot_check") or {}
    if sc.get("action") == "human_review":
        review.append(f"re-run disagreed: {sc.get('reason')}")
    if rule["requires"] == "anchored" and "work_type_cleared_bar" not in receipt.get("relay_checks", []):
        review.append(f"the open model has not cleared rule {rule['id']}'s bar "
                      f"(on_no_open_model: {rule.get('on_no_open_model', 'human_review')})")
    if review:
        return {"action": "human_review", "reasons": review}
    out = receipt["output"]
    if out.get("decision") != "approve":
        return {"action": "hold", "reasons": [f"decision {out.get('decision')}/{out.get('reason_code')}"]}
    if verify_verdict is None:
        return {"action": "pay_after_verify", "reasons": ["approve; pays only once relay-verify passes"]}
    return {"action": "pay", "reasons": []}


def review_queue(receipts: dict[str, dict], rules: dict[str, dict]) -> list[dict]:
    """Every receipt the gate sends to human review, with its reasons (before verification)."""
    q = []
    for item_id, r in sorted(receipts.items()):
        d = decide(r, None, rules[r["rule"]["id"]])
        if d["action"] == "human_review":
            q.append({"item_id": item_id, "receipt_id": r["receipt_id"], "output": r["output"],
                      "spot_check_disagreed": (r.get("spot_check") or {}).get("agreed") is False,
                      "reasons": d["reasons"]})
    return q


def write_queue(path: Path, receipts: dict[str, dict], rules: dict[str, dict]) -> dict:
    q = review_queue(receipts, rules)
    path.write_text(json.dumps(q, indent=2, sort_keys=True) + "\n")
    return {"items": len(q), "re_run_disagreed": sum(x["spot_check_disagreed"] for x in q)}
