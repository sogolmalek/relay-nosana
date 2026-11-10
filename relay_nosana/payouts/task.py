"""payout_check: compile each payout into one inputs.jsonl line, and score outputs against truth."""

from __future__ import annotations

import math

from .data import DECISIONS, REASONS, SKUS, usd

TASK = "payout_check"

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "hold"]},
        "reason_code": {"type": "string", "enum": REASONS},
    },
    "required": ["decision", "reason_code"],
    "additionalProperties": False,
}

SYSTEM = """You check marketplace seller payouts before they are sent. For one payout, decide "approve" or "hold" and give one reason code.

Apply these rules in this order and use the first one that applies:
1. seller_on_hold: a decision in force puts the payout's seller on hold.
2. duplicate: the payout's invoice_id is already in the seller's paid invoices.
3. amount_mismatch: amount_usd is not equal to the sum of the lines' line_total_usd.
4. stale_price: a sale line dated on or after a price change's effective date uses the old unit price.
5. needs_approval: a refund line for an order placed more than 30 days before the payout date has no approval_id.
If none applies, approve with reason_code "ok". A hold always names the rule that applies; an approve is always "ok".

Reply with JSON only: {"decision": "approve" | "hold", "reason_code": "ok" | "seller_on_hold" | "duplicate" | "amount_mismatch" | "stale_price" | "needs_approval"}"""


def _line_view(x: dict) -> dict:
    if x["type"] == "sale":
        return {"type": "sale", "sku": x["sku"], "product": SKUS[x["sku"]]["name"], "qty": x["qty"],
                "sale_date": x["sale_date"], "unit_price_usd": usd(x["unit_price_cents"]),
                "line_total_usd": usd(x["qty"] * x["unit_price_cents"])}
    v = {"type": "refund", "order_id": x["order_id"], "order_date": x["order_date"],
         "line_total_usd": usd(-x["refund_cents"])}
    if x.get("approval_id"):
        v["approval_id"] = x["approval_id"]
    return v


def decisions_for(payout: dict) -> list[dict]:
    """The compiler's selection: price changes for SKUs in the payout, every seller hold, and the
    refund rule if the payout has a refund. Selected by relevance, never by the answer."""
    skus = {x.get("sku") for x in payout["lines"]}
    has_refund = any(x["type"] == "refund" for x in payout["lines"])
    out = []
    for d in DECISIONS:
        if (d["kind"] == "price_change" and d["sku"] in skus) or d["kind"] == "seller_hold" \
                or (d["kind"] == "refund_approval" and has_refund):
            out.append({"id": d["id"], "date": d["date"], "text": d["text"]})
    return out


def user_message(payout: dict, paid_invoices: dict[str, list[str]]) -> str:
    view = {"payout_id": payout["payout_id"], "payout_date": payout["payout_date"], "seller_id": payout["seller_id"],
            "invoice_id": payout["invoice_id"], "currency": payout["currency"],
            "amount_usd": usd(payout["amount_cents"]), "lines": [_line_view(x) for x in payout["lines"]]}
    import json
    return ("Decisions in force:\n" + json.dumps(decisions_for(payout), indent=1)
            + f"\n\nPaid invoices for seller {payout['seller_id']}: "
            + json.dumps(paid_invoices.get(payout["seller_id"], []))
            + "\n\nPayout:\n" + json.dumps(view, indent=1))


def input_line(payout: dict, paid_invoices: dict[str, list[str]], max_tokens: int = 48, seed: int = 0) -> dict:
    return {"item_id": payout["payout_id"], "task": TASK,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": user_message(payout, paid_invoices)}],
            "params": {"temperature": 0, "max_tokens": max_tokens, "seed": seed}}


def correct(output: dict | None, truth: dict) -> bool:
    return bool(output) and output.get("decision") == truth["decision"] \
        and output.get("reason_code") == truth["reason_code"]


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))
