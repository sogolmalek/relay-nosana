"""Compiler v2 (run e): rules 1-3 resolved in code. Each fact is tested on hand-built payouts, then against
the generator's labels over the whole run-e pool. The raw arm and run b's inputs must not change."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from relay_nosana.payouts import task
from relay_nosana.payouts.data import (N_1E, PLAN_1B, PLAN_1E_POOL, SEED_1B, SEED_1E, content_key, draw,
                                       generate)
from relay_nosana.nosana import jobdef as jd_mod

ROOT = Path(__file__).resolve().parent.parent


def _payout(seller="S-101", invoice="INV-1", amount=8400, date="2026-11-09", lines=None):
    lines = lines if lines is not None else [
        {"type": "sale", "sku": "PRO", "qty": 2, "sale_date": "2026-11-05", "unit_price_cents": 4200}]
    return {"payout_id": "P-0001", "payout_date": date, "seller_id": seller, "invoice_id": invoice,
            "currency": "USD", "amount_cents": amount, "lines": lines}


# ---------------------------------------------------------------- rule 1: seller on an active hold

@pytest.mark.parametrize("seller,decision", [("S-114", "D-103"), ("S-207", "D-104")])
def test_seller_on_active_hold(seller, decision):
    hit, why = task.fact_seller_on_hold(_payout(seller=seller))
    assert hit and decision in why and seller in why


def test_seller_not_on_hold():
    hit, why = task.fact_seller_on_hold(_payout(seller="S-101"))
    assert not hit and "S-101" in why


def test_hold_not_yet_in_force_does_not_apply():
    # D-104 (S-207) is dated 2026-11-06: a payout dated before it is not held by it
    assert not task.fact_seller_on_hold(_payout(seller="S-207", date="2026-11-05"))[0]
    assert task.fact_seller_on_hold(_payout(seller="S-207", date="2026-11-06"))[0]


# ---------------------------------------------------------------- rule 2: duplicate invoice

def test_duplicate_when_invoice_already_paid_to_same_seller():
    hit, why = task.fact_duplicate(_payout(invoice="INV-7"), {"S-101": ["INV-3", "INV-7"]})
    assert hit and "INV-7" in why


@pytest.mark.parametrize("paid", [{}, {"S-101": ["INV-3"]}, {"S-999": ["INV-7"]}])
def test_not_duplicate(paid):
    # unpaid, or paid only to a different seller
    assert not task.fact_duplicate(_payout(invoice="INV-7"), paid)[0]


# ---------------------------------------------------------------- rule 3: amount matches the lines

def test_amount_matches_sales_and_refunds():
    lines = [{"type": "sale", "sku": "TEAM", "qty": 3, "sale_date": "2026-11-05", "unit_price_cents": 12900},
             {"type": "refund", "order_id": "O-1", "order_date": "2026-10-01", "refund_cents": 4200}]
    hit, why = task.fact_amount_mismatch(_payout(amount=3 * 12900 - 4200, lines=lines))
    assert not hit and "345.00" in why


@pytest.mark.parametrize("delta", [-1, 1, 1000])
def test_amount_mismatch(delta):
    hit, why = task.fact_amount_mismatch(_payout(amount=8400 + delta))
    assert hit and "84.00" in why


# ---------------------------------------------------------------- against the generator over run e's pool

def test_facts_agree_with_generator_labels_over_pool():
    pool = generate(SEED_1E, PLAN_1E_POOL)
    for p in pool["payouts"]:
        facts = task.resolved_facts(p, pool["paid_invoices"])
        code = pool["truth"][p["payout_id"]]["reason_code"]
        if code in facts:  # the first rule that applies must be resolved as applying
            assert facts[code][0], (p["payout_id"], code)
        first = next((name for name, (hit, _) in facts.items() if hit), None)
        if first is not None:
            assert code == first, (p["payout_id"], code, first)  # rules 1-3 outrank 4-5


def test_v2_message_carries_facts_and_keeps_v1_context():
    pool = generate(SEED_1E, PLAN_1E_POOL)
    p = pool["payouts"][0]
    msg = task.input_line_1b(p, pool["paid_invoices"], "compiled", compiler="v2")["messages"][1]["content"]
    assert msg.startswith("Resolved facts")
    for i, name in enumerate(["seller_on_hold", "duplicate", "amount_mismatch"], start=1):
        assert f"rule {i} {name}:" in msg
    assert msg.endswith(task.user_message(p, pool["paid_invoices"], task.decisions_compiled(p)))


def test_raw_arm_is_the_same_for_every_compiler():
    pool = generate(SEED_1E, PLAN_1E_POOL)
    p = pool["payouts"][3]
    assert (task.input_line_1b(p, pool["paid_invoices"], "raw", compiler="v1")
            == task.input_line_1b(p, pool["paid_invoices"], "raw", compiler="v2"))


def test_run_b_inputs_unchanged():
    run = generate(SEED_1B, PLAN_1B)
    lines = [task.input_line_1b(p, run["paid_invoices"], arm) for p in run["payouts"] for arm in task.ARMS]
    pre = json.loads((ROOT / "runs/payouts-2026-11-10b/preregistration.json").read_text())
    assert hashlib.sha256(jd_mod.inputs_bytes(lines)).hexdigest() == pre["inputs_sha256"]


# ---------------------------------------------------------------- run e sample

def test_run_e_sample_excludes_run_b_and_is_deterministic():
    b = json.loads((ROOT / "runs/payouts-2026-11-10b/payout_run.json").read_text())
    excl = {content_key(p) for p in b["payouts"]}
    pool = generate(SEED_1E, PLAN_1E_POOL)
    e1, e2 = draw(pool, N_1E, SEED_1E, excl), draw(pool, N_1E, SEED_1E, excl)
    assert e1["payouts"] == e2["payouts"] and len(e1["payouts"]) == 200
    assert not ({content_key(p) for p in e1["payouts"]} & excl)
    assert set(e1["truth"]) == {p["payout_id"] for p in e1["payouts"]}


def test_threshold_at_n_200():
    assert task.wilson(189, 200)[0] >= 0.90 > task.wilson(188, 200)[0]


# ---------------------------------------------------------------- run e: support notes (judgment)

from relay_nosana.payouts.data import (NOTE_CHANGES, NOTE_DISTRACTORS, NOTE_PLAN_1E, add_notes, truth_1e,  # noqa: E402
                                       truth_rules_only)


def _pool_1e():
    return add_notes(generate(SEED_1E, PLAN_1E_POOL), SEED_1E)


def test_notes_follow_plan_and_intent_is_hidden_from_the_model():
    pool = _pool_1e()
    got: dict[str, int] = {}
    for p in pool["payouts"]:
        if p["notes"]:
            got[p["notes"][0]["intent"]] = got.get(p["notes"][0]["intent"], 0) + 1
        for arm in task.ARMS:
            line = task.input_line_1e(p, pool["paid_invoices"], arm)
            assert "intent" not in line["messages"][1]["content"]
            for word in NOTE_CHANGES + NOTE_DISTRACTORS:
                assert word not in line["messages"][1]["content"]
    assert got == NOTE_PLAN_1E


def test_notes_change_exactly_the_planned_answers():
    pool = _pool_1e()
    for p in pool["payouts"]:
        intent = p["notes"][0]["intent"] if p["notes"] else None
        t, r = pool["truth"][p["payout_id"]], truth_rules_only(p, pool["paid_invoices"])
        if intent == "finance_approval":
            assert r["reason_code"] == "needs_approval" and t == {"decision": "approve", "reason_code": "ok"}
        elif intent == "dispute_open":
            assert r["reason_code"] == "ok" and t == {"decision": "hold", "reason_code": "disputed"}
        else:  # distractors and no note: the notes don't change the answer
            assert t == r, (p["payout_id"], intent)


def test_distractor_notes_name_the_wrong_sender_order_or_invoice_or_are_closed():
    pool = _pool_1e()
    for p in pool["payouts"]:
        if not p["notes"]:
            continue
        intent, notes = p["notes"][0]["intent"], p["notes"]
        refund = next((x for x in p["lines"] if x["type"] == "refund"), None)
        if intent == "seller_claims_approval":
            assert notes[0]["from"] == "seller" and refund["order_id"] in notes[0]["text"]
        if intent in ("finance_approval", "finance_approval_other_order"):
            assert notes[0]["from"] == "finance"
            assert (refund["order_id"] in notes[0]["text"]) == (intent == "finance_approval")
        if intent in ("dispute_open", "dispute_other_invoice"):
            assert (p["invoice_id"] in notes[0]["text"]) == (intent == "dispute_open")
        if intent == "dispute_withdrawn":
            assert len(notes) == 2 and notes[0]["date"] < notes[1]["date"] and p["invoice_id"] in notes[1]["text"]


def test_run_e_share_of_note_dependent_items():
    b = json.loads((ROOT / "runs/payouts-2026-11-10b/payout_run.json").read_text())
    e = draw(_pool_1e(), N_1E, SEED_1E, {content_key(p) for p in b["payouts"]})
    changed = [p for p in e["payouts"] if e["truth"][p["payout_id"]] != truth_rules_only(p, e["paid_invoices"])]
    assert 0.10 <= len(changed) / N_1E <= 0.30


def test_run_e_system_prompt_and_schema_add_rule_6_without_touching_run_b():
    assert "6. disputed" in task.SYSTEM_1E and "6." not in task.SYSTEM_1B
    assert task.OUTPUT_SCHEMA_1E["properties"]["reason_code"]["enum"][-1] == "disputed"
    assert "disputed" not in task.OUTPUT_SCHEMA_1B["properties"]["reason_code"]["enum"]


# ---------------------------------------------------------------- fourth no-model baseline: rules + regex on notes

from relay_nosana.payouts.data import truth_keywords_naive, truth_rules_regex  # noqa: E402


def _no_intent(p):
    return dict(p, notes=[{k: v for k, v in n.items() if k != "intent"} for n in p["notes"]])


def test_regex_baselines_never_read_the_hidden_intent():
    pool = _pool_1e()
    for p in pool["payouts"]:
        assert truth_rules_regex(p, pool["paid_invoices"]) == truth_rules_regex(_no_intent(p), pool["paid_invoices"])
        assert truth_keywords_naive(p, pool["paid_invoices"]) == \
            truth_keywords_naive(_no_intent(p), pool["paid_invoices"])


def test_regex_baselines_on_run_e_the_notes_are_template_easy():
    """Recorded finding (2026-11-10), before run e is posted: a short regex over the notes recovers every label,
    so on these synthetic notes the 7B column can't show judgment that code lacks."""
    e = json.loads((ROOT / "runs" / "payouts-2026-11-10e" / "payout_run.json").read_text())
    paid, t = e["paid_invoices"], e["truth"]
    score = {f.__name__: sum(f(p, paid) == t[p["payout_id"]] for p in e["payouts"])
             for f in (truth_rules_only, truth_rules_regex, truth_keywords_naive)}
    assert score == {"truth_rules_only": 166, "truth_rules_regex": 200, "truth_keywords_naive": 183}


def test_frontier_column_issues_declared_receipts_the_gate_refuses_over_500(tmp_path):
    from relay_nosana import gate
    from relay_nosana import rules as rules_mod
    from relay_nosana.payouts import run_1b
    from relay_nosana.payouts.run_poc import amounts
    from relay_verify.checks import verdict, verify
    base = ROOT / "runs" / "payouts-2026-11-10e"
    run = json.loads((base / "payout_run.json").read_text())
    lines = [x for x in map(json.loads, (base / "inputs.jsonl").read_text().splitlines()) if x["arm"] == "compiled"]
    assert len(lines) == 200
    amt = amounts(run)
    hi = next(x for x in lines if amt[x["item_id"].split("/")[0]] > 500)
    lo = next(x for x in lines if amt[x["item_id"].split("/")[0]] <= 500)
    P = {"base": tmp_path, "receipts": tmp_path / "receipts"}
    with (tmp_path / "frontier.jsonl").open("w") as fh:
        for x in (hi, lo):
            fh.write(json.dumps({"item_id": x["item_id"], "model": "claude-opus-5-5", "session_id": "s-" + x["item_id"],
                                 "timestamp": "2026-11-10T18:00:00Z", "error": None,
                                 "output": {"reasoning": "r", "decision": "approve", "reason_code": "ok"}}) + "\n")
    rules = {r["id"]: r for r in rules_mod.load()}
    recs = run_1b._frontier_receipts(P, run, amt, rules, {x["item_id"]: x for x in lines})
    assert len(list((tmp_path / "receipts" / "frontier").glob("*.json"))) == 2
    for x, want in ((hi, "refuse"), (lo, "pay")):
        r = recs[x["item_id"].split("/")[0]]
        assert r["grade"] == "declared" and r["output"] == {"decision": "approve", "reason_code": "ok"}
        assert verdict(verify(r, None)) == 0
        assert gate.decide(r, 0, rules[r["rule"]["id"]])["action"] == want


# ---------------------------------------------------------------- compiler v3 (run h): rules 4-5 as facts

from relay_nosana.payouts.data import SEED_1H  # noqa: E402


def _pool_1h():
    return add_notes(generate(SEED_1H, PLAN_1E_POOL), SEED_1H)


def _first_rule_v3(p, paid):
    f = task.resolved_facts(p, paid)
    order = [("seller_on_hold", f["seller_on_hold"][0]), ("duplicate", f["duplicate"][0]),
             ("amount_mismatch", f["amount_mismatch"][0]), ("stale_price", task.fact_stale_price(p)[0]),
             ("needs_approval", bool(task.old_unapproved_refunds(p)))]
    return next((r for r, hit in order if hit), "ok")


def test_v3_facts_agree_with_the_code_rules_over_run_h_pool():
    """Rules 1-5 resolved by v3 give exactly what the notes-blind rules give, on every payout of the fresh pool."""
    pool = _pool_1h()
    for p in pool["payouts"]:
        assert _first_rule_v3(p, pool["paid_invoices"]) == truth_rules_only(p, pool["paid_invoices"])["reason_code"]


def _sale(sku, date, cents):
    return {"type": "sale", "sku": sku, "qty": 1, "sale_date": date, "unit_price_cents": cents}


@pytest.mark.parametrize("line,hit", [
    (_sale("PRO", "2026-11-05", 4500), True),    # old price after the change
    (_sale("PRO", "2026-11-03", 4500), True),    # on the effective date
    (_sale("PRO", "2026-11-02", 4500), False),   # before the change
    (_sale("PRO", "2026-11-05", 4200), False),   # new price
    (_sale("TEAM", "2026-11-05", 12900), False),  # never repriced
])
def test_v3_stale_price_fact(line, hit):
    p = _payout(lines=[line])
    assert task.fact_stale_price(p)[0] is hit


@pytest.mark.parametrize("order_date,approval,hit", [
    ("2026-10-09", None, True),        # 31 days before the 2026-11-09 payout: more than 30
    ("2026-10-10", None, False),       # exactly 30 days: not more than 30
    ("2026-10-25", None, False),       # recent
    ("2026-10-01", "FA-1234", False),  # old but approved on the line
])
def test_v3_refund_fact(order_date, approval, hit):
    line = {"type": "refund", "order_id": "O-1", "order_date": order_date, "refund_cents": 1000}
    if approval:
        line["approval_id"] = approval
    p = _payout(lines=[_sale("TEAM", "2026-11-05", 12900), line])
    assert bool(task.old_unapproved_refunds(p)) is hit
    assert ("APPLIES unless a support note approves it" in task.facts_header_v3(p, {})) is hit


def test_v3_message_states_rules_1_to_5_and_hides_the_intent():
    pool = _pool_1h()
    for p in pool["payouts"][:80]:
        line = task.input_line_1h(p, pool["paid_invoices"])
        user = line["messages"][1]["content"]
        for i in range(1, 6):
            assert f"- rule {i} " in user
        assert "intent" not in user and not any(n["intent"] in user for n in p.get("notes", []))
        assert line["messages"][0]["content"] == task.SYSTEM_1E


def test_run_h_seed_is_fresh_and_excludes_earlier_payouts():
    from relay_nosana.payouts.data import SEED, SEED_1B, SEED_1E
    from relay_nosana.payouts.run_fresh import SEED_FRESH
    assert SEED_1H not in {SEED, SEED_1B, SEED_1E, SEED_FRESH}
    seen = set()
    for r in ("payouts-2026-11-10b", "payouts-2026-11-10e"):
        seen |= {content_key(p) for p in json.loads((ROOT / "runs" / r / "payout_run.json").read_text())["payouts"]}
    h = draw(_pool_1h(), N_1E, SEED_1H, seen)
    assert len(h["payouts"]) == 200 and not ({content_key(p) for p in h["payouts"]} & seen)


def test_run_e_and_g_inputs_unchanged_by_v3():
    e = json.loads((ROOT / "runs" / "payouts-2026-11-10e" / "payout_run.json").read_text())
    want = (ROOT / "runs" / "payouts-2026-11-10e" / "inputs.jsonl").read_bytes()
    lines = [task.input_line_1e(p, e["paid_invoices"], arm) for p in e["payouts"] for arm in task.ARMS]
    assert jd_mod.inputs_bytes(lines) == want
