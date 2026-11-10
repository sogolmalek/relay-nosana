"""Synthetic payout run and decision ledger (docs/POC_PAYOUTS.md). SYNTHETIC: no real sellers or money.

Ground truth comes from `truth()`, deterministic rules over the payout and the ledger, not from the
generator's intent; the generator asserts the two agree, so every payout has exactly one known
answer. Money is in integer cents; strings with two decimals in anything a model reads.
"""

from __future__ import annotations

import datetime as dt
import random

RUN_DATE = "2026-11-09"
SEED = 20261109
SYNTHETIC_NOTE = "Synthetic data generated for the Relay x Nosana POC. Not real sellers, payouts or money."

SKUS = {
    "PRO": {"name": "Pro plan", "price": 4200, "old_price": 4500, "changed": "2026-11-03"},
    "ENT": {"name": "Enterprise seat", "price": 115000, "old_price": 120000, "changed": "2026-11-01"},
    "TEAM": {"name": "Team plan", "price": 12900},
    "ADDON": {"name": "Storage add-on", "price": 900},
}

DECISIONS = [
    {"id": "D-101", "date": "2026-11-03", "kind": "price_change", "sku": "PRO",
     "text": "The Pro plan price dropped from $45.00 to $42.00 per unit for sales on or after 2026-11-03."},
    {"id": "D-102", "date": "2026-11-01", "kind": "price_change", "sku": "ENT",
     "text": "The Enterprise seat price dropped from $1200.00 to $1150.00 per unit for sales on or after 2026-11-01."},
    {"id": "D-103", "date": "2026-11-05", "kind": "seller_hold", "seller": "S-114",
     "text": "Seller S-114 is on a fraud hold. Hold every payout to S-114."},
    {"id": "D-104", "date": "2026-11-06", "kind": "seller_hold", "seller": "S-207",
     "text": "Seller S-207 is on a compliance hold. Hold every payout to S-207."},
    {"id": "D-105", "date": "2026-10-15", "kind": "refund_approval",
     "text": "A refund for an order placed more than 30 days before the payout date needs finance approval: "
             "hold the payout unless that refund line carries an approval_id."},
]

REASONS = ["ok", "seller_on_hold", "duplicate", "amount_mismatch", "stale_price", "needs_approval"]
PRIORITY = REASONS[1:]  # first that applies wins

# (label, amount band). Bands: small <= $500 (frontier lane), mid $500-$5000, high > $5000 (always re-run).
PLAN = (
    [("ok", "small")] * 3 + [("ok", "mid")] * 5 + [("ok", "high")] * 2
    + [("ok_old_price", "mid"), ("ok_refund_approved", "mid"), ("ok_refund_recent", "small")]
    + [("seller_on_hold", "small"), ("seller_on_hold", "mid"), ("seller_on_hold", "mid"), ("seller_on_hold", "high")]
    + [("duplicate", "small"), ("duplicate", "mid"), ("duplicate", "mid")]
    + [("amount_mismatch", "small"), ("amount_mismatch", "mid"), ("amount_mismatch", "high")]
    + [("stale_price", "small"), ("stale_price", "mid"), ("stale_price", "mid"), ("stale_price", "high")]
    + [("needs_approval", "small"), ("needs_approval", "mid"), ("needs_approval", "mid")]
)


# Phase 1b (2026-11-10): a fresh seed, never used before, and the Phase 1 label mix doubled to 60 payouts.
# 60 because the bar (Wilson 95% lower bound >= 0.90) can't be reached at n = 30 even with 30/30 (0.886);
# at n = 60 it is reached with at most one error (59/60 -> 0.911).
SEED_1B = 20261110
PLAN_1B = PLAN * 2


def usd(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _days_before(n: int) -> str:
    return (_date(RUN_DATE) - dt.timedelta(days=n)).isoformat()


# ---------------------------------------------------------------- ground truth

def line_total(line: dict) -> int:
    return line["qty"] * line["unit_price_cents"] if line["type"] == "sale" else -line["refund_cents"]


def truth(payout: dict, paid_invoices: dict[str, list[str]]) -> dict:
    """The one correct {"decision", "reason_code"}, by the rules in PRIORITY order."""
    holds = {d["seller"] for d in DECISIONS if d["kind"] == "seller_hold"}
    found = []
    if payout["seller_id"] in holds:
        found.append("seller_on_hold")
    if payout["invoice_id"] in paid_invoices.get(payout["seller_id"], []):
        found.append("duplicate")
    if payout["amount_cents"] != sum(line_total(x) for x in payout["lines"]):
        found.append("amount_mismatch")
    for x in payout["lines"]:
        sku = SKUS.get(x.get("sku", ""), {})
        if x["type"] == "sale" and "changed" in sku and x["sale_date"] >= sku["changed"] \
                and x["unit_price_cents"] == sku["old_price"]:
            found.append("stale_price")
            break
    for x in payout["lines"]:
        if x["type"] == "refund" and (_date(RUN_DATE) - _date(x["order_date"])).days > 30 and not x.get("approval_id"):
            found.append("needs_approval")
            break
    for reason in PRIORITY:
        if reason in found:
            return {"decision": "hold", "reason_code": reason}
    return {"decision": "approve", "reason_code": "ok"}


# ---------------------------------------------------------------- generator

def _sale(rng, sku, qty, date, price=None):
    return {"type": "sale", "sku": sku, "qty": qty, "sale_date": date,
            "unit_price_cents": SKUS[sku]["price"] if price is None else price}


def _base_lines(rng, band):
    d = _days_before(rng.randint(1, 5))  # after both price changes: current prices apply
    if band == "small":
        lines = [_sale(rng, "PRO", rng.randint(2, 7), d)]
        if rng.random() < 0.5:
            lines.append(_sale(rng, "ADDON", rng.randint(1, 6), d))
    elif band == "mid":
        lines = [_sale(rng, "TEAM", rng.randint(5, 25), d), _sale(rng, "PRO", rng.randint(1, 10), d)]
    else:
        lines = [_sale(rng, "ENT", rng.randint(5, 9), d), _sale(rng, "TEAM", rng.randint(1, 6), d)]
    return lines


def generate(seed: int = SEED, plan: list | tuple = PLAN) -> dict:
    rng = random.Random(seed)
    plan = list(plan)
    rng.shuffle(plan)
    # 101..139 for the 30-payout plan (unchanged from Phase 1); more sellers for a longer plan
    normal_sellers = [f"S-{n}" for n in range(101, max(140, 110 + len(plan))) if f"S-{n}" not in ("S-114",)]
    rng.shuffle(normal_sellers)
    paid_invoices: dict[str, list[str]] = {}
    payouts = []
    for i, (label, band) in enumerate(plan):
        pid = f"P-{101 + i:04d}"
        seller = rng.choice(["S-114", "S-207"]) if label == "seller_on_hold" else normal_sellers[i]
        invoice = f"INV-{rng.randint(10000, 99999)}"
        if rng.random() < 0.4 or label == "duplicate":
            prev = [f"INV-{rng.randint(10000, 99999)}" for _ in range(rng.randint(1, 3))]
            paid_invoices[seller] = sorted(set(paid_invoices.get(seller, []) + prev))
        lines = _base_lines(rng, band)
        if label == "duplicate":
            invoice = rng.choice(paid_invoices[seller])
        elif label == "stale_price":
            sku = "ENT" if band == "high" else "PRO"
            for x in lines:
                if x["sku"] == sku:
                    x["unit_price_cents"] = SKUS[sku]["old_price"]
                    break
            else:
                lines.append(_sale(rng, sku, 2, _days_before(2), SKUS[sku]["old_price"]))
        elif label == "ok_old_price":
            lines.append(_sale(rng, "PRO", rng.randint(2, 6), "2026-10-30", SKUS["PRO"]["old_price"]))
        elif label in ("needs_approval", "ok_refund_approved", "ok_refund_recent"):
            age = rng.randint(10, 25) if label == "ok_refund_recent" else rng.randint(35, 60)
            ref = {"type": "refund", "order_id": f"O-{rng.randint(100000, 999999)}", "order_date": _days_before(age),
                   "refund_cents": min(rng.choice([4200, 8400, 12900]), sum(line_total(x) for x in lines) // 2)}
            if label == "ok_refund_approved":
                ref["approval_id"] = f"FA-{rng.randint(1000, 9999)}"
            lines.append(ref)
        amount = sum(line_total(x) for x in lines)
        if label == "amount_mismatch":
            amount += rng.choice([-1, 1]) * rng.choice([1000, 4200, 12900])
        payout = {"payout_id": pid, "seller_id": seller, "invoice_id": invoice, "payout_date": RUN_DATE,
                  "currency": "USD", "amount_cents": amount, "lines": lines}
        want = {"decision": "approve", "reason_code": "ok"} if label.startswith("ok") \
            else {"decision": "hold", "reason_code": label}
        payouts.append((payout, want))
    # check against the final ledger, after every seller's paid invoices are known
    for payout, want in payouts:
        got = truth(payout, paid_invoices)
        assert got == want, (payout["payout_id"], want, got)
    payouts = [p for p, _ in payouts]
    return {"run_date": RUN_DATE, "seed": seed, "note": SYNTHETIC_NOTE, "decisions": DECISIONS,
            "paid_invoices": paid_invoices, "payouts": payouts,
            "truth": {p["payout_id"]: truth(p, paid_invoices) for p in payouts}}
