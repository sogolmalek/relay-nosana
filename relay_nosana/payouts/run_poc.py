"""Phase 1 POC: one real payout run on Nosana with receipts (SPEC_V2 Phase 1, docs/POC_PAYOUTS.md).

    python -m relay_nosana.payouts.run_poc prepare  --run-id R
    python -m relay_nosana.payouts.run_poc nosana   --run-id R      # batch + risk-based re-run (credits)
    python -m relay_nosana.payouts.run_poc declared --run-id R      # headless Claude Code, payouts <= $500 only
    python -m relay_nosana.payouts.run_poc frontier --run-id R --api  # Claude API on all items (API credits;
                                                                      # off in the POC, kept for later)
    python -m relay_nosana.payouts.run_poc issue    --run-id R      # accuracy, bar, receipts
    python -m relay_nosana.payouts.run_poc verify   --run-id R      # relay-verify every receipt
    python -m relay_nosana.payouts.run_poc export   --run-id R      # ledger tables -> runs/R/ledger/*.json

Everything lands in runs/<run_id>/ (committed) and out/phase1/<run_id>.sqlite (the ledger, not committed;
`export` writes its tables as JSON). Keys come from env only.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from relay_verify.checks import Fetcher, verdict, verify
from relay_verify.jcs import canonical, sha256_hex
from relay_verify.outputs import parse_outputs
from relay_verify.solana import DEFAULT_RPC

from .. import gate
from .. import rules as rules_mod
from ..lane import Budget, chain_for, log, run_batch
from ..ledger import init_db, log_call, upsert
from ..nosana import jobdef as jd_mod
from ..nosana.jobs import Jobs, idle_nodes
from ..receipts import issue
from . import claude_code, frontier, task
from .data import generate, usd

ROOT = Path(__file__).resolve().parents[2]
PHASE1_CAP_USD = 3.0
OPEN_ID = f"nosana:{jd_mod.MODEL}@{jd_mod.REVISION[:12]}"
# Phase 0: this node downloaded and loaded the 7B weights in 160 s (another took 1,233 s)
FAST_NODES = ["2cwhCqyvGEiC2UbZdhctrh2LGud2qyx62ED5DkKmJ1ot"]


def paths(run_id: str) -> dict[str, Path]:
    base = ROOT / "runs" / run_id
    return {"base": base, "state": base / "state.json", "run": base / "payout_run.json",
            "inputs": base / "inputs.jsonl", "nosana": base / "nosana", "frontier": base / "frontier.jsonl",
            "declared": base / "declared.jsonl",
            "receipts": base / "receipts", "ledger_dir": base / "ledger",
            "db": ROOT / "out" / "phase1" / f"{run_id}.sqlite"}


def db(run_id: str) -> sqlite3.Connection:
    p = paths(run_id)["db"]
    p.parent.mkdir(parents=True, exist_ok=True)
    return init_db(p)


def load_state(run_id: str) -> dict:
    p = paths(run_id)["state"]
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(run_id: str, state: dict) -> None:
    paths(run_id)["state"].write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def amounts(run: dict) -> dict[str, float]:
    return {p["payout_id"]: p["amount_cents"] / 100 for p in run["payouts"]}


# ---------------------------------------------------------------- prepare

def cmd_prepare(a) -> None:
    P = paths(a.run_id)
    P["base"].mkdir(parents=True, exist_ok=True)
    run = generate()
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
    save_state(a.run_id, {"run_id": a.run_id, "lanes": lanes, "prepared_at": time.time()})
    n_anch = sum(1 for v in lanes.values() if v == "payouts-need-proof")
    log(f"prepared {len(lines)} payouts: {n_anch} need an anchored receipt (> $500), "
        f"{len(lines) - n_anch} take the declared lane; inputs.jsonl {P['inputs'].stat().st_size} bytes")


# ---------------------------------------------------------------- nosana: batch + re-run

def cmd_nosana(a) -> None:
    P, state = paths(a.run_id), load_state(a.run_id)
    run = json.loads(P["run"].read_text())
    lines = [json.loads(x) for x in P["inputs"].read_text().splitlines()]
    by_id = {x["item_id"]: x for x in lines}
    conn = db(a.run_id)
    jobs = Jobs()
    budget = Budget(jobs, a.cap)
    weights = f"{jd_mod.MODEL}@{jd_mod.REVISION}"
    if "budget_baseline" in state:  # the cap is for the whole run, across re-invocations
        budget.baseline = state["budget_baseline"]
    state["budget_baseline"] = budget.baseline
    save_state(a.run_id, state)

    # batch: every payout, so the open model is measured on the whole run
    if not (state.get("batch") or {}).get("ok"):
        def batch_node(attempt: int) -> str | None:
            """Retries after a hang: a Phase 0 fast node if idle, else an idle node that hasn't failed us
            in this run, else any node in the market."""
            bad = {r["node"] for r in conn.execute("SELECT node FROM jobs WHERE run_id=? AND ok=0 AND node IS NOT NULL",
                                                   (a.run_id,))} | set(a.avoid_node or [])
            idle = idle_nodes(a.market, bad)
            pick = next((n for n in FAST_NODES if n in idle), idle[0] if idle else None)
            log(f"batch attempt {attempt}: targeting {pick or 'any node'} (avoiding {sorted(bad)})")
            return pick

        jd, _, inputs_cid = jd_mod.build(lines, task.OUTPUT_SCHEMA, a.run_id, "batch", task.TASK)
        out = run_batch(conn=conn, run_id=a.run_id, tag="batch", jobdef=jd, market=a.market, rpc=a.rpc, jobs=jobs,
                        budget=budget, fixtures=P["nosana"], image=jd_mod.IMAGE, weights=weights,
                        inputs_cid=inputs_cid, max_attempts=a.max_attempts, node_for_attempt=batch_node)
        state["batch"] = {"ok": out.ok, "job": out.job, "jobdef_cid": out.jobdef_cid, "inputs_cid": inputs_cid,
                          "account": out.account, "attempts": [r["job"] for r in out.attempts],
                          "result_file": out.result_file}
        save_state(a.run_id, state)
        if not out.ok:
            sys.exit(f"batch failed after {len(out.attempts)} attempts; see runs/{a.run_id}/nosana/")

    # risk-based re-run of anchored items on a different node
    if not (state.get("rerun") or {}).get("ok"):
        rule = next(r for r in rules_mod.load() if r["id"] == "payouts-need-proof")
        amt = amounts(run)
        anchored = [(pid, amt[pid]) for pid, lane in state["lanes"].items() if lane == rule["id"]]
        sample = rules_mod.rerun_sample(rule, anchored, a.run_id)
        sub = [by_id[i] for i in sample]
        first_node = state["batch"]["account"]["node"]

        def node_for(attempt: int) -> str | None:
            """A second node: not the batch's node, and not one that failed or hung for us in this run."""
            bad = {r["node"] for r in conn.execute("SELECT node FROM jobs WHERE run_id=? AND ok=0 AND node IS NOT NULL",
                                                   (a.run_id,))} | set(a.avoid_node or [])
            cands = idle_nodes(a.market, {first_node} | bad)
            if not cands:
                raise RuntimeError("no idle second node found in the market")
            log(f"rerun attempt {attempt}: targeting {cands[0]} (avoiding {sorted(bad | {first_node})})")
            return cands[0]

        jd, _, inputs_cid = jd_mod.build(sub, task.OUTPUT_SCHEMA, a.run_id, "rerun", task.TASK)
        out = run_batch(conn=conn, run_id=a.run_id, tag="rerun", jobdef=jd, market=a.market, rpc=a.rpc, jobs=jobs,
                        budget=budget, fixtures=P["nosana"], image=jd_mod.IMAGE, weights=weights,
                        inputs_cid=inputs_cid, node_for_attempt=node_for, max_attempts=a.max_attempts,
                        queue_wait_s=900)
        state["rerun"] = {"ok": out.ok, "job": out.job, "jobdef_cid": out.jobdef_cid, "inputs_cid": inputs_cid,
                          "account": out.account, "items": sample, "attempts": [r["job"] for r in out.attempts],
                          "result_file": out.result_file}
        save_state(a.run_id, state)
        if not out.ok:
            sys.exit(f"re-run failed after {len(out.attempts)} attempts")
    state["nosana_spent_usd"] = budget.spent()
    save_state(a.run_id, state)
    log(f"nosana done: batch {state['batch']['job']}, re-run {state['rerun']['job']}, "
        f"Phase 1 spend so far ${state['nosana_spent_usd']:.3f}")


# ---------------------------------------------------------------- frontier

def cmd_frontier(a) -> None:
    if not a.api:
        sys.exit("the API frontier lane spends API credits and is off in the POC; pass --api to run it")
    import anthropic

    P = paths(a.run_id)
    lines = [json.loads(x) for x in P["inputs"].read_text().splitlines()]
    done = {}
    if P["frontier"].exists():
        for x in P["frontier"].read_text().splitlines():
            r = json.loads(x)
            if r.get("output") is not None:
                done[r["item_id"]] = r
    client = anthropic.Anthropic()
    with P["frontier"].open("w") as fh:
        for line in lines:
            rec = done.get(line["item_id"]) or frontier.call(client, line, task.OUTPUT_SCHEMA)
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            log(f"frontier {line['item_id']}: {rec.get('output')} {rec.get('error') or ''} "
                f"served_by={rec.get('served_model')}")
    recs = [json.loads(x) for x in P["frontier"].read_text().splitlines()]
    log(f"frontier: {sum(r.get('output') is not None for r in recs)}/{len(recs)} answered, "
        f"cost ${sum(r.get('cost_usd') or 0 for r in recs):.4f}")


# ---------------------------------------------------------------- declared lane: headless Claude Code

def cmd_declared(a) -> None:
    """Payouts the rules send to the declared lane (<= $500) only, one `claude -p` session each."""
    P, state = paths(a.run_id), load_state(a.run_id)
    lines = [json.loads(x) for x in P["inputs"].read_text().splitlines()]
    want = [x for x in lines if state["lanes"][x["item_id"]] == "payouts-default"]
    done = {}
    if P["declared"].exists():
        for x in P["declared"].read_text().splitlines():
            r = json.loads(x)
            if r.get("output") is not None:
                done[r["item_id"]] = r
    with P["declared"].open("w") as fh:
        for line in want:
            rec = done.get(line["item_id"]) or claude_code.call(line, task.OUTPUT_SCHEMA)
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            fh.flush()
            log(f"claude-code {line['item_id']}: {rec.get('output')} {rec.get('error') or ''} "
                f"model={rec.get('model')} session={rec.get('session_id')}")
    recs = [json.loads(x) for x in P["declared"].read_text().splitlines()]
    log(f"declared: {sum(r.get('output') is not None for r in recs)}/{len(recs)} answered; notional list cost "
        f"${sum(r.get('notional_cost_usd') or 0 for r in recs):.4f} (subscription login, not charged)")


# ---------------------------------------------------------------- issue: accuracy, bar, receipts

def cmd_issue(a) -> None:
    P, state = paths(a.run_id), load_state(a.run_id)
    run = json.loads(P["run"].read_text())
    truth = run["truth"]
    lines = [json.loads(x) for x in P["inputs"].read_text().splitlines()]
    by_id = {x["item_id"]: x for x in lines}
    batch_res = json.loads((P["nosana"] / state["batch"]["result_file"]).read_text())
    rerun_res = json.loads((P["nosana"] / state["rerun"]["result_file"]).read_text())
    open_rows, _ = parse_outputs(batch_res)
    rerun_rows, _ = parse_outputs(rerun_res)
    dec = {r["item_id"]: r for r in (json.loads(x) for x in P["declared"].read_text().splitlines())} \
        if P["declared"].exists() else {}
    fr = {r["item_id"]: r for r in (json.loads(x) for x in P["frontier"].read_text().splitlines())} \
        if P["frontier"].exists() else {}
    conn = db(a.run_id)
    conn.execute("DELETE FROM calls WHERE run_id=?", (a.run_id,))
    batch_cost = (state["batch"].get("cost_usd") or _job_cost(conn, state["batch"]["job"]))
    per_item = batch_cost / len(lines)
    for pid in by_id:
        o = open_rows[pid]["output"]
        log_call(conn, run_id=a.run_id, phase=task.TASK, task_type=task.TASK, instance_id=pid, recipient_id=OPEN_ID,
                 mode="compiled", cost_usd=per_item, score=float(task.correct(o, truth[pid])),
                 parse_ok="parse_error" not in o, error=None)
        if pid in dec:  # declared lane: the <= $500 payouts only
            d = dec[pid]
            log_call(conn, run_id=a.run_id, phase=task.TASK, task_type=task.TASK, instance_id=pid,
                     recipient_id=f"claude-code:{d.get('model') or 'unknown'}", mode="compiled",
                     tokens_in=d.get("tokens_in"), tokens_out=d.get("tokens_out"), latency_ms=d.get("latency_ms"),
                     cost_usd=0.0, score=float(task.correct(d.get("output"), truth[pid])),
                     parse_ok=d.get("output") is not None, error=d.get("error"))
        if pid in fr:  # API frontier (only when run with --api; not in the POC)
            f = fr[pid]
            log_call(conn, run_id=a.run_id, phase=task.TASK, task_type=task.TASK, instance_id=pid,
                     recipient_id=f"frontier:{f.get('served_model') or frontier.MODEL}", mode="compiled",
                     tokens_in=f.get("tokens_in"), tokens_out=f.get("tokens_out"), latency_ms=f.get("latency_ms"),
                     cost_usd=f.get("cost_usd"), score=float(task.correct(f.get("output"), truth[pid])),
                     parse_ok=f.get("output") is not None, error=f.get("error"))
    conn.commit()
    acc = accuracy_from_ledger(conn, a.run_id)
    # the open model on the anchored lane alone (the items the rule's bar is about)
    rules = {r["id"]: r for r in rules_mod.load()}
    proof = rules["payouts-need-proof"]
    anchored_ids = [pid for pid, lane in state["lanes"].items() if lane == proof["id"]]
    k = sum(task.correct(open_rows[pid]["output"], truth[pid]) for pid in anchored_ids)
    lo, hi = task.wilson(k, len(anchored_ids))
    acc[OPEN_ID + " (anchored lane, > $500)"] = {"n": len(anchored_ids), "correct": k,
                                                 "accuracy": round(k / len(anchored_ids), 4),
                                                 "ci_low": round(lo, 4), "ci_high": round(hi, 4)}
    o_acc = acc[OPEN_ID]
    cleared = rules_mod.bar_cleared(proof, o_acc["ci_low"])
    state["accuracy"] = acc
    state["bar"] = {"rule": proof["id"], "metric": proof["bar"]["metric"], "cleared": cleared,
                    "open_accuracy": o_acc["accuracy"], "open_ci_low": o_acc["ci_low"],
                    "needed_ci_low": proof["bar"]["ci_lower_gte"], "n": o_acc["n"],
                    "frontier_ratio": "not measured in POC"}

    # spot checks from the re-run (compare parsed outputs by hash)
    chains = {k: chain_for(job=state[k]["job"], tag=k, fixtures=P["nosana"], rpc=a.rpc) for k in ("batch", "rerun")}
    state["chains"] = chains
    rr_job, rr_node = chains["rerun"]["job"], chains["rerun"]["node"]
    spots = {pid: issue.spot_check(rerun_job=rr_job, rerun_node=rr_node, output_sha256=open_rows[pid]["output_sha256"],
                                   rerun_output_sha256=rerun_rows[pid]["output_sha256"])
             for pid in state["rerun"]["items"]}
    state["rerun_agreement"] = {"n": len(spots), "agreed": sum(s["agreed"] for s in spots.values())}

    checks = {}
    for pid in anchored_ids:
        c = []
        if "parse_error" not in open_rows[pid]["output"]:
            c.append("output_schema")
        if cleared:
            c.append("work_type_cleared_bar")
        if pid in spots and spots[pid]["agreed"]:
            c.append("spot_check_agreed")
        checks[pid] = c
    receipts = issue.anchored_from_result(
        result=batch_res, chain=chains["batch"], job_definition_cid=state["batch"]["jobdef_cid"],
        inputs_cid=state["batch"]["inputs_cid"], image=jd_mod.IMAGE, weights=f"{jd_mod.MODEL}@{jd_mod.REVISION}",
        items={pid: by_id[pid] for pid in anchored_ids}, rule=proof, rule_sha256=rules_mod.snapshot_sha256(proof),
        work_type=task.TASK, relay_checks=checks, spot_checks=spots, cost_usd_per_item=per_item)
    dflt = rules["payouts-default"]
    for pid, lane in state["lanes"].items():
        if lane != dflt["id"]:
            continue
        d = dec.get(pid)
        if not d or d.get("output") is None:
            log(f"{pid}: no Claude Code answer ({(d or {}).get('error')}); no declared receipt")
            continue
        receipts[pid] = issue.declared(
            item_id=pid, input_line=by_id[pid], output=d["output"],
            provider={"name": claude_code.NAME, "model": d["model"], "session_id": d["session_id"],
                      "timestamp": d["timestamp"]},
            rule=dflt, rule_sha256=rules_mod.snapshot_sha256(dflt), work_type=task.TASK, vendor=claude_code.NAME,
            relay_checks=["output_schema"], cost_usd=0.0)
    P["receipts"].mkdir(parents=True, exist_ok=True)
    for old in P["receipts"].glob("*.json"):
        old.unlink()
    conn.execute("DELETE FROM receipts WHERE run_id=?", (a.run_id,))
    for pid, r in sorted(receipts.items()):
        (P["receipts"] / f"{pid}.json").write_text(json.dumps(r, indent=2, sort_keys=True) + "\n")
        upsert(conn, "receipts", {
            "receipt_id": r["receipt_id"], "run_id": a.run_id, "item_id": pid, "grade": r["grade"],
            "rule_id": r["rule"]["id"], "rule_sha256": r["rule"]["snapshot_sha256"],
            "job": (r.get("nosana") or {}).get("job"), "output_sha256": r["output_sha256"],
            "merkle_root": (r.get("merkle") or {}).get("root"), "verified_at": None, "verify_verdict": None,
            "spot_check_agreed": None if r["grade"] != "anchored" or r["spot_check"]["agreed"] is None
            else int(r["spot_check"]["agreed"]), "receipt_json": canonical(r).decode()}, "receipt_id")
    conn.commit()
    state["receipts"] = {"anchored": sum(r["grade"] == "anchored" for r in receipts.values()),
                         "declared": sum(r["grade"] == "declared" for r in receipts.values())}
    state["review_queue"] = gate.write_queue(P["base"] / "review_queue.json", receipts, rules)
    save_state(a.run_id, state)
    log(f"accuracy: {json.dumps(acc)}")
    log(f"bar cleared: {cleared}; re-run agreement {state['rerun_agreement']}; receipts {state['receipts']}")


def _job_cost(conn, job: str) -> float:
    r = conn.execute("SELECT credits_before, credits_after FROM jobs WHERE job=?", (job,)).fetchone()
    return max(0.0, (r["credits_before"] or 0) - (r["credits_after"] or 0)) if r else 0.0


def accuracy_from_ledger(conn: sqlite3.Connection, run_id: str) -> dict:
    rows = conn.execute("SELECT recipient_id, COUNT(*) n, SUM(score) k, SUM(cost_usd) cost FROM calls "
                        "WHERE run_id=? AND phase=? GROUP BY recipient_id", (run_id, task.TASK)).fetchall()
    out = {}
    for r in rows:
        lo, hi = task.wilson(int(r["k"]), r["n"])
        out[r["recipient_id"]] = {"n": r["n"], "correct": int(r["k"]), "accuracy": round(r["k"] / r["n"], 4),
                                  "ci_low": round(lo, 4), "ci_high": round(hi, 4), "cost_usd": round(r["cost"] or 0, 6)}
    return out


# ---------------------------------------------------------------- verify

def cmd_verify(a) -> None:
    P, state = paths(a.run_id), load_state(a.run_id)
    conn = db(a.run_id)
    f = Fetcher(a.rpc)
    report, counts = [], {0: 0, 1: 0, 2: 0}
    for path in sorted(P["receipts"].glob("*.json")):
        r = json.loads(path.read_text())
        cks = verify(r, f)
        v = verdict(cks)
        counts[v] += 1
        name = ["verified", "failed", "could not check"][v]
        report.append({"item_id": r["item_id"], "grade": r["grade"], "verdict": name,
                       "checks": [c.to_dict() for c in cks]})
        conn.execute("UPDATE receipts SET verified_at=?, verify_verdict=? WHERE receipt_id=?",
                     (time.time(), name, r["receipt_id"]))
        log(f"{r['item_id']} ({r['grade']}): {name}" + "".join(f"; {c.id} {c.reason}" for c in cks if c.ok is not True))
    conn.commit()
    (P["base"] / "verify_report.json").write_text(json.dumps(report, indent=2) + "\n")
    state["verify"] = {"verified": counts[0], "failed": counts[1], "could_not_check": counts[2]}
    save_state(a.run_id, state)
    log(f"verify: {state['verify']}")


# ---------------------------------------------------------------- export

def cmd_export(a) -> None:
    P = paths(a.run_id)
    conn = db(a.run_id)
    P["ledger_dir"].mkdir(parents=True, exist_ok=True)
    for table, where in (("jobs", "run_id=?"), ("receipts", "run_id=?"), ("rules", None), ("calls", "run_id=?")):
        q = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "")
        rows = [dict(r) for r in conn.execute(q, (a.run_id,) if where else ())]
        (P["ledger_dir"] / f"{table}.json").write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n")
        log(f"exported {table}: {len(rows)} rows")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["prepare", "nosana", "declared", "frontier", "issue", "verify", "export"])
    p.add_argument("--api", action="store_true", help="allow the API frontier lane (spends API credits)")
    p.add_argument("--run-id", required=True)
    p.add_argument("--market", default=jd_mod.MARKET_3090)
    p.add_argument("--rpc", default=os.environ.get("SOLANA_RPC", DEFAULT_RPC))
    p.add_argument("--cap", type=float, default=PHASE1_CAP_USD)
    p.add_argument("--max-attempts", type=int, default=3, help="per tag, counting earlier attempts")
    p.add_argument("--avoid-node", action="append", help="node to leave out when targeting a retry")
    a = p.parse_args(argv)
    {"prepare": cmd_prepare, "nosana": cmd_nosana, "declared": cmd_declared, "frontier": cmd_frontier, "issue": cmd_issue,
     "verify": cmd_verify, "export": cmd_export}[a.cmd](a)


if __name__ == "__main__":
    main()
