"""Phase 1b, steps 2-3: the Phase 1 payout batch, unchanged in design, on a fresh seed (docs/phase1b-report.md).

    python -m relay_nosana.payouts.run_fresh prepare --run-id R --model 7b|14b
    python -m relay_nosana.payouts.run_fresh nosana  --run-id R      # batch + the rule's re-run (credits)
    python -m relay_nosana.payouts.run_fresh issue   --run-id R      # accuracy vs truth, bar, receipts
    python -m relay_nosana.payouts.run_poc   verify  --run-id R
    python -m relay_nosana.payouts.run_poc   export  --run-id R

Same task as Phase 1 and docs/POC_PAYOUTS.md: 30 payouts (PLAN), the Phase 1 prompt, schema and compiler
(task.input_line / decisions_for), temperature 0, guided JSON, 48 tokens. Only the seed and, for step 3,
the model change. Known-good nodes first (nodes.py), in-container deadline 20 minutes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from relay_verify.cid import cidv0_of_bytes
from relay_verify.jcs import canonical
from relay_verify.outputs import parse_outputs
from relay_verify.solana import DEFAULT_RPC

from .. import rules as rules_mod
from ..lane import Budget, chain_for, log, run_batch
from ..ledger import log_call, upsert
from ..nosana import jobdef as jd_mod
from ..nosana import nodes as nodes_mod
from ..nosana.jobs import Jobs, idle_nodes
from ..receipts import issue
from . import task
from .data import PLAN, generate
from .run_poc import amounts, db, load_state, paths, save_state

SEED_FRESH = 20261111  # never used before (Phase 1: 20261109, two-arm run: 20261110)
CAP_USD = 2.0          # Phase 1b, all jobs, from the baseline below
PHASE1B_BASELINE = 3.405
DEADLINE_S = 1200
MODELS = {
    "7b": (jd_mod.MODEL, jd_mod.REVISION),
    "14b": ("Qwen/Qwen2.5-14B-Instruct-AWQ", "539535859b135b0244c91f3e59816150c8056698"),
}


def _model(state: dict) -> tuple[str, str]:
    return MODELS[state["model_key"]]


def build(lines, run_id, tag, model, revision):
    return jd_mod.build(lines, task.OUTPUT_SCHEMA, run_id, tag, task.TASK, model=model, revision=revision,
                        phase="1b", deadline_s=DEADLINE_S)


def cmd_prepare(a) -> None:
    P = paths(a.run_id)
    if P["state"].exists():
        sys.exit("already prepared")
    P["base"].mkdir(parents=True, exist_ok=True)
    run = generate(SEED_FRESH, PLAN)
    P["run"].write_text(json.dumps(run, indent=2, sort_keys=True) + "\n")
    lines = [task.input_line(p, run["paid_invoices"]) for p in run["payouts"]]
    P["inputs"].write_bytes(jd_mod.inputs_bytes(lines))
    rules = rules_mod.load()
    conn = db(a.run_id)
    for r in rules:
        upsert(conn, "rules", {"snapshot_sha256": rules_mod.snapshot_sha256(r), "rule_id": r["id"],
                               "rule_json": canonical(r).decode(), "first_seen": time.time()}, "snapshot_sha256")
    conn.commit()
    amt = amounts(run)
    lanes = {pid: rules_mod.rule_for(rules, task.TASK, amt[pid])["id"] for pid in amt}
    model, rev = MODELS[a.model]
    jd, raw, inputs_cid = build(lines, a.run_id, "batch", model, rev)
    plan = {"written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "run_id": a.run_id, "phase": "1b",
            "step": {"7b": "2: Qwen-7B after the diagnosis", "14b": "3: Qwen-14B-AWQ, 7B missed the bar"}[a.model],
            "seed": SEED_FRESH, "payouts": len(lines), "design": "Phase 1 / docs/POC_PAYOUTS.md, unchanged",
            "model": f"{model}@{rev}", "image": jd_mod.IMAGE, "inputs_cid": inputs_cid,
            "job_definition_cid": cidv0_of_bytes(canonical(jd)),
            "bar": next(r for r in rules if r["id"] == "payouts-need-proof")["bar"],
            "bar_note": "at n = 30 the Wilson 95% lower bound is 0.886 even for 30/30, so the bar can't be "
                        "cleared at this size; 35/35 is the smallest run that can clear it",
            "cap_usd": CAP_USD, "deadline_s": DEADLINE_S}
    (P["base"] / "plan.json").write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    save_state(a.run_id, {"run_id": a.run_id, "model_key": a.model, "lanes": lanes, "prepared_at": time.time()})
    log(f"prepared {len(lines)} payouts, seed {SEED_FRESH}, {a.model} {model}@{rev[:12]}; "
        f"job definition {len(canonical(jd))} bytes, CID {plan['job_definition_cid']}")


def _tried(conn, run_id):
    return {r["node"] for r in conn.execute("SELECT node FROM jobs WHERE run_id=? AND node IS NOT NULL", (run_id,))}


def cmd_nosana(a) -> None:
    P, state = paths(a.run_id), load_state(a.run_id)
    run = json.loads(P["run"].read_text())
    lines = [json.loads(x) for x in P["inputs"].read_text().splitlines()]
    by_id = {x["item_id"]: x for x in lines}
    model, rev = _model(state)
    weights = f"{model}@{rev}"
    conn = db(a.run_id)
    jobs = Jobs()
    budget = Budget(jobs, a.cap)
    budget.baseline = PHASE1B_BASELINE  # the cap is for all of Phase 1b
    bad = set(nodes_mod.known_nodes(a.market)["bad"])

    def node_for(first: str | None):
        def f(attempt: int):
            avoid = _tried(conn, a.run_id) | ({first} if first else set())
            idle = idle_nodes(a.market, set())
            node, why = nodes_mod.pick(a.market, idle, avoid)
            if node is None and first:  # a re-run must not land on the batch's node: target an idle one
                rest = [n for n in idle if n not in avoid and n not in bad]
                if not rest:
                    raise RuntimeError("no idle second node")
                node, why = rest[0], "no known-good node idle: an idle node not known bad"
            log(f"{'rerun' if first else 'batch'} attempt {attempt}: {node or 'open market'} ({why})")
            return node
        return f

    if not (state.get("batch") or {}).get("ok"):
        jd, _, inputs_cid = build(lines, a.run_id, "batch", model, rev)
        out = run_batch(conn=conn, run_id=a.run_id, tag="batch", jobdef=jd, market=a.market, rpc=a.rpc, jobs=jobs,
                        budget=budget, fixtures=P["nosana"], image=jd_mod.IMAGE, weights=weights,
                        inputs_cid=inputs_cid, node_for_attempt=node_for(None), max_attempts=a.max_attempts,
                        queue_wait_s=900, run_wait_s=DEADLINE_S + 300)
        state["batch"] = {"ok": out.ok, "job": out.job, "jobdef_cid": out.jobdef_cid, "inputs_cid": inputs_cid,
                          "account": out.account, "attempts": [r["job"] for r in out.attempts],
                          "result_file": out.result_file}
        save_state(a.run_id, state)
        if not out.ok:
            sys.exit(f"batch failed after {len(out.attempts)} attempts")

    if not (state.get("rerun") or {}).get("ok") and not a.batch_only:
        rule = next(r for r in rules_mod.load() if r["id"] == "payouts-need-proof")
        amt = amounts(run)
        anchored = [(pid, amt[pid]) for pid, lane in state["lanes"].items() if lane == rule["id"]]
        sample = rules_mod.rerun_sample(rule, anchored, a.run_id)
        jd, _, inputs_cid = build([by_id[i] for i in sample], a.run_id, "rerun", model, rev)
        out = run_batch(conn=conn, run_id=a.run_id, tag="rerun", jobdef=jd, market=a.market, rpc=a.rpc, jobs=jobs,
                        budget=budget, fixtures=P["nosana"], image=jd_mod.IMAGE, weights=weights,
                        inputs_cid=inputs_cid, node_for_attempt=node_for(state["batch"]["account"]["node"]),
                        max_attempts=a.max_attempts, queue_wait_s=900, run_wait_s=DEADLINE_S + 300)
        state["rerun"] = {"ok": out.ok, "job": out.job, "jobdef_cid": out.jobdef_cid, "inputs_cid": inputs_cid,
                          "account": out.account, "items": sample, "attempts": [r["job"] for r in out.attempts],
                          "result_file": out.result_file}
        save_state(a.run_id, state)
        if not out.ok:
            sys.exit(f"re-run failed after {len(out.attempts)} attempts")
    state["phase1b_spent_usd"] = budget.spent()
    save_state(a.run_id, state)
    log(f"nosana done: batch {state['batch']['job']}, re-run {(state.get('rerun') or {}).get('job')}; "
        f"Phase 1b spend ${state['phase1b_spent_usd']:.3f}")


def cmd_issue(a) -> None:
    P, state = paths(a.run_id), load_state(a.run_id)
    run = json.loads(P["run"].read_text())
    truth, amt = run["truth"], amounts(run)
    lines = [json.loads(x) for x in P["inputs"].read_text().splitlines()]
    by_id = {x["item_id"]: x for x in lines}
    model, rev = _model(state)
    batch_res = json.loads((P["nosana"] / state["batch"]["result_file"]).read_text())
    rows, _ = parse_outputs(batch_res)
    rr = state.get("rerun") or {}
    rerun_rows = parse_outputs(json.loads((P["nosana"] / rr["result_file"]).read_text()))[0] if rr.get("ok") else {}
    conn = db(a.run_id)
    conn.execute("DELETE FROM calls WHERE run_id=?", (a.run_id,))
    c = conn.execute("SELECT credits_before - credits_after c FROM jobs WHERE job=?", (state["batch"]["job"],)).fetchone()
    per_item = max(0.0, (c["c"] if c else 0.0) or 0.0) / len(lines)
    rules = {r["id"]: r for r in rules_mod.load()}
    proof = rules["payouts-need-proof"]
    pids = sorted(truth)
    anchored = [p for p in pids if state["lanes"][p] == proof["id"]]
    rid = f"nosana:{model}@{rev[:12]}"
    ok = {}
    for pid in pids:
        o = rows[pid]["output"]
        ok[pid] = task.correct(o, truth[pid])
        log_call(conn, run_id=a.run_id, phase=task.TASK, task_type=task.TASK, instance_id=pid, recipient_id=rid,
                 mode="compiled", cost_usd=per_item, score=float(ok[pid]), parse_ok="parse_error" not in o, error=None)
    conn.commit()
    k, n = sum(ok.values()), len(pids)
    lo, hi = task.wilson(k, n)
    ka = sum(ok[p] for p in anchored)
    alo, ahi = task.wilson(ka, len(anchored))
    pred: dict[str, int] = {}
    for pid in pids:
        o = rows[pid]["output"]
        key = "parse_error" if "parse_error" in o else f"{o['decision']}/{o['reason_code']}"
        pred[key] = pred.get(key, 0) + 1
    cleared = rules_mod.bar_cleared(proof, round(lo, 4))
    state["accuracy"] = {"model": rid, "n": n, "correct": k, "accuracy": round(k / n, 4), "ci_low": round(lo, 4),
                         "ci_high": round(hi, 4), "anchored": {"n": len(anchored), "correct": ka,
                                                              "ci_low": round(alo, 4), "ci_high": round(ahi, 4)},
                         "predicted": dict(sorted(pred.items())),
                         "wrong": {p: {"said": rows[p]["output"], "truth": truth[p]} for p in pids if not ok[p]}}
    state["bar"] = {"rule": proof["id"], "cleared": cleared, "ci_low": round(lo, 4),
                    "needed": proof["bar"]["ci_lower_gte"], "max_ci_low_at_this_n": round(task.wilson(n, n)[0], 4),
                    "frontier_ratio": "not measured in POC"}
    chains = {"batch": chain_for(job=state["batch"]["job"], tag="batch", fixtures=P["nosana"], rpc=a.rpc)}
    if rr.get("ok"):
        chains["rerun"] = chain_for(job=rr["job"], tag="rerun", fixtures=P["nosana"], rpc=a.rpc)
    state["chains"] = chains
    spots = {pid: issue.spot_check(rerun_job=rr["job"], rerun_node=chains["rerun"]["node"],
                                   output_sha256=rows[pid]["output_sha256"],
                                   rerun_output_sha256=rerun_rows[pid]["output_sha256"])
             for pid in rr.get("items", [])} if rr.get("ok") else {}
    state["rerun_agreement"] = ({"n": len(spots), "agreed": sum(s["agreed"] for s in spots.values())} if spots
                                else "not run (step 2 baseline: batch only)")
    checks = {}
    for pid in anchored:
        cks = [] if "parse_error" in rows[pid]["output"] else ["output_schema"]
        if cleared:
            cks.append("work_type_cleared_bar")
        if pid in spots and spots[pid]["agreed"]:
            cks.append("spot_check_agreed")
        checks[pid] = cks
    receipts = issue.anchored_from_result(
        result=batch_res, chain=chains["batch"], job_definition_cid=state["batch"]["jobdef_cid"],
        inputs_cid=state["batch"]["inputs_cid"], image=jd_mod.IMAGE, weights=f"{model}@{rev}",
        items={p: by_id[p] for p in anchored}, rule=proof, rule_sha256=rules_mod.snapshot_sha256(proof),
        work_type=task.TASK, relay_checks=checks, spot_checks=spots, cost_usd_per_item=per_item)
    P["receipts"].mkdir(parents=True, exist_ok=True)
    for old in P["receipts"].glob("*.json"):
        old.unlink()
    conn.execute("DELETE FROM receipts WHERE run_id=?", (a.run_id,))
    for pid, r in sorted(receipts.items()):
        (P["receipts"] / f"{pid}.json").write_text(json.dumps(r, indent=2, sort_keys=True) + "\n")
        upsert(conn, "receipts", {
            "receipt_id": r["receipt_id"], "run_id": a.run_id, "item_id": pid, "grade": r["grade"],
            "rule_id": r["rule"]["id"], "rule_sha256": r["rule"]["snapshot_sha256"], "job": r["nosana"]["job"],
            "output_sha256": r["output_sha256"], "merkle_root": r["merkle"]["root"], "verified_at": None,
            "verify_verdict": None, "spot_check_agreed": None if r["spot_check"]["agreed"] is None
            else int(r["spot_check"]["agreed"]), "receipt_json": canonical(r).decode()}, "receipt_id")
    conn.commit()
    state["receipts"] = {"anchored": len(receipts), "declared": 0}
    state["routing"] = ("payouts over $500 may be paid on a verified anchored receipt" if cleared
                        else "human review for payouts over $500 (on_no_open_model)")
    save_state(a.run_id, state)
    log(f"{rid}: {k}/{n} = {k / n:.3f} CI [{lo:.3f}, {hi:.3f}]; > $500 {ka}/{len(anchored)}; predicted {pred}")
    log(f"bar cleared: {cleared} (max possible CI low at n={n}: {task.wilson(n, n)[0]:.3f}); "
        f"re-run {state['rerun_agreement']}; receipts {state['receipts']}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["prepare", "nosana", "issue"])
    p.add_argument("--run-id", required=True)
    p.add_argument("--model", choices=sorted(MODELS), default="7b")
    p.add_argument("--market", default=jd_mod.MARKET_3090)
    p.add_argument("--rpc", default=os.environ.get("SOLANA_RPC", DEFAULT_RPC))
    p.add_argument("--cap", type=float, default=CAP_USD)
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--batch-only", action="store_true", help="skip the rule's re-run")
    a = p.parse_args(argv)
    {"prepare": cmd_prepare, "nosana": cmd_nosana, "issue": cmd_issue}[a.cmd](a)


if __name__ == "__main__":
    main()
