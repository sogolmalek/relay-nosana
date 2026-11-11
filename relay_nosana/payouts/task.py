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


# ---------------------------------------------------------------- Phase 1b: reasoning first, two arms

# Key order matters: guided decoding writes the keys in this order, so the model reasons before it
# commits to a decision. The job definition carries this schema with its key order kept (not JCS).
OUTPUT_SCHEMA_1B = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "decision": {"type": "string", "enum": ["approve", "hold"]},
        "reason_code": {"type": "string", "enum": REASONS},
    },
    "required": ["reasoning", "decision", "reason_code"],
    "additionalProperties": False,
}
# What the receipts hash (output_sha256, the Merkle leaf): the parsed decision only, not the reasoning.
OUTPUT_KEYS_1B = ["decision", "reason_code"]
MAX_TOKENS_1B = 256  # the same budget for both arms

SYSTEM_1B = SYSTEM.replace(
    """Reply with JSON only: {"decision": "approve" | "hold", "reason_code": "ok" | "seller_on_hold" | "duplicate" | "amount_mismatch" | "stale_price" | "needs_approval"}""",
    """Reply with JSON only: {"reasoning": "<at most 60 words: check each rule in order against this payout's own fields>", "decision": "approve" | "hold", "reason_code": "ok" | "seller_on_hold" | "duplicate" | "amount_mismatch" | "stale_price" | "needs_approval"}""")
assert SYSTEM_1B != SYSTEM

ARMS = ("raw", "compiled")


def decisions_raw(payout: dict) -> list[dict]:
    """Raw arm: every decision in the ledger, for every payout."""
    return [{"id": d["id"], "date": d["date"], "text": d["text"]} for d in DECISIONS]


def decisions_compiled(payout: dict) -> list[dict]:
    """Compiled arm: only decisions that reference this payout's seller_id or one of its SKUs, plus the
    global rules (the refund-approval rule applies to every payout). Selected by reference, never by the answer."""
    skus = {x.get("sku") for x in payout["lines"]}
    out = []
    for d in DECISIONS:
        if (d["kind"] == "price_change" and d["sku"] in skus) \
                or (d["kind"] == "seller_hold" and d["seller"] == payout["seller_id"]) \
                or d["kind"] == "refund_approval":
            out.append({"id": d["id"], "date": d["date"], "text": d["text"]})
    return out


def input_line_1b(payout: dict, paid_invoices: dict[str, list[str]], arm: str, seed: int = 0,
                  compiler: str = "v1") -> dict:
    """compiler applies to the compiled arm only: v1 (run b) selects decisions; v2 (run e) also writes the
    resolved facts. The raw arm is the same for every compiler."""
    sel = {"raw": decisions_raw, "compiled": decisions_compiled}[arm]
    if arm == "compiled" and compiler == "v2":
        content = user_message_v2(payout, paid_invoices)
    elif compiler in ("v1", "v2"):
        content = user_message(payout, paid_invoices, sel(payout))
    else:
        raise ValueError(f"unknown compiler {compiler!r}")
    return {"item_id": f"{payout['payout_id']}/{arm}", "task": TASK, "arm": arm,
            "messages": [{"role": "system", "content": SYSTEM_1B},
                         {"role": "user", "content": content}],
            "params": {"temperature": 0, "max_tokens": MAX_TOKENS_1B, "seed": seed}}


# ---------------------------------------------------------------- compiler v2: resolved facts
# Rules 1-3 are lookups and arithmetic, so code resolves them and the model reads the answer.
# Each fact is computed from the payout, the decision ledger and the paid invoices only, never from truth().

def active_holds(payout_date: str) -> dict[str, str]:
    """seller_id -> decision id, for seller holds dated on or before the payout date."""
    return {d["seller"]: d["id"] for d in DECISIONS if d["kind"] == "seller_hold" and d["date"] <= payout_date}


def fact_seller_on_hold(payout: dict) -> tuple[bool, str]:
    holds = active_holds(payout["payout_date"])
    s = payout["seller_id"]
    if s in holds:
        return True, f"decision {holds[s]} puts seller {s} on hold"
    return False, f"no decision in force puts seller {s} on hold"


def fact_duplicate(payout: dict, paid_invoices: dict[str, list[str]]) -> tuple[bool, str]:
    s, inv = payout["seller_id"], payout["invoice_id"]
    if inv in paid_invoices.get(s, []):
        return True, f"{inv} is already in seller {s}'s paid invoices"
    return False, f"{inv} is not in seller {s}'s paid invoices"


def _line_cents(x: dict) -> int:
    return x["qty"] * x["unit_price_cents"] if x["type"] == "sale" else -x["refund_cents"]


def fact_amount_mismatch(payout: dict) -> tuple[bool, str]:
    total = sum(_line_cents(x) for x in payout["lines"])
    amount = payout["amount_cents"]
    if amount != total:
        return True, f"amount_usd {usd(amount)} differs from the sum of line_total_usd {usd(total)}"
    return False, f"amount_usd {usd(amount)} equals the sum of line_total_usd {usd(total)}"


def resolved_facts(payout: dict, paid_invoices: dict[str, list[str]]) -> dict[str, tuple[bool, str]]:
    return {"seller_on_hold": fact_seller_on_hold(payout),
            "duplicate": fact_duplicate(payout, paid_invoices),
            "amount_mismatch": fact_amount_mismatch(payout)}


def facts_header(payout: dict, paid_invoices: dict[str, list[str]],
                 remaining: str = "rule 4 stale_price, rule 5 needs_approval") -> str:
    facts = resolved_facts(payout, paid_invoices)
    return ("Resolved facts (computed by code from this payout, the decisions in force and the paid invoices; "
            "use them as given):\n"
            + "\n".join(f"- rule {i} {name}: {'APPLIES' if hit else 'does not apply'} ({why})"
                        for i, (name, (hit, why)) in enumerate(facts.items(), start=1))
            + f"\nStill to check from the decisions and lines below: {remaining}.\n\n")


def user_message_v2(payout: dict, paid_invoices: dict[str, list[str]]) -> str:
    """Compiled v2: the resolved facts for rules 1-3, then the v1 compiled context for rules 4-5."""
    return facts_header(payout, paid_invoices) + user_message(payout, paid_invoices, decisions_compiled(payout))


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


def user_message(payout: dict, paid_invoices: dict[str, list[str]], decisions: list[dict] | None = None) -> str:
    view = {"payout_id": payout["payout_id"], "payout_date": payout["payout_date"], "seller_id": payout["seller_id"],
            "invoice_id": payout["invoice_id"], "currency": payout["currency"],
            "amount_usd": usd(payout["amount_cents"]), "lines": [_line_view(x) for x in payout["lines"]]}
    import json
    return ("Decisions in force:\n" + json.dumps(decisions_for(payout) if decisions is None else decisions, indent=1)
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


# ---------------------------------------------------------------- run e: support notes (judgment)
# Run b's builders above stay byte-identical; run e has its own prompt, schema and decision list.
from .data import DECISIONS_1E, REASONS_1E  # noqa: E402

SYSTEM_1E = """You check marketplace seller payouts before they are sent. For one payout, decide "approve" or "hold" and give one reason code.

Apply these rules in this order and use the first one that applies:
1. seller_on_hold: a decision in force puts the payout's seller on hold.
2. duplicate: the payout's invoice_id is already in the seller's paid invoices.
3. amount_mismatch: amount_usd is not equal to the sum of the lines' line_total_usd.
4. stale_price: a sale line dated on or after a price change's effective date uses the old unit price.
5. needs_approval: a refund line for an order placed more than 30 days before the payout date has no approval, either as an approval_id on the line or as a support note that counts under the decisions in force.
6. disputed: under the decisions in force, a buyer dispute or chargeback on the payout's invoice is still open.
If none applies, approve with reason_code "ok". A hold always names the rule that applies; an approve is always "ok".
Support notes are free text from finance, support, sellers or buyers; read who wrote each one and what it refers to.

Reply with JSON only: {"reasoning": "<at most 60 words: check each rule in order against this payout's own fields and notes>", "decision": "approve" | "hold", "reason_code": "ok" | "seller_on_hold" | "duplicate" | "amount_mismatch" | "stale_price" | "needs_approval" | "disputed"}"""

OUTPUT_SCHEMA_1E = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "decision": {"type": "string", "enum": ["approve", "hold"]},
        "reason_code": {"type": "string", "enum": REASONS_1E},
    },
    "required": ["reasoning", "decision", "reason_code"],
    "additionalProperties": False,
}


def decisions_raw_1e(payout: dict) -> list[dict]:
    """Raw arm (run e): every decision in the run-e ledger, for every payout."""
    return [{"id": d["id"], "date": d["date"], "text": d["text"]} for d in DECISIONS_1E]


def decisions_compiled_1e(payout: dict) -> list[dict]:
    """Compiled context (run e): decisions naming this payout's seller or SKUs, plus the global rules
    (refund approval and both note policies). Selected by reference, never by the answer."""
    skus = {x.get("sku") for x in payout["lines"]}
    return [{"id": d["id"], "date": d["date"], "text": d["text"]} for d in DECISIONS_1E
            if (d["kind"] == "price_change" and d["sku"] in skus)
            or (d["kind"] == "seller_hold" and d["seller"] == payout["seller_id"])
            or d["kind"] in ("refund_approval", "note_policy")]


def user_message_1e(payout: dict, paid_invoices: dict[str, list[str]], decisions: list[dict]) -> str:
    import json
    notes = [{"from": x["from"], "date": x["date"], "text": x["text"]} for x in payout.get("notes", [])]  # no intent
    return (user_message(payout, paid_invoices, decisions)
            + "\n\nSupport notes on this payout:\n" + (json.dumps(notes, indent=1) if notes else "none"))


def input_line_1e(payout: dict, paid_invoices: dict[str, list[str]], arm: str, seed: int = 0) -> dict:
    """raw: every decision; compiled: compiler v2 (resolved facts for rules 1-3, then the compiled context)."""
    if arm == "raw":
        content = user_message_1e(payout, paid_invoices, decisions_raw_1e(payout))
    elif arm == "compiled":
        content = (facts_header(payout, paid_invoices, "rule 4 stale_price, rule 5 needs_approval (with the "
                                "support notes), rule 6 disputed (from the support notes)")
                   + user_message_1e(payout, paid_invoices, decisions_compiled_1e(payout)))
    else:
        raise ValueError(arm)
    return {"item_id": f"{payout['payout_id']}/{arm}", "task": TASK, "arm": arm,
            "messages": [{"role": "system", "content": SYSTEM_1E}, {"role": "user", "content": content}],
            "params": {"temperature": 0, "max_tokens": MAX_TOKENS_1B, "seed": seed}}


# ---------------------------------------------------------------- compiler v3 (run h): rules 4-5 in code too
# Rules 4 and 5 are lookups and date arithmetic like rules 1-3, so code resolves them as facts. Rule 5 is
# resolved as far as code can: an old refund with no approval_id on the line is a fact; whether a support note
# from finance approves it (D-106) is the model's to read. Rule 6 (disputes) is read from the notes. Every fact
# comes from the payout and the decisions in force (prices from the SKU catalogue the price-change decisions
# describe), never from truth(). Same system prompt as runs e and g.
from .data import SKUS  # noqa: E402

REFUND_APPROVAL_DAYS = 30


def price_changes_in_force(payout_date: str) -> dict[str, dict]:
    """sku -> {"id", "date", "old", "new"} for price-change decisions dated on or before the payout date."""
    return {d["sku"]: {"id": d["id"], "date": d["date"], "old": SKUS[d["sku"]]["old_price"],
                       "new": SKUS[d["sku"]]["price"]}
            for d in DECISIONS_1E if d["kind"] == "price_change" and d["date"] <= payout_date}


def fact_stale_price(payout: dict) -> tuple[bool, str]:
    changes = price_changes_in_force(payout["payout_date"])
    for x in payout["lines"]:
        c = changes.get(x.get("sku", ""))
        if x["type"] == "sale" and c and x["sale_date"] >= c["date"] and x["unit_price_cents"] == c["old"]:
            return True, (f"the {SKUS[x['sku']]['name']} line sold on {x['sale_date']} uses the old unit price "
                          f"${usd(c['old'])}; decision {c['id']} set ${usd(c['new'])} from {c['date']}")
    checked = sorted({x["sku"] for x in payout["lines"] if x["type"] == "sale" and x["sku"] in changes})
    return False, ("no sale line uses an old price on or after its price change"
                   + (f" (checked {', '.join(checked)})" if checked else " (no repriced product on this payout)"))


def old_unapproved_refunds(payout: dict) -> list[tuple[str, int]]:
    """(order_id, days before the payout) for refund lines older than 30 days with no approval_id on the line."""
    pd = __import__("datetime").date.fromisoformat(payout["payout_date"])
    out = []
    for x in payout["lines"]:
        if x["type"] != "refund" or x.get("approval_id"):
            continue
        days = (pd - __import__("datetime").date.fromisoformat(x["order_date"])).days
        if days > REFUND_APPROVAL_DAYS:
            out.append((x["order_id"], days))
    return out


def facts_header_v3(payout: dict, paid_invoices: dict[str, list[str]]) -> str:
    facts = resolved_facts(payout, paid_invoices)
    facts["stale_price"] = fact_stale_price(payout)
    lines = [f"- rule {i} {name}: {'APPLIES' if hit else 'does not apply'} ({why})"
             for i, (name, (hit, why)) in enumerate(facts.items(), start=1)]
    old = old_unapproved_refunds(payout)
    if not old:
        refunds = [x for x in payout["lines"] if x["type"] == "refund"]
        lines.append("- rule 5 needs_approval: does not apply ("
                     + ("no refund line" if not refunds else
                        "every refund line is either within 30 days of the payout date or carries an approval_id")
                     + ")")
    else:
        orders = "; ".join(f"order {o}, placed {d} days before the payout date" for o, d in old)
        lines.append(f"- rule 5 needs_approval: APPLIES unless a support note approves it ({orders}; no approval_id "
                     "on the line). Under D-106 only a note from the finance team that names this order_id and "
                     "gives an approval id counts")
    return ("Resolved facts (computed by code from this payout, the decisions in force and the paid invoices; "
            "use them as given):\n" + "\n".join(lines)
            + "\nStill to read from the support notes: whether a note approves the refund in rule 5 (only if rule 5 "
              "says so), and rule 6 disputed.\n\n")


def input_line_1h(payout: dict, paid_invoices: dict[str, list[str]], seed: int = 0) -> dict:
    """Compiler v3, compiled arm only (run h): resolved facts for rules 1-5, then the compiled context and notes."""
    content = facts_header_v3(payout, paid_invoices) + user_message_1e(payout, paid_invoices,
                                                                        decisions_compiled_1e(payout))
    return {"item_id": f"{payout['payout_id']}/compiled", "task": TASK, "arm": "compiled",
            "messages": [{"role": "system", "content": SYSTEM_1E}, {"role": "user", "content": content}],
            "params": {"temperature": 0, "max_tokens": MAX_TOKENS_1B, "seed": seed}}
