"""Phase 1b: raw vs compiled arms, reasoning not hashed, multi-block CIDs, known-good nodes."""

import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from relay_nosana.nosana import jobdef as jd_mod
from relay_nosana.nosana import nodes as nodes_mod
from relay_nosana.payouts import task
from relay_nosana.payouts.data import DECISIONS, PLAN, PLAN_1B, SEED, SEED_1B, generate
from relay_nosana.payouts.run_1b import build_batch, sign_test
from relay_verify.cid import cidv0_of_bytes

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("relay_batch_runner", ROOT / "batch_runner" / "run.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


@pytest.fixture(scope="module")
def run1b():
    return generate(SEED_1B, PLAN_1B)


def test_fresh_seed_plan_is_new_and_consistent(run1b):
    assert SEED_1B != SEED and len(run1b["payouts"]) == 60 == 2 * len(PLAN)
    assert len({p["payout_id"] for p in run1b["payouts"]}) == 60
    old = generate()
    assert {p["invoice_id"] for p in old["payouts"]}.isdisjoint({p["invoice_id"] for p in run1b["payouts"]}) \
        or old["payouts"][0] != run1b["payouts"][0]
    labels = [v["reason_code"] for v in run1b["truth"].values()]
    assert labels.count("ok") == 26 and labels.count("seller_on_hold") == 8


def test_bar_needs_more_than_30_items():
    assert task.wilson(30, 30)[0] < 0.90 <= task.wilson(59, 60)[0]
    assert task.wilson(58, 60)[0] < 0.90


def test_compiled_selects_by_reference_raw_gives_everything(run1b):
    for p in run1b["payouts"]:
        raw, comp = task.decisions_raw(p), task.decisions_compiled(p)
        assert [d["id"] for d in raw] == [d["id"] for d in DECISIONS]
        ids = {d["id"] for d in comp}
        skus = {x.get("sku") for x in p["lines"]}
        assert "D-105" in ids  # the global rule
        assert ("D-101" in ids) == ("PRO" in skus) and ("D-102" in ids) == ("ENT" in skus)
        assert ("D-103" in ids) == (p["seller_id"] == "S-114") and ("D-104" in ids) == (p["seller_id"] == "S-207")
        # never by the answer: the selection is the same whatever the truth says
        q = copy.deepcopy(p)
        q["amount_cents"] += 100
        assert task.decisions_compiled(q) == comp


def test_both_arms_share_prompt_and_budget(run1b):
    p = run1b["payouts"][0]
    r, c = (task.input_line_1b(p, run1b["paid_invoices"], arm) for arm in task.ARMS)
    assert r["item_id"] == f"{p['payout_id']}/raw" and c["item_id"] == f"{p['payout_id']}/compiled"
    assert r["messages"][0] == c["messages"][0] and r["params"] == c["params"]
    assert r["params"]["max_tokens"] == task.MAX_TOKENS_1B
    assert "Hold every payout to S-114" in r["messages"][1]["content"]
    if p["seller_id"] != "S-114":
        assert "S-114" not in c["messages"][1]["content"]


def test_jobdef_keeps_schema_order_and_20_min_deadline(run1b):
    lines = [task.input_line_1b(p, run1b["paid_invoices"], arm) for p in run1b["payouts"] for arm in task.ARMS]
    jd, raw, cid = build_batch(lines, "t", "batch")
    env = jd["ops"][0]["args"]["env"]
    schema = json.loads(base64.b64decode(env["RELAY_OUTPUT_SCHEMA_B64"]))
    assert list(schema["properties"]) == ["reasoning", "decision", "reason_code"]
    assert env["RELAY_DEADLINE_S"] == "1200" and env["RELAY_OUTPUT_KEYS"] == "decision,reason_code"
    assert jd["meta"]["relay"]["phase"] == "1b" and cid == cidv0_of_bytes(raw)


def test_reasoning_is_not_hashed():
    inputs = [{"item_id": "P-1/compiled", "x": 1}, {"item_id": "P-2/compiled", "x": 2}]
    a = [json.dumps({"reasoning": "short", "decision": "hold", "reason_code": "duplicate"}),
         json.dumps({"reasoning": "ok", "decision": "approve", "reason_code": "ok"})]
    b = [json.dumps({"reasoning": "a different, much longer explanation " * 30, "decision": "hold",
                     "reason_code": "duplicate"}), a[1]]
    keys = task.OUTPUT_KEYS_1B
    ra, root_a = runner.build_outputs(inputs, a, task.OUTPUT_SCHEMA_1B, keys)
    rb, root_b = runner.build_outputs(inputs, b, task.OUTPUT_SCHEMA_1B, keys)
    assert root_a == root_b and ra[0]["output"] == {"decision": "hold", "reason_code": "duplicate"}
    assert rb[0]["output_raw_truncated"] == len(b[0]) and len(rb[0]["output_raw"]) == runner.RAW_MAX
    bad = [json.dumps({"decision": "hold", "reason_code": "duplicate"}), a[1]]  # reasoning missing
    rc, _ = runner.build_outputs(inputs, bad, task.OUTPUT_SCHEMA_1B, keys)
    assert rc[0]["output"] == {"parse_error": True}
    rm, _ = runner.build_outputs(inputs, a, task.OUTPUT_SCHEMA_1B, keys,
                                 [{"tokens_in": 9, "tokens_out": 3, "finish": "stop"}] * 2)
    assert rm[0]["tokens_in"] == 9 and rm[0]["output_sha256"] == ra[0]["output_sha256"]


def test_multiblock_cid_matches_pinata():
    fx = json.loads((ROOT / "tests" / "fixtures" / "phase1b" / "multiblock_cid_pinata.json").read_text())
    data = (ROOT / "runs" / "payouts-2026-11-10b" / "inputs.jsonl").read_bytes()
    assert hashlib.sha256(data).hexdigest() == fx["sha256"] and len(data) > 262144
    assert cidv0_of_bytes(data) == fx["pinata_IpfsHash"]
    assert cidv0_of_bytes(b"hello world") == "Qmf412jQZiuVUtdgnB36FXFX7xg5V6KEbSJ4dpQuhkLyfD"


def _ledger(tmp, rows):
    d = tmp / "runs" / "r1" / "ledger"
    d.mkdir(parents=True)
    (d / "jobs.json").write_text(json.dumps(rows))
    (tmp / "tests" / "fixtures" / "phase0").mkdir(parents=True)


def test_known_nodes_from_outcomes(tmp_path):
    m = "M" * 32
    row = lambda node, ok, err=None, load=None, ts=0: {"market": m, "node": node, "ok": ok, "error": err,
                                                      "load_s": load, "ts": ts, "tag": "batch", "attempt": 1}
    _ledger(tmp_path, [row("good_fast", 1, load=100, ts=1), row("good_slow", 1, load=900, ts=2),
                       row("hung", 0, "TimeoutError: still running after 3300s", ts=3),
                       row("broken", 0, "exit code 1", ts=4), row("broken", 0, "exit code 1", ts=5),
                       row("once", 0, "exit code 1", ts=6),
                       row("flaky", 1, load=50, ts=7), row("flaky", 0, "TimeoutError", ts=8)])
    k = nodes_mod.known_nodes(m, tmp_path)
    assert k["good"] == ["good_fast", "good_slow"]
    assert k["bad"] == ["broken", "flaky", "hung"]
    assert nodes_mod.pick(m, ["good_slow", "x"], set(), tmp_path)[0] == "good_slow"
    assert nodes_mod.pick(m, ["good_fast"], {"good_fast"}, tmp_path)[0] is None  # open market


def test_sign_test():
    assert sign_test(0, 0) == 1.0
    assert sign_test(10, 0) == pytest.approx(2 / 1024)
    assert sign_test(3, 3) == 1.0
