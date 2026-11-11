"""Synthetic payout run and decision ledger (docs/POC_PAYOUTS.md). SYNTHETIC: no real sellers or money.

Ground truth comes from `truth()`, deterministic rules over the payout and the ledger, not from the
generator's intent; the generator asserts the two agree, so every payout has exactly one known
answer. Money is in integer cents; strings with two decimals in anything a model reads.
"""

from __future__ import annotations

import datetime as dt
import random
import re

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

# Run e (pre-registered 2026-11-10): n = 200 drawn from a 210-payout pool (the Phase 1 label mix x7) on a seed
# never used before, excluding any payout identical in content to one of run b's. The bar (Wilson 95% lower
# bound >= 0.90) needs >= 189/200.
SEED_1E = 20261112
SEED_1H = 20261113  # run h (Phase 1c): fresh, never used (runs: 20261109 P1, 20261110 b, 20261111 c, 20261112 e/g)
PLAN_1E_POOL = PLAN * 7
N_1E = 200


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
    # Held sellers never get a normal payout. S-207 only enters the range for plans over 96 payouts, so
    # excluding it leaves every earlier run (30 and 60 payouts) byte-identical.
    normal_sellers = [f"S-{n}" for n in range(101, max(140, 110 + len(plan))) if f"S-{n}" not in ("S-114", "S-207")]
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


def content_key(payout: dict) -> str:
    """Identity of a payout by content (payout_id is renumbered per run): seller, invoice, amount, lines."""
    import json
    return json.dumps([payout["seller_id"], payout["invoice_id"], payout["amount_cents"], payout["lines"]],
                      sort_keys=True)


def draw(pool: dict, n: int, seed: int, exclude: set[str]) -> dict:
    """n payouts from a generated pool, none whose content_key is in `exclude`, by a seeded shuffle.
    The paid-invoices ledger stays the pool's, so truth() is unchanged for every drawn payout."""
    eligible = [p for p in pool["payouts"] if content_key(p) not in exclude]
    if len(eligible) < n:
        raise ValueError(f"pool has {len(eligible)} eligible payouts, need {n}")
    rng = random.Random(seed)
    picked = sorted(rng.sample(eligible, n), key=lambda p: p["payout_id"])
    ids = {p["payout_id"] for p in picked}
    return {**pool, "payouts": picked, "truth": {k: v for k, v in pool["truth"].items() if k in ids},
            "pool": {"size": len(pool["payouts"]), "excluded": len(pool["payouts"]) - len(eligible), "drawn": n}}


# ---------------------------------------------------------------- run e: support notes (judgment)
# Rules 1-5 are lookups and arithmetic: code alone gets them all right. Run e adds free-text support notes on
# some payouts and two policies that say which notes count. A note's effect (its intent) is set by the
# generator and never shown to the model; the model must read who wrote the note, what it refers to and
# whether it is still in force. SYNTHETIC: our own templates, so a parser written against them could decode
# them; they stand in for text a code-only rule set does not read.

REASONS_1E = REASONS + ["disputed"]
PRIORITY_1E = REASONS_1E[1:]

DECISIONS_1E = DECISIONS + [
    {"id": "D-106", "date": "2026-10-20", "kind": "note_policy",
     "text": "Finance may approve an old refund in a support note: a note from the finance team that names the "
             "refund's order_id and gives an approval id counts as that refund's approval. Notes from sellers or "
             "buyers never count as approval."},
    {"id": "D-107", "date": "2026-10-20", "kind": "note_policy",
     "text": "Hold a payout while a buyer dispute or chargeback on its invoice is open (reason: disputed). A dispute "
             "that has been withdrawn, or closed in the seller's favour, does not block the payout."},
]

# Intents that change the answer the structured fields give, and distractors that don't.
NOTE_CHANGES = ("finance_approval", "dispute_open")
NOTE_DISTRACTORS = ("seller_claims_approval", "finance_approval_other_order", "dispute_withdrawn",
                    "dispute_other_invoice", "chatter")
NOTE_PLAN_1E = {  # counts over the 210-payout pool
    "finance_approval": 10, "dispute_open": 30,
    "seller_claims_approval": 4, "finance_approval_other_order": 3,  # leaves 4 old refunds with no note
    "dispute_withdrawn": 12, "dispute_other_invoice": 12, "chatter": 20,
}

_T = {
    "finance_approval": [
        ("finance", "Approved the late refund on {order}. Approval {fa}."),
        ("finance", "Finance here: the refund for order {order} is signed off, reference {fa}."),
        ("finance", "Following our review, refund {order} is approved ({fa}). OK to pay out."),
    ],
    "seller_claims_approval": [
        ("seller", "Finance already approved the refund on {order}, approval {fa}. Please release the payout."),
        ("seller", "We cleared the {order} refund with your finance team last week (ref {fa}), so this is fine."),
    ],
    "finance_approval_other_order": [
        ("finance", "Approved the late refund on {other_order}. Approval {fa}."),
        ("finance", "Finance here: refund {other_order} is signed off, reference {fa}."),
    ],
    "dispute_open": [
        ("support", "The buyer filed a chargeback with their card issuer for {invoice}; case {cb} is open."),
        ("support", "Dispute opened by the buyer on invoice {invoice}: they say the seats were never delivered. "
                    "Case {cb}, waiting on the seller's evidence."),
        ("buyer", "I am disputing {invoice}. We cancelled before the renewal and were charged anyway. "
                  "Opened case {cb} with my bank."),
    ],
    "dispute_withdrawn": [
        ("support", "The buyer filed a chargeback for {invoice}; case {cb} is open.",
         "support", "Update on {invoice}: the buyer withdrew chargeback {cb}. Case closed in the seller's favour."),
        ("buyer", "Disputing {invoice}, we never got access. Case {cb}.",
         "support", "Case {cb} on {invoice} is resolved: access was confirmed and the buyer closed the dispute."),
    ],
    "dispute_other_invoice": [
        ("support", "The buyer filed a chargeback for {other_invoice}; case {cb} is open."),
        ("support", "Dispute opened on invoice {other_invoice} (a different payout); case {cb}."),
    ],
    "chatter": [
        ("seller", "Can you confirm when this payout lands? Our bank details have not changed."),
        ("seller", "Thanks for the fast payouts this month. Please send the remittance PDF to accounts."),
        ("support", "Seller asked to change their payout email; verified and updated."),
    ],
}


def _note_eligible(label: str, intent: str) -> bool:
    if intent in ("finance_approval", "seller_claims_approval", "finance_approval_other_order"):
        return label == "needs_approval"  # an old refund without approval_id
    if intent in ("dispute_open", "dispute_withdrawn", "dispute_other_invoice"):
        return label == "ok"              # plain ok: no other rule applies
    return True


def add_notes(pool: dict, seed: int, plan: dict = NOTE_PLAN_1E) -> dict:
    """Attach support notes to pool payouts by NOTE_PLAN_1E; set truth with truth_1e. Each payout gets at
    most one intent. `notes[*].intent` is hidden from the model (task.payout_view drops it)."""
    rng = random.Random(seed)
    labels = {p["payout_id"]: pool["truth"][p["payout_id"]]["reason_code"] for p in pool["payouts"]}
    by_id = {p["payout_id"]: p for p in pool["payouts"]}
    free = sorted(by_id)
    rng.shuffle(free)
    taken: set[str] = set()
    for intent in sorted(plan, key=lambda k: (k == "chatter", k)):  # chatter last: it fits anything
        n = plan[intent]
        ids = [i for i in free if i not in taken and _note_eligible(labels[i], intent)
               and (intent != "dispute_open" or by_id[i]["lines"][0]["type"] == "sale")][:n]
        if len(ids) < n:
            raise ValueError(f"only {len(ids)} payouts eligible for {intent}, need {n}")
        for i in ids:
            taken.add(i)
            p = by_id[i]
            refund = next((x for x in p["lines"] if x["type"] == "refund"), None)
            fill = {"order": refund["order_id"] if refund else "", "fa": f"FA-{rng.randint(1000, 9999)}",
                    "other_order": f"O-{rng.randint(100000, 999999)}", "invoice": p["invoice_id"],
                    "other_invoice": f"INV-{rng.randint(10000, 99999)}", "cb": f"CB-{rng.randint(10000, 99999)}"}
            t = rng.choice(_T[intent])
            notes = [{"from": t[0], "date": _days_before(rng.randint(2, 4)), "text": t[1].format(**fill)}]
            if len(t) == 4:  # an open dispute, then a later note that closes it
                notes.append({"from": t[2], "date": _days_before(1), "text": t[3].format(**fill)})
            for x in notes:
                x["intent"] = intent
            p["notes"] = notes
    for p in pool["payouts"]:
        p.setdefault("notes", [])
    pool["truth"] = {p["payout_id"]: truth_1e(p, pool["paid_invoices"]) for p in pool["payouts"]}
    pool["decisions"] = DECISIONS_1E
    return pool


def truth_1e(payout: dict, paid_invoices: dict[str, list[str]]) -> dict:
    """truth() plus the notes' hidden intents: a finance approval of this payout's refund removes
    needs_approval; an open dispute on this payout's invoice adds disputed (last in priority)."""
    intents = {x["intent"] for x in payout.get("notes", [])}
    base = dict(payout)
    if "finance_approval" in intents:
        base["lines"] = [dict(x, approval_id="FA-NOTE") if x["type"] == "refund" else x for x in payout["lines"]]
    t = truth(base, paid_invoices)
    if t["reason_code"] == "ok" and "dispute_open" in intents:
        return {"decision": "hold", "reason_code": "disputed"}
    return t


def truth_rules_only(payout: dict, paid_invoices: dict[str, list[str]]) -> dict:
    """The rules-only baseline: rules 1-5 in code over the structured fields; notes are free text, unread."""
    return truth(payout, paid_invoices)


_APPROVE_RE = re.compile(r"\b(approv\w*|signed off|sign-off)\b", re.IGNORECASE)
_DISPUTE_RE = re.compile(r"\b(disput\w*|chargeback\w*)\b", re.IGNORECASE)
_CLOSED_RE = re.compile(r"\b(withdr\w*|closed|resolved)\b", re.IGNORECASE)


def truth_rules_regex(payout: dict, paid_invoices: dict[str, list[str]]) -> dict:
    """Rules + a simple keyword/regex reading of the notes, no model. A finance note that names the refund's
    order_id and matches an approval keyword approves it; a note naming this invoice with a dispute keyword
    holds it as disputed, unless a later note naming the invoice matches a withdrawn/closed keyword."""
    notes = sorted(payout.get("notes", []), key=lambda x: x["date"])
    base = dict(payout)
    base["lines"] = [dict(x, approval_id="FA-REGEX") if x["type"] == "refund" and not x.get("approval_id") and any(
        n["from"] == "finance" and x["order_id"] in n["text"] and _APPROVE_RE.search(n["text"]) for n in notes)
        else x for x in payout["lines"]]
    t = truth(base, paid_invoices)
    if t["reason_code"] != "ok":
        return t
    open_ = False
    for n in notes:
        if payout["invoice_id"] in n["text"]:
            if _CLOSED_RE.search(n["text"]):
                open_ = False
            elif _DISPUTE_RE.search(n["text"]):
                open_ = True
    return {"decision": "hold", "reason_code": "disputed"} if open_ else t


def truth_keywords_naive(payout: dict, paid_invoices: dict[str, list[str]]) -> dict:
    """The naive reading: any approval keyword approves every refund; any dispute keyword holds the payout
    unless a closed keyword appears. No sender, order or invoice check."""
    text = " ".join(n["text"] for n in payout.get("notes", []))
    base = dict(payout)
    if _APPROVE_RE.search(text):
        base["lines"] = [dict(x, approval_id="FA-KW") if x["type"] == "refund" else x for x in payout["lines"]]
    t = truth(base, paid_invoices)
    if t["reason_code"] == "ok" and _DISPUTE_RE.search(text) and not _CLOSED_RE.search(text):
        return {"decision": "hold", "reason_code": "disputed"}
    return t
