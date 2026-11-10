"""Phase 1: JCS, Merkle, the batch runner, receipts v0.1 and relay-verify (offline, fake chain/IPFS)."""

import copy
import gzip
import importlib.util
import json
import random
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from relay_nosana import rules as rules_mod
from relay_nosana.ledger import init_db
from relay_nosana.nosana import jobdef as jd_mod
from relay_nosana.nosana.jobs import Jobs
from relay_nosana.payouts import task
from relay_nosana.payouts.data import PLAN, generate, truth
from relay_nosana.receipts import issue
from relay_verify import merkle
from relay_verify.checks import Unavailable, receipt_id_of, verdict, verify
from relay_verify.cid import cidv0_of_bytes
from relay_verify.jcs import canonical, sha256_hex
from relay_verify.schema import validate
from relay_verify.solana import JobAccount

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("relay_batch_runner", ROOT / "batch_runner" / "run.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


# ---------------------------------------------------------------- JCS

def test_jcs_rfc8785_key_order_and_numbers():
    obj = {"€": 1, "\r": 2, "דּ": 3, "1": 4, "\U0001F600": 5, "\u0080": 6, "ö": 7}
    keys = list(json.loads(canonical(obj)).keys())
    assert keys == ["\r", "1", "\u0080", "ö", "€", "\U0001F600", "דּ"]  # RFC 8785 3.2.3
    assert canonical({"b": [1, 2.5, "x"], "a": None}) == b'{"a":null,"b":[1,2.5,"x"]}'
    nums = [0.0, -0.0, 1.0, 1e21, 1e-7, 0.000001, 123456789012.5, -1.5e-10, 1e20, 4.5]
    assert [canonical(x).decode() for x in nums] == \
        ["0", "0", "1", "1e+21", "1e-7", "0.000001", "123456789012.5", "-1.5e-10", "100000000000000000000", "4.5"]
    assert canonical("a\"b\\c\né") == '"a\\"b\\\\c\\né"'.encode()
    with pytest.raises(ValueError):
        canonical(2**60)


def test_jcs_stable_across_key_order_and_whitespace():
    a = json.loads('{"x": 1, "y": {"b": 2, "a": [3, {"d": 4, "c": 5}]}}')
    b = json.loads('{"y":{"a":[3,{"c":5,"d":4}],"b":2},"x":1}')
    assert sha256_hex(a) == sha256_hex(b)


def test_runner_copy_matches_relay_verify():
    rng = random.Random(1)
    for _ in range(50):
        obj = {"k" + str(rng.randint(0, 99)): [rng.random() * 10 ** rng.randint(-8, 22), rng.randint(-10**9, 10**9),
                                               "sé\n", None, True] for _ in range(4)}
        assert runner.canonical(obj) == canonical(obj)
    for n in range(1, 12):
        rows = [{"item_id": f"P-{i:04d}", "input_sha256": sha256_hex({"i": i}), "output_sha256": sha256_hex({"o": i})}
                for i in range(n)]
        random.Random(n).shuffle(rows)
        assert runner.merkle_root(rows) == merkle.root(rows)


# ---------------------------------------------------------------- Merkle

def _rows(n):
    return [{"item_id": f"P-{i:04d}", "input_sha256": sha256_hex({"in": i}), "output_sha256": sha256_hex({"out": i})}
            for i in range(n)]


def test_merkle_known_vector_and_single_leaf():
    rows = _rows(1)
    assert merkle.root(rows) == merkle.leaf_hash("P-0000", rows[0]["input_sha256"], rows[0]["output_sha256"]).hex()
    r3 = _rows(3)
    l = [merkle.leaf_hash(r["item_id"], r["input_sha256"], r["output_sha256"]) for r in r3]
    assert merkle.root(r3) == merkle.node_hash(merkle.node_hash(l[0], l[1]), l[2]).hex()  # odd node promoted


@pytest.mark.parametrize("n", [1, 2, 3, 5, 8, 21, 30])
def test_merkle_paths_verify_and_tamper_fails(n):
    rows = _rows(n)
    root = merkle.root(rows)
    for r in rows:
        leaf = merkle.leaf_hash(r["item_id"], r["input_sha256"], r["output_sha256"])
        p = merkle.path(rows, r["item_id"])
        assert merkle.root_from_path(leaf, p) == root
        bad = merkle.leaf_hash(r["item_id"], r["input_sha256"], sha256_hex({"tampered": 1}))
        assert merkle.root_from_path(bad, p) != root
        if p:
            p2 = copy.deepcopy(p)
            p2[0]["hash"] = "00" * 32
            assert merkle.root_from_path(leaf, p2) != root


# ---------------------------------------------------------------- payout data, rules, job definition

def test_payout_run_is_deterministic_with_one_known_answer_each():
    a, b = generate(), generate()
    assert canonical(a) == canonical(b)
    assert len(a["payouts"]) == len(PLAN) == 30
    for p in a["payouts"]:
        assert truth(p, a["paid_invoices"]) == a["truth"][p["payout_id"]]
        assert p["amount_cents"] > 0
    reasons = [t["reason_code"] for t in a["truth"].values()]
    assert reasons.count("ok") == 13 and len(set(reasons)) == 6


def test_compiler_selects_by_relevance_not_by_answer():
    run = generate()
    for p in run["payouts"]:
        kinds = [d["id"] for d in task.decisions_for(p)]
        assert "D-103" in kinds and "D-104" in kinds  # every hold is always shown, held seller or not
        assert ("D-105" in kinds) == any(x["type"] == "refund" for x in p["lines"])


def test_rules_threshold_and_rerun_sample():
    rules = rules_mod.load()
    assert rules_mod.rule_for(rules, "payout_check", 500.01)["id"] == "payouts-need-proof"
    assert rules_mod.rule_for(rules, "payout_check", 500.0)["id"] == "payouts-default"
    proof = rules[0]
    items = [(f"P-{i}", 600.0 + i * 300) for i in range(21)]  # several above $5000
    s = rules_mod.rerun_sample(proof, items, "run-x")
    assert len(s) == 15 and all(i in s for i, amt in items if amt > 5000)
    assert s == rules_mod.rerun_sample(proof, items, "run-x")
    assert proof["bar"] == {"metric": "accuracy_vs_ground_truth", "ci_lower_gte": 0.90}
    assert rules_mod.bar_cleared(proof, 0.90) is True
    assert rules_mod.bar_cleared(proof, 0.8999) is False


def test_jobdef_embeds_inputs_and_fits_one_block():
    run = generate()
    lines = [task.input_line(p, run["paid_invoices"]) for p in run["payouts"]]
    jd, raw, cid = jd_mod.build(lines, task.OUTPUT_SCHEMA, "t", "batch", task.TASK)
    env = jd["ops"][0]["args"]["env"]
    import base64
    assert gzip.decompress(base64.b64decode(env["RELAY_INPUTS_GZ_B64"])) == raw
    assert cidv0_of_bytes(raw) == cid == jd["meta"]["relay"]["inputs_cid"]
    assert len(canonical(jd)) < 262144
    assert "@sha256:" in jd["ops"][0]["args"]["image"]
    assert jd["ops"][0]["args"]["cmd"][0] == (ROOT / "batch_runner" / "run.py").read_text()
    assert not any(k in json.dumps(jd) for k in ("PINATA", "NOSANA_API", "ANTHROPIC", "eyJhbGci"))


def test_ledger_has_phase1_tables(tmp_path):
    conn = init_db(tmp_path / "l.sqlite")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "receipts", "rules", "calls"} <= names


def test_balance_is_usd_on_phase0_numbers():
    bal = {"assignedCredits": 10, "reservedCredits": 0.044, "settledCredits": 2.804}  # live, 2026-11-09
    assert Jobs.available_usd(bal) == pytest.approx(7.152)


@pytest.mark.asyncio
async def test_v1_client_reads_usd_not_cents():
    from relay_nosana.providers.nosana import NosanaClient
    with patch("httpx.AsyncClient") as mock_cls:
        resp = MagicMock()
        resp.json.return_value = {"assignedCredits": 10, "reservedCredits": 0, "settledCredits": 2.804}
        resp.raise_for_status = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=False)
        ctx.get = AsyncMock(return_value=resp)
        mock_cls.return_value = ctx
        assert await NosanaClient(api_key="k").available_credits() == pytest.approx(7.196)


# ---------------------------------------------------------------- receipts + verifier on a fake chain

MARKET, NODE, NODE2 = "CA5pMpqkYFKtme7K31pNB1s62X2SdhEv1nN9RdxKCpuQ", "2cwhCqyvGEiC2UbZdhctrh2LGud2qyx62ED5DkKmJ1ot", \
    "3hcXmBj7BoQpnrxYJpKqrf1cqD85JKeS7d2bZpTbUqrV"
JOB, JOB2 = "Hs7FRTa6EgiKxNdqaG7rnhpkSu8QpyYaceRrRsbGyN1z", "GaWjy48VRM4VdTeQk5H8gzFhr49qbN9HBmLFkK8GCQ42"


class FakeFetcher:
    def __init__(self):
        self.accounts, self.blobs = {}, {}

    def put(self, data: bytes) -> str:
        cid = cidv0_of_bytes(data)
        self.blobs[cid] = data
        return cid

    def account(self, address):
        return self.accounts.get(address)

    def ipfs(self, cid):
        if cid not in self.blobs:
            raise Unavailable(cid)
        return self.blobs[cid]


def _texts(lines, run):
    """Mock model: right answer except one item, to show receipts carry what the model said."""
    out = []
    for i, x in enumerate(lines):
        t = dict(run["truth"][x["item_id"]])
        if i == 3:
            t = {"decision": "approve", "reason_code": "ok"}
        out.append(json.dumps(t) if i != 7 else '{"decision": "hold", "reason_code": "stale_price"} ')
    return out


def _fake_job(f, lines, run, job=JOB, node=NODE, status="success"):
    jd, raw, inputs_cid = jd_mod.build(lines, task.OUTPUT_SCHEMA, "t", "batch", task.TASK)
    jd_cid = f.put(canonical(jd))
    rows, root = runner.build_outputs(lines, _texts(lines, run), task.OUTPUT_SCHEMA)
    logs = [{"type": "stderr", "log": "INFO noise\n"}]
    logs += [{"type": "stdout", "log": "RELAY_OUT " + runner.canonical(r).decode() + "\n"} for r in rows]
    summary = {"merkle_root": root, "count": len(rows), "model": jd_mod.MODEL, "revision": jd_mod.REVISION,
               "vllm_version": "0.10.2"}
    logs += [{"type": "stdout", "log": "RELAY_SUMMARY " + runner.canonical(summary).decode() + "\n"},
             {"type": "stdout", "log": f"RELAY_MERKLE_ROOT={root}\n"}]
    result = {"status": status, "startTime": 1794252057791, "endTime": 1794252240771, "opStates": [{
        "operationId": "relay-batch", "status": status, "exitCode": 0 if status == "success" else 1,
        "startTime": 1794252057815, "endTime": 1794252237383, "logs": logs,
        "results": {"merkle_root": [f"RELAY_MERKLE_ROOT={root}", root]}}]}
    res_cid = f.put(canonical(result))
    f.accounts[job] = JobAccount(address=job, ipfs_job=jd_cid, ipfs_result=res_cid, market=MARKET, node=node,
                                 payer="credFgXCGH3JFUT6WEKF6tJitxrXhSCM2VEqF7GE6pt", price=111,
                                 project="BgvjVh4dxEWMkk3cNaznZ7zY9wbx1U8FycehRmEjwipo", state=2,
                                 state_name="COMPLETED", time_end=1794252248, time_start=1794252052, timeout=3600)
    return jd_cid, inputs_cid, result


@pytest.fixture
def issued():
    run = generate()
    lines = [task.input_line(p, run["paid_invoices"]) for p in run["payouts"]]
    f = FakeFetcher()
    jd_cid, inputs_cid, result = _fake_job(f, lines, run)
    rule = rules_mod.load()[0]
    items = {x["item_id"]: x for x in lines[:21]}
    recs = issue.anchored_from_result(
        result=result, account=f.accounts[JOB].to_dict(), job_definition_cid=jd_cid, inputs_cid=inputs_cid,
        image=jd_mod.IMAGE, weights=f"{jd_mod.MODEL}@{jd_mod.REVISION}", items=items, rule=rule,
        rule_sha256=rules_mod.snapshot_sha256(rule), work_type="payout_check", relay_checks={},
        spot_checks={}, cost_usd_per_item=0.001)
    return f, recs, lines, run


def _ids_failing(r, f):
    return {c.id for c in verify(r, f) if c.ok is False}


def test_every_issued_receipt_verifies(issued):
    f, recs, _, _ = issued
    assert len(recs) == 21
    for r in recs.values():
        cks = verify(r, f)
        assert verdict(cks) == 0, [c for c in cks if c.ok is not True]


def test_receipt_carries_what_the_model_said_even_when_wrong(issued):
    f, recs, lines, run = issued
    wrong = recs[lines[3]["item_id"]]
    assert wrong["output"] == {"decision": "approve", "reason_code": "ok"}
    assert verdict(verify(wrong, f)) == 0


def _retag(r):
    r = copy.deepcopy(r)
    return r


def test_tamper_output_fails_check_5(issued):
    f, recs, _, _ = issued
    r = _retag(next(iter(recs.values())))
    r["output"] = {"decision": "approve" if r["output"]["decision"] == "hold" else "hold", "reason_code": "ok"}
    assert "5" in _ids_failing(r, f)
    r["output_sha256"] = sha256_hex(r["output"])  # recomputing the hash doesn't help
    r["receipt_id"] = receipt_id_of(r)
    assert "5" in _ids_failing(r, f)


def test_tamper_model_weights_fails_check_3(issued):
    f, recs, _, _ = issued
    r = _retag(next(iter(recs.values())))
    r["model"]["weights"] = "Qwen/Qwen2.5-7B-Instruct@0000000000000000000000000000000000000000"
    r["receipt_id"] = receipt_id_of(r)
    assert _ids_failing(r, f) == {"3"}


def test_tamper_job_to_another_real_job_fails_2_or_4(issued):
    f, recs, lines, run = issued
    _fake_job(f, lines[:5], run, job=JOB2)  # a different real job with different inputs
    r = _retag(recs[lines[10]["item_id"]])
    r["nosana"]["job"] = JOB2
    r["receipt_id"] = receipt_id_of(r)
    assert _ids_failing(r, f) & {"2", "4"}


def test_tamper_merkle_path_fails_check_5(issued):
    f, recs, _, _ = issued
    r = _retag(next(iter(recs.values())))
    r["merkle"]["path"][0]["hash"] = "ab" * 32
    r["receipt_id"] = receipt_id_of(r)
    assert _ids_failing(r, f) == {"5"}


def test_tamper_node_fails_check_6(issued):
    f, recs, _, _ = issued
    r = _retag(next(iter(recs.values())))
    r["nosana"]["node"] = NODE2
    r["receipt_id"] = receipt_id_of(r)
    assert _ids_failing(r, f) == {"6"}


def test_tamper_container_time_fails_check_6_and_any_edit_breaks_receipt_id(issued):
    f, recs, _, _ = issued
    r = _retag(next(iter(recs.values())))
    r["nosana"]["container_start"] += 1000
    assert {"6", "receipt_id"} <= _ids_failing(r, f)


def test_completed_job_with_failed_result_fails_check_1():
    run = generate()
    lines = [task.input_line(p, run["paid_invoices"]) for p in run["payouts"]][:4]
    f = FakeFetcher()
    jd_cid, inputs_cid, result = _fake_job(f, lines, run, status="failed")
    with pytest.raises(ValueError):
        issue.anchored_from_result(result=result, account=f.accounts[JOB].to_dict(), job_definition_cid=jd_cid,
                                   inputs_cid=inputs_cid, image=jd_mod.IMAGE, weights="m@r",
                                   items={x["item_id"]: x for x in lines}, rule=rules_mod.load()[0], rule_sha256="x",
                                   work_type="payout_check", relay_checks={}, spot_checks={}, cost_usd_per_item=0)


def test_network_unavailable_is_exit_2(issued):
    f, recs, _, _ = issued
    r = next(iter(recs.values()))
    f.blobs.clear()
    f.accounts.clear()
    assert verdict(verify(r, FakeFetcherDown())) == 2


class FakeFetcherDown:
    def account(self, address):
        raise Unavailable("rpc down")

    def ipfs(self, cid):
        raise Unavailable("gateway down")


def test_declared_receipt_schema_and_verdict():
    run = generate()
    line = task.input_line(run["payouts"][0], run["paid_invoices"])
    rule = rules_mod.load()[1]
    r = issue.declared(item_id=line["item_id"], input_line=line, output={"decision": "approve", "reason_code": "ok"},
                       provider={"name": "anthropic", "model": "claude-opus-5-5", "request_id": "req_x",
                                 "timestamp": "2026-11-10T00:00:00+00:00"},
                       rule=rule, rule_sha256=rules_mod.snapshot_sha256(rule), work_type="payout_check",
                       vendor="anthropic", relay_checks=["output_schema"], cost_usd=0.001)
    assert validate(r) == []
    cks = verify(r, FakeFetcher())
    assert verdict(cks) == 0 and cks[-1].id == "declared"
    bad = dict(r, nosana={})
    assert validate(bad)


def test_declared_receipt_from_claude_code_session():
    run = generate()
    line = task.input_line(run["payouts"][1], run["paid_invoices"])
    rule = rules_mod.load()[1]
    r = issue.declared(item_id=line["item_id"], input_line=line, output={"decision": "approve", "reason_code": "ok"},
                       provider={"name": "claude-code", "model": "claude-opus-5-5",
                                 "session_id": "fef32fac-07cc-4feb-bf03-adacc779f546",
                                 "timestamp": "2026-11-10T07:18:15Z"},
                       rule=rule, rule_sha256=rules_mod.snapshot_sha256(rule), work_type="payout_check",
                       vendor="claude-code", relay_checks=["output_schema"], cost_usd=0.0)
    assert validate(r) == []
    cks = verify(r, FakeFetcher())
    assert verdict(cks) == 0 and "session fef32fac" in cks[-1].reason
    no_id = copy.deepcopy(r)
    del no_id["provider"]["session_id"]
    assert "provider has neither request_id nor session_id" in validate(no_id)


def _cc_stdout(so, subtype="success", is_error=False):
    return json.dumps({"type": "result", "subtype": subtype, "is_error": is_error, "session_id": "s-1",
                       "result": json.dumps(so), "structured_output": so, "total_cost_usd": 0.02,
                       "usage": {"input_tokens": 2, "cache_creation_input_tokens": 100, "output_tokens": 9},
                       "modelUsage": {"claude-opus-5-5": {"canonicalModel": "claude-opus-5-5"}}})


@pytest.mark.parametrize("so,subtype,is_error,ok", [
    ({"decision": "hold", "reason_code": "duplicate"}, "success", False, True),
    ({"decision": "hold", "reason_code": "made_up"}, "success", False, False),   # off the enum
    ({"decision": "hold"}, "success", False, False),                              # missing key
    ({"decision": "hold", "reason_code": "duplicate"}, "error_max_turns", True, False),
])
def test_claude_code_lane_parses_and_checks_the_schema(so, subtype, is_error, ok):
    from relay_nosana.payouts import claude_code
    run = generate()
    line = task.input_line(run["payouts"][0], run["paid_invoices"])
    done = MagicMock(returncode=0, stdout=_cc_stdout(so, subtype, is_error), stderr="")
    with patch.object(claude_code.subprocess, "run", return_value=done) as m:
        rec = claude_code.call(line, task.OUTPUT_SCHEMA)
    cmd = m.call_args_list[-1].args[0]
    assert cmd[:2] == ["claude", "-p"] and "--json-schema" in cmd and cmd[cmd.index("--tools") + 1] == ""
    assert m.call_args_list[-1].kwargs["input"] == line["messages"][1]["content"]
    assert rec["session_id"] == "s-1" and rec["model"] == "claude-opus-5-5"
    assert (rec["output"] == so) is ok and (rec["error"] is None) is ok


def test_run_node_reads_the_node_from_the_run_account():
    import base64 as b64
    from relay_nosana import lane
    from relay_verify.b58 import b58decode
    job, node = "BmW33L4Ykw4EXfvC4f7U22rf2HDNESBR82tDRhEwdSo3", "38yH7ChdmMZJrC7TjL7WfZsVyj7cJ5iYiuLwidJnXx8J"
    data = bytes(8) + b58decode(job) + b58decode(node) + bytes(48)
    resp = {"result": {"value": {"data": [b64.b64encode(data).decode(), "base64"]}}}
    with patch.object(lane, "rpc_call", return_value=resp):
        assert lane.run_node("rpc", "run") == node
    with patch.object(lane, "rpc_call", return_value={"result": {"value": None}}):
        assert lane.run_node("rpc", "run") is None
