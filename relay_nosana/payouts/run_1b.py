"""Phase 1b: raw vs compiled context, one Nosana batch job, fresh seed (docs/phase1b-report.md).

    python -m relay_nosana.payouts.run_1b prepare --run-id R   # data, inputs (both arms), pre-registration
    python -m relay_nosana.payouts.run_1b nosana  --run-id R   # one batch job (both arms) + re-run (credits)
    python -m relay_nosana.payouts.run_1b issue   --run-id R   # score both arms, bar, routing, receipts
    python -m relay_nosana.payouts.run_poc verify --run-id R   # relay-verify every receipt (Phase 1 code)
    python -m relay_nosana.payouts.run_poc export --run-id R

Arms (same model, same prompt, same token budget; only the decisions in the context differ):
  raw       every decision in the ledger, for every payout
  compiled  decisions that reference this payout's seller_id or SKUs, plus the global rules
The model writes `reasoning` first; receipts hash only {decision, reason_code}.
Routing: the compiled arm if its Wilson 95% lower bound against ground truth is >= 0.90, else human review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

from relay_verify.jcs import canonical
from relay_verify.outputs import parse_outputs
from relay_verify.solana import DEFAULT_RPC

from .. import rules as rules_mod
from ..lane import Budget, log, run_batch
from ..ledger import log_call, upsert
from ..nosana import jobdef as jd_mod
from ..nosana import nodes as nodes_mod
from ..nosana.jobs import Jobs, idle_nodes
from ..receipts import issue
from . import task
from .data import PLAN_1B, SEED_1B, generate
from .run_poc import amounts, db, load_state, paths, save_state

PHASE1B_CAP_USD = 2.0
DEADLINE_S = 1200          # in-container deadline: 20 minutes
RUN_WAIT_S = DEADLINE_S + 300
MODEL_ID = f"nosana:{jd_mod.MODEL}@{jd_mod.REVISION[:12]}"


def build_batch(lines: list[dict], run_id: str, tag: str):
    return jd_mod.build(lines, task.OUTPUT_SCHEMA_1B, run_id, tag, task.TASK, phase="1b", keep_schema_order=True,
                        output_keys=task.OUTPUT_KEYS_1B, deadline_s=DEADLINE_S)


# ---------------------------------------------------------------- prepare + pre-registration

def cmd_prepare(a) -> None:
    P = paths(a.run_id)
    if (P["base"] / "preregistration.json").exists():
        sys.exit("already prepared: the pre-registration is fixed once written")
    P["base"].mkdir(parents=True, exist_ok=True)
    run = generate(SEED_1B, PLAN_1B)
    P["run"].write_text(json.dumps(run, indent=2, sort_keys=True) + "\n")
    lines = [task.input_line_1b(p, run["paid_invoices"], arm) for p in run["payouts"] for arm in task.ARMS]
    P["inputs"].write_bytes(jd_mod.inputs_bytes(lines))
    rules = rules_mod.load()
    conn = db(a.run_id)
    for r in rules:
        upsert(conn, "rules", {"snapshot_sha256": rules_mod.snapshot_sha256(r), "rule_id": r["id"],
                               "rule_json": canonical(r).decode(), "first_seen": time.time()}, "snapshot_sha256")
    conn.commit()
    amt = amounts(run)
    lanes = {pid: rules_mod.rule_for(rules, task.TASK, amt[pid])["id"] for pid in amt}
    jd, raw, inputs_cid = build_batch(lines, a.run_id, "batch")
    proof = next(r for r in rules if r["id"] == "payouts-need-proof")
    prereg = {
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": a.run_id, "phase": "1b", "data": "synthetic",
        "seed": SEED_1B, "payouts": len(run["payouts"]), "plan": "Phase 1 label mix x2",
        "why_60": "Wilson 95% lower bound for 30/30 is 0.886 < 0.90; at n = 60, 59/60 gives 0.911",
        "arms": {"raw": task.decisions_raw.__doc__.strip(), "compiled": task.decisions_compiled.__doc__.strip()},
        "model": f"{jd_mod.MODEL}@{jd_mod.REVISION}", "image": jd_mod.IMAGE,
        "params": {"temperature": 0, "seed": 0, "max_tokens": task.MAX_TOKENS_1B, "guided_json": True},
        "output_schema": task.OUTPUT_SCHEMA_1B, "hashed_output_keys": task.OUTPUT_KEYS_1B,
        "generation_key_order": list(task.OUTPUT_SCHEMA_1B["properties"]),  # this file sorts keys; the job doesn't
        "system_prompt_sha256": hashlib.sha256(task.SYSTEM_1B.encode()).hexdigest(),
        "inputs_cid": inputs_cid, "inputs_sha256": hashlib.sha256(raw).hexdigest(),
        "job_definition_cid": __import__("relay_verify.cid", fromlist=["x"]).cidv0_of_bytes(canonical(jd)),
        "scoring": "exact match of {decision, reason_code} against data.truth(); per arm over all 60 payouts, "
                   "Wilson 95% CI; also the > $500 subset; paired discordant counts with an exact two-sided "
                   "binomial (sign) test",
        "bar": proof["bar"], "rule_snapshot_sha256": rules_mod.snapshot_sha256(proof),
        "routing": "compiled arm if its CI lower bound >= 0.90, else human review for payouts > $500",
        "rerun": proof["rerun"], "rerun_arm": "compiled",
        "nodes": "known-good node list per market from measured outcomes (relay_nosana/nosana/nodes.py), "
                 "open market as fallback; in-container deadline 20 min",
        "cap_usd": PHASE1B_CAP_USD,
        "expectation": "compiled > raw (Phase 1: raw-style context, no reasoning, 4/30)",
    }
    (P["base"] / "preregistration.json").write_text(json.dumps(prereg, indent=2, sort_keys=True) + "\n")
    save_state(a.run_id, {"run_id": a.run_id, "phase": "1b", "lanes": lanes, "prepared_at": time.time()})
    n_anch = sum(1 for v in lanes.values() if v == "payouts-need-proof")
    log(f"prepared {len(run['payouts'])} payouts x {len(task.ARMS)} arms = {len(lines)} lines; {n_anch} over $500; "
        f"inputs {P['inputs'].stat().st_size} bytes; job definition {len(canonical(jd))} bytes, "
        f"CID {prereg['job_definition_cid']}")


# ---------------------------------------------------------------- nosana

def _tried(conn, run_id: str) -> set[str]:
    return {r["node"] for r in conn.execute("SELECT node FROM jobs WHERE run_id=? AND node IS NOT NULL", (run_id,))}


def cmd_nosana(a) -> None:
    P, state = paths(a.run_id), load_state(a.run_id)
    run = json.loads(P["run"].read_text())
    lines = [json.loads(x) for x in P["inputs"].read_text().splitlines()]
    by_id = {x["item_id"]: x for x in lines}
    conn = db(a.run_id)
    jobs = Jobs()
    budget = Budget(jobs, a.cap)
    if "budget_baseline" in state:
        budget.baseline = state["budget_baseline"]
    state["budget_baseline"] = budget.baseline
    state["known_nodes"] = {k: v for k, v in nodes_mod.known_nodes(a.market).items() if k != "table"}
    save_state(a.run_id, state)
    weights = f"{jd_mod.MODEL}@{jd_mod.REVISION}"

    def batch_node(attempt: int) -> str | None:
        node, why = nodes_mod.pick(a.market, idle_nodes(a.market, set()), _tried(conn, a.run_id))
        log(f"batch attempt {attempt}: {node or 'open market'} ({why})")
        return node

    if not (state.get("batch") or {}).get("ok"):
        jd, _, inputs_cid = build_batch(lines, a.run_id, "batch")
        out = run_batch(conn=conn, run_id=a.run_id, tag="batch", jobdef=jd, market=a.market, rpc=a.rpc, jobs=jobs,
                        budget=budget, fixtures=P["nosana"], image=jd_mod.IMAGE, weights=weights,
                        inputs_cid=inputs_cid, node_for_attempt=batch_node, max_attempts=a.max_attempts,
                        queue_wait_s=900, run_wait_s=RUN_WAIT_S)
        state["batch"] = {"ok": out.ok, "job": out.job, "jobdef_cid": out.jobdef_cid, "inputs_cid": inputs_cid,
                          "account": out.account, "attempts": [r["job"] for r in out.attempts],
                          "result_file": out.result_file}
        save_state(a.run_id, state)
        if not out.ok:
            sys.exit(f"batch failed after {len(out.attempts)} attempts; see runs/{a.run_id}/nosana/")

    if not (state.get("rerun") or {}).get("ok"):
        rule = next(r for r in rules_mod.load() if r["id"] == "payouts-need-proof")
        amt = amounts(run)
        anchored = [(pid, amt[pid]) for pid, lane in state["lanes"].items() if lane == rule["id"]]
        sample = rules_mod.rerun_sample(rule, anchored, a.run_id)
        sub = [by_id[f"{pid}/compiled"] for pid in sample]
        first = state["batch"]["account"]["node"]
        bad = set(nodes_mod.known_nodes(a.market)["bad"])

        def rerun_node(attempt: int) -> str | None:
            avoid = _tried(conn, a.run_id) | {first}
            idle = idle_nodes(a.market, set())
            node, why = nodes_mod.pick(a.market, idle, avoid)
            if node is None:  # the open market could hand the re-run to the batch's own node: target instead
                rest = [n for n in idle if n not in avoid and n not in bad]
                if not rest:
                    raise RuntimeError("no idle second node")
                node, why = rest[0], "no known-good node idle: an idle node not known bad"
            log(f"rerun attempt {attempt}: {node} ({why})")
            return node

        jd, _, inputs_cid = build_batch(sub, a.run_id, "rerun")
        out = run_batch(conn=conn, run_id=a.run_id, tag="rerun", jobdef=jd, market=a.market, rpc=a.rpc, jobs=jobs,
                        budget=budget, fixtures=P["nosana"], image=jd_mod.IMAGE, weights=weights,
                        inputs_cid=inputs_cid, node_for_attempt=rerun_node, max_attempts=a.max_attempts,
                        queue_wait_s=900, run_wait_s=RUN_WAIT_S)
        state["rerun"] = {"ok": out.ok, "job": out.job, "jobdef_cid": out.jobdef_cid, "inputs_cid": inputs_cid,
                          "account": out.account, "items": [f"{p}/compiled" for p in sample],
                          "attempts": [r["job"] for r in out.attempts], "result_file": out.result_file}
        save_state(a.run_id, state)
        if not out.ok:
            sys.exit(f"re-run failed after {len(out.attempts)} attempts")
    state["nosana_spent_usd"] = budget.spent()
    save_state(a.run_id, state)
    log(f"nosana done: batch {state['batch']['job']}, re-run {state['rerun']['job']}, "
        f"Phase 1b spend ${state['nosana_spent_usd']:.3f}")


# ---------------------------------------------------------------- issue: score, bar, routing, receipts

def sign_test(b: int, c: int) -> float:
    """Exact two-sided binomial test on discordant pairs (b vs c), p = 0.5."""
    n = b + c
    if n == 0:
        return 1.0
    from math import comb
    k = min(b, c)
    p = sum(comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def _words(row: dict) -> int | None:
    try:
        return len(json.loads(row["output_raw"]).get("reasoning", "").split())
    except Exception:  # noqa: BLE001 - truncated or unparsable
        return None


def cmd_issue(a) -> None:
    P, state = paths(a.run_id), load_state(a.run_id)
    run = json.loads(P["run"].read_text())
    truth, amt = run["truth"], amounts(run)
    lines = [json.loads(x) for x in P["inputs"].read_text().splitlines()]
    by_id = {x["item_id"]: x for x in lines}
    batch_res = json.loads((P["nosana"] / state["batch"]["result_file"]).read_text())
    rerun_res = json.loads((P["nosana"] / state["rerun"]["result_file"]).read_text())
    rows, _ = parse_outputs(batch_res)
    rerun_rows, _ = parse_outputs(rerun_res)
    pids = sorted(truth)
    conn = db(a.run_id)
    conn.execute("DELETE FROM calls WHERE run_id=?", (a.run_id,))
    cost = conn.execute("SELECT credits_before - credits_after c FROM jobs WHERE job=?",
                        (state["batch"]["job"],)).fetchone()["c"] or 0.0
    per_item = max(0.0, cost) / len(lines)
    rules = {r["id"]: r for r in rules_mod.load()}
    proof = rules["payouts-need-proof"]
    anchored = [pid for pid in pids if state["lanes"][pid] == proof["id"]]

    arms = {}
    for arm in task.ARMS:
        ok = {}
        for pid in pids:
            r = rows[f"{pid}/{arm}"]
            ok[pid] = task.correct(r["output"], truth[pid])
            log_call(conn, run_id=a.run_id, phase=task.TASK, task_type=task.TASK, instance_id=pid,
                     recipient_id=f"{MODEL_ID}:{arm}", mode="compiled" if arm == "compiled" else "raw",
                     tokens_in=r.get("tokens_in"), tokens_out=r.get("tokens_out"), cost_usd=per_item,
                     score=float(ok[pid]), parse_ok="parse_error" not in r["output"], error=None)
        k, n = sum(ok.values()), len(pids)
        lo, hi = task.wilson(k, n)
        ka = sum(ok[p] for p in anchored)
        alo, ahi = task.wilson(ka, len(anchored))
        rs = [rows[f"{p}/{arm}"] for p in pids]
        words = [w for w in (_words(r) for r in rs) if w is not None]
        by_reason: dict[str, list[int]] = {}
        for p in pids:
            by_reason.setdefault(truth[p]["reason_code"], [0, 0])
            by_reason[truth[p]["reason_code"]][0] += ok[p]
            by_reason[truth[p]["reason_code"]][1] += 1
        pred: dict[str, int] = {}
        for r in rs:
            key = "parse_error" if "parse_error" in r["output"] else f"{r['output']['decision']}/{r['output']['reason_code']}"
            pred[key] = pred.get(key, 0) + 1
        arms[arm] = {"n": n, "correct": k, "accuracy": round(k / n, 4), "ci_low": round(lo, 4), "ci_high": round(hi, 4),
                     "anchored": {"n": len(anchored), "correct": ka, "accuracy": round(ka / len(anchored), 4),
                                  "ci_low": round(alo, 4), "ci_high": round(ahi, 4)},
                     "by_true_reason": {k2: f"{v[0]}/{v[1]}" for k2, v in sorted(by_reason.items())},
                     "predicted": dict(sorted(pred.items())),
                     "parse_errors": sum("parse_error" in r["output"] for r in rs),
                     "finish_length": sum(r.get("finish") == "length" for r in rs),
                     "tokens_in_mean": round(sum(r.get("tokens_in") or 0 for r in rs) / n, 1),
                     "tokens_out_mean": round(sum(r.get("tokens_out") or 0 for r in rs) / n, 1),
                     "reasoning_words_median": sorted(words)[len(words) // 2] if words else None,
                     "reasoning_words_max": max(words) if words else None,
                     "_ok": ok}
    conn.commit()
    b = sum(arms["compiled"]["_ok"][p] and not arms["raw"]["_ok"][p] for p in pids)
    c = sum(arms["raw"]["_ok"][p] and not arms["compiled"]["_ok"][p] for p in pids)
    paired = {"compiled_right_raw_wrong": b, "raw_right_compiled_wrong": c, "sign_test_p": round(sign_test(b, c), 6)}
    for arm in arms.values():
        arm.pop("_ok")
    cleared = rules_mod.bar_cleared(proof, arms["compiled"]["ci_low"])
    raw_cleared = rules_mod.bar_cleared(proof, arms["raw"]["ci_low"])
    state["arms"], state["paired"] = arms, paired
    state["bar"] = {"rule": proof["id"], "metric": proof["bar"]["metric"], "needed_ci_low": proof["bar"]["ci_lower_gte"],
                    "compiled_ci_low": arms["compiled"]["ci_low"], "compiled_cleared": cleared,
                    "raw_ci_low": arms["raw"]["ci_low"], "raw_cleared": raw_cleared,
                    "frontier_ratio": "not measured in POC"}
    state["routing"] = ("compiled arm: payouts over $500 may be paid on a verified anchored receipt" if cleared
                        else "human review for payouts over $500 (on_no_open_model)")

    rr_job, rr_node = state["rerun"]["job"], state["rerun"]["account"]["node"]
    spots = {iid: {"rerun_job": rr_job, "rerun_node": rr_node,
                   "agreed": rerun_rows[iid]["output_sha256"] == rows[iid]["output_sha256"]}
             for iid in state["rerun"]["items"]}
    state["rerun_agreement"] = {"n": len(spots), "agreed": sum(s["agreed"] for s in spots.values())}

    items = {f"{pid}/compiled": by_id[f"{pid}/compiled"] for pid in anchored}
    checks = {}
    for iid in items:
        cks = []
        if "parse_error" not in rows[iid]["output"]:
            cks.append("output_schema")
        if cleared:
            cks.append("work_type_cleared_bar")
        if iid in spots and spots[iid]["agreed"]:
            cks.append("spot_check_agreed")
        checks[iid] = cks
    receipts = issue.anchored_from_result(
        result=batch_res, account=state["batch"]["account"], job_definition_cid=state["batch"]["jobdef_cid"],
        inputs_cid=state["batch"]["inputs_cid"], image=jd_mod.IMAGE, weights=f"{jd_mod.MODEL}@{jd_mod.REVISION}",
        items=items, rule=proof, rule_sha256=rules_mod.snapshot_sha256(proof), work_type=task.TASK,
        relay_checks=checks, spot_checks=spots, cost_usd_per_item=per_item)
    P["receipts"].mkdir(parents=True, exist_ok=True)
    for old in P["receipts"].glob("*.json"):
        old.unlink()
    conn.execute("DELETE FROM receipts WHERE run_id=?", (a.run_id,))
    for iid, r in sorted(receipts.items()):
        (P["receipts"] / f"{iid.split('/')[0]}.json").write_text(json.dumps(r, indent=2, sort_keys=True) + "\n")
        upsert(conn, "receipts", {
            "receipt_id": r["receipt_id"], "run_id": a.run_id, "item_id": iid, "grade": r["grade"],
            "rule_id": r["rule"]["id"], "rule_sha256": r["rule"]["snapshot_sha256"], "job": r["nosana"]["job"],
            "output_sha256": r["output_sha256"], "merkle_root": r["merkle"]["root"], "verified_at": None,
            "verify_verdict": None, "spot_check_agreed": None if r["spot_check"]["agreed"] is None
            else int(r["spot_check"]["agreed"]), "receipt_json": canonical(r).decode()}, "receipt_id")
    conn.commit()
    state["receipts"] = {"anchored": len(receipts), "declared": 0}
    save_state(a.run_id, state)
    for arm in task.ARMS:
        x = arms[arm]
        log(f"{arm}: {x['correct']}/{x['n']} = {x['accuracy']} CI [{x['ci_low']}, {x['ci_high']}]; "
            f"> $500 {x['anchored']['correct']}/{x['anchored']['n']}; predicted {x['predicted']}")
    log(f"paired {paired}; bar compiled {cleared} (raw {raw_cleared}); routing: {state['routing']}; "
        f"re-run {state['rerun_agreement']}; receipts {state['receipts']}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["prepare", "nosana", "issue"])
    p.add_argument("--run-id", required=True)
    p.add_argument("--market", default=jd_mod.MARKET_3090)
    p.add_argument("--rpc", default=os.environ.get("SOLANA_RPC", DEFAULT_RPC))
    p.add_argument("--cap", type=float, default=PHASE1B_CAP_USD)
    p.add_argument("--max-attempts", type=int, default=4)
    a = p.parse_args(argv)
    {"prepare": cmd_prepare, "nosana": cmd_nosana, "issue": cmd_issue}[a.cmd](a)


if __name__ == "__main__":
    main()
