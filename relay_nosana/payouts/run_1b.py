"""Phase 1b: raw vs compiled context, one Nosana batch job, fresh seed (docs/phase1b-report.md).

    python -m relay_nosana.payouts.run_1b prepare --run-id R [--model 7b|14b]  # data, inputs, pre-registration
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

from .. import gate
from .. import rules as rules_mod
from ..lane import Budget, BudgetExceeded, chain_for, log, run_batch
from ..ledger import log_call, upsert
from ..nosana import jobdef as jd_mod
from ..nosana import nodes as nodes_mod
from ..nosana.jobs import Jobs, idle_nodes
from ..receipts import issue
from . import task
from .data import (N_1E, NOTE_CHANGES, NOTE_PLAN_1E, PLAN_1B, PLAN_1E_POOL, SEED_1B, SEED_1E, add_notes,
                   content_key, draw, generate, truth_keywords_naive, truth_rules_only,
                   truth_rules_regex)
from .run_poc import ROOT, amounts, db, load_state, paths, save_state

ROOT_RUNS = ROOT / "runs"

PHASE1B_CAP_USD = 2.0
PHASE1B_BASELINE = 3.405   # settled credits when Phase 1b started: the cap counts every Phase 1b job from here
SHARD_PAYOUTS = 100        # payouts per job when sharded: 200 result rows (~140 KB) stay in one 256 KiB block
DEADLINE_S = 1200          # in-container deadline: 20 minutes (7B, 14B)
DEADLINES_S = {"32b": 2700}  # 32B AWQ (~19 GB download): 45 minutes (Sogol, 2026-11-11)


def deadline_for(model_key: str) -> int:
    return DEADLINES_S.get(model_key, DEADLINE_S)
MODELS = {  # pinned by commit; a run's model is fixed in its pre-registration
    "7b": (jd_mod.MODEL, jd_mod.REVISION),
    "14b": ("Qwen/Qwen2.5-14B-Instruct-AWQ", "539535859b135b0244c91f3e59816150c8056698"),
    "32b": ("Qwen/Qwen2.5-32B-Instruct-AWQ", "5c7cb76a268fc6cfbb9c4777eb24ba6e27f9ee6c"),  # run g, A6000 (48 GB)
}
MARKET_A6000 = "EjryZ6XEthz3z7nnLfjXBYafyn7VyHgChfbfM47LfAao"  # NVIDIA A6000 PREMIUM, vLLM v0.10.2 cached
MARKET_A40_COMMUNITY = "8kGjraXnXirSQcgjxTbFBK7QTBVz9fm9ufSRVr3DqBTV"  # NVIDIA A40 Community (48 GB), v0.10.2 cached
MARKET_A100_40 = "F3aGGSMb73XHbJbDXVbcXo7iYM9fyevvAZGQfwgrnWtB"  # NVIDIA A100 40GB PREMIUM
MARKET_A100_80 = "GLJHzqRN9fKGBsvsFzmGnaQGknUtLN1dqaFR8n3YdM22"  # NVIDIA A100 80GB PREMIUM
MARKET_USD_PER_HOUR = {jd_mod.MARKET_3090: 0.1745, MARKET_A6000: 0.3636, MARKET_A40_COMMUNITY: 0.27273,
                       MARKET_A100_40: 0.5545, MARKET_A100_80: 0.8181}  # credit jobs: premium markets only  # Phase 0 discover; a post reserves 1 h


def _model(state: dict) -> tuple[str, str]:
    return MODELS[state.get("model_key", "7b")]  # payouts-2026-11-10b predates model_key: 7B


DESIGNS = {  # design -> (system prompt, output schema). 1b: run b. 1e: run e, with support notes and rule 6.
    "1b": (task.SYSTEM_1B, task.OUTPUT_SCHEMA_1B),
    "1e": (task.SYSTEM_1E, task.OUTPUT_SCHEMA_1E),
}


def build_batch(lines: list[dict], run_id: str, tag: str, model_key: str = "7b", design: str = "1b"):
    model, revision = MODELS[model_key]
    return jd_mod.build(lines, DESIGNS[design][1], run_id, tag, task.TASK, model=model, revision=revision,
                        phase="1b", keep_schema_order=True, output_keys=task.OUTPUT_KEYS_1B, deadline_s=deadline_for(model_key))


# ---------------------------------------------------------------- prepare + pre-registration

def _judgment_part(run: dict) -> dict:
    """Run e's pre-registered account of what the model must judge, and the baselines it is shown against."""
    truth, paid = run["truth"], run["paid_invoices"]
    n = len(run["payouts"])
    intents = {p["payout_id"]: p["notes"][0]["intent"] for p in run["payouts"] if p.get("notes")}
    changed = [p["payout_id"] for p in run["payouts"] if truth[p["payout_id"]] != truth_rules_only(p, paid)]
    by_intent: dict[str, int] = {}
    for i in intents.values():
        by_intent[i] = by_intent.get(i, 0) + 1
    return {
        "label_inputs": "data.truth_1e: seller_id vs seller-hold decisions; invoice_id vs the seller's paid invoices; "
                        "amount vs the sum of line totals; each sale line's sku, sale_date and unit price vs the "
                        "price-change decisions; each refund line's order_date (> 30 days before the payout date) "
                        "and approval_id; the rule priority; and each support note's hidden intent (never shown to "
                        "the model). Amount thresholds ($500, $5,000) set the lane and the re-run, never the label",
        "judgment": {
            "what": "free-text support notes (from finance, support, sellers or buyers) and two policies, D-106 "
                    "(which notes count as refund approval) and D-107 (open vs withdrawn disputes). The model must "
                    "read who wrote a note, which order or invoice it names, and whether a dispute is still open",
            "notes_plan_pool_210": NOTE_PLAN_1E,
            "items_with_notes": len(intents), "by_intent": dict(sorted(by_intent.items())),
            "items_whose_answer_the_notes_change": len(changed),
            "share_answer_depends_on_notes": round(len(changed) / n, 4),
            "changes": f"{', '.join(NOTE_CHANGES)}: approve an old refund finance approved in a note; hold a "
                       "payout with an open dispute on its invoice (reason disputed)",
            "caveat": "synthetic: the notes come from our own templates, so a parser written against them could "
                      "decode them. They stand in for text a code-only rule set does not read",
        },
        "baselines": {
            "rules_only": f"code alone, no model: rules 1-5 over the structured fields, notes unread "
                          f"(data.truth_rules_only). Scored on the same {n} items; by construction it is right on "
                          f"every item the notes don't change: {n - len(changed)}/{n}",
            "rules_regex": "code alone, no model: rules 1-5 plus a keyword/regex reading of the notes "
                           "(data.truth_rules_regex: a finance note naming the refund's order with an approval word "
                           "approves it; the latest note naming this invoice with a dispute word holds it, a "
                           "withdrawn/closed word releases it). keywords_naive, reported beside it, drops the sender, "
                           "order and invoice checks",
            "frontier": "declared lane, not anchored: headless Claude Code (relay_nosana/payouts/claude_code.py, "
                        "Phase 1's lane, the owner's login, no API credits) on the compiled-arm input of each of the "
                        f"{n} items, same schema; run_1b frontier. Its score is the frontier column; the bar stays "
                        "absolute",
        },
        "report_columns": ["raw", "compiled v2", "rules only", "rules + regex", "frontier"],
    }


def _ops_1e(lanes: dict, amt: dict, run_id: str) -> dict:
    proof = next(r for r in rules_mod.load() if r["id"] == "payouts-need-proof")
    rerun = rules_mod.rerun_sample(proof, [(p, amt[p]) for p, l in lanes.items() if l == proof["id"]], run_id)
    return {
        "rerun_items": len(rerun),
        "cost_estimate_usd": {
            "basis": "measured on this market, image and model: 7 successful 7B jobs took 161-321 s listed->finished "
                     "and settled $0.010-0.016 (~$0.175/hour); run b's 120-line batch generated in 14.5 s",
            "jobs": f"batch1 (200 lines), batch2 (200 lines), re-run ({len(rerun)} lines): 3 Nosana jobs",
            "expected": "0.03-0.05",
            "bad_node": "a download that hits the 20-min deadline ~$0.064 (seen today); a hang stopped at 1,500 s "
                        "~$0.073",
            "worst_case": "0.88 (all 4 attempts of all 3 jobs stopped)",
            "frontier": "Claude Code on the owner's login: no API charge; Phase 1's notional list price was about "
                        "$0.014 per item, so ~$2.80 notional for 200, not counted against the Nosana cap",
        },
        "cap_scope": "USD on Nosana, all of Phase 1b, from settled credits 3.405 (runs b, diag, c, run b's re-run, "
                     "run e). Budget.check refuses any post (batch, re-run or retry) whose $0.175 up-front "
                     "reservation would take the total past $2.00",
        "result_size": "each shard's result ~140 KB (run b: 82.8 KB for 120 rows), under one 256 KiB IPFS block",
        "posting": "not posted; requires the owner's explicit approval",
    }


def _threshold(n: int, ci_low: float) -> dict:
    """Smallest k with Wilson 95% lower bound >= ci_low, and the bound at k and k - 1."""
    k = next(k for k in range(n + 1) if task.wilson(k, n)[0] >= ci_low)
    return {"n": n, "min_correct": k, "max_errors": n - k, "accuracy": round(k / n, 4),
            "wilson_low_at_min": round(task.wilson(k, n)[0], 4),
            "wilson_low_one_fewer": round(task.wilson(k - 1, n)[0], 4)}


def _phase1c(a, rerun_items: int) -> dict:
    """Phase 1c (Sogol, 2026-11-11): skip the smaller models; 32B AWQ on a 48 GB market; $2 for all of Phase 1c."""
    hourly = MARKET_USD_PER_HOUR.get(a.market)
    return {
        "phase": "1c",
        "cost_estimate_usd": {
            "basis": f"market {a.market} at ${hourly}/hour; each post reserves 1 hour. 32B AWQ is ~19 GB to "
                     "download; no 32B job has run on Nosana before, so the time is not measured yet",
            "jobs": f"batch1 (200 lines), batch2 (200 lines), re-run ({rerun_items} lines): 3 Nosana jobs, "
                    "each downloads the model",
            "expected": f"{round(3 * hourly * 0.25, 2)}-{round(3 * hourly * 0.45, 2)} (15-27 min per job)",
            "bad_node": f"a download that hits the 45-min deadline ~${round(hourly * 0.8, 2)}",
            "worst_case": "bounded by the cap: Budget.check refuses any post that could pass $2.00",
            "frontier": "deferred: the owner's Claude Code limit resets Nov 14; no API key",
        },
        "cap_usd": 2.0,
        "cap_scope": "USD on Nosana, all of Phase 1c, from settled credits 3.536, including run e's stopped "
                     "attempts ($0.107 settled before this pre-registration)",
        "run_e": "stopped by the owner, partial, not counted; its partial results are not used for any decision",
        "market_choice": "48 GB class, checked for idle nodes on 2026-11-11: A6000 PREMIUM 1, A40 Community 3, "
                         "A40 PREMIUM 0, A100 40GB Community 47 (fallback). A40 Community is the only 48 GB market "
                         "with two or more idle nodes, which the batch + second-node re-run need",
    }


def cmd_prepare(a) -> None:
    P = paths(a.run_id)
    if (P["base"] / "preregistration.json").exists():
        sys.exit("already prepared: the pre-registration is fixed once written")
    P["base"].mkdir(parents=True, exist_ok=True)
    if a.sample == "1b":
        run, seed, plan_note = generate(SEED_1B, PLAN_1B), SEED_1B, "Phase 1 label mix x2"
    else:  # 1e: 200 of a 210-payout pool with support notes, none identical to a run-b payout
        if a.compiler != "v2":
            sys.exit("run e's design uses compiler v2")
        b = json.loads((ROOT_RUNS / "payouts-2026-11-10b" / "payout_run.json").read_text())
        pool = add_notes(generate(SEED_1E, PLAN_1E_POOL), SEED_1E)
        run = draw(pool, N_1E, SEED_1E, {content_key(p) for p in b["payouts"]})
        seed, plan_note = SEED_1E, ("Phase 1 label mix x7 (210-payout pool) with support notes (NOTE_PLAN_1E), "
                                    "200 drawn, run b's payouts excluded")
    design = a.sample
    P["run"].write_text(json.dumps(run, indent=2, sort_keys=True) + "\n")
    build_line = ((lambda p, arm: task.input_line_1b(p, run["paid_invoices"], arm, compiler=a.compiler))
                  if design == "1b" else (lambda p, arm: task.input_line_1e(p, run["paid_invoices"], arm)))
    lines = [build_line(p, arm) for p in run["payouts"] for arm in task.ARMS]
    pids = [p["payout_id"] for p in run["payouts"]]
    shards = ([pids[i:i + SHARD_PAYOUTS] for i in range(0, len(pids), SHARD_PAYOUTS)]
              if len(pids) > SHARD_PAYOUTS else None)
    P["inputs"].write_bytes(jd_mod.inputs_bytes(lines))
    rules = rules_mod.load()
    conn = db(a.run_id)
    for r in rules:
        upsert(conn, "rules", {"snapshot_sha256": rules_mod.snapshot_sha256(r), "rule_id": r["id"],
                               "rule_json": canonical(r).decode(), "first_seen": time.time()}, "snapshot_sha256")
    conn.commit()
    amt = amounts(run)
    lanes = {pid: rules_mod.rule_for(rules, task.TASK, amt[pid])["id"] for pid in amt}
    jd, raw, inputs_cid = build_batch(lines, a.run_id, "batch", a.model, design)
    shard_defs = []
    for i, sp in enumerate(shards or [], start=1):
        sub = [x for x in lines if x["item_id"].split("/")[0] in set(sp)]
        sjd, sraw, scid = build_batch(sub, a.run_id, f"batch{i}", a.model, design)
        shard_defs.append({"tag": f"batch{i}", "payouts": [sp[0], sp[-1]], "lines": len(sub), "inputs_cid": scid,
                           "job_definition_cid": __import__("relay_verify.cid", fromlist=["x"]).cidv0_of_bytes(
                               canonical(sjd))})
    model, revision = MODELS[a.model]
    proof = next(r for r in rules if r["id"] == "payouts-need-proof")
    prereg = {
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": a.run_id, "phase": "1b", "data": "synthetic",
        "seed": seed, "payouts": len(run["payouts"]), "plan": plan_note,
        **({"why_60": "Wilson 95% lower bound for 30/30 is 0.886 < 0.90; at n = 60, 59/60 gives 0.911"}
           if a.sample == "1b" else {"pool": run["pool"]}),
        "threshold": _threshold(len(run["payouts"]), proof["bar"]["ci_lower_gte"]),
        "compiler": a.compiler,
        "arms": ({"raw": task.decisions_raw.__doc__.strip(), "compiled": task.decisions_compiled.__doc__.strip()}
                 if design == "1b" else
                 {"raw": task.decisions_raw_1e.__doc__.strip() + " The payout and its support notes, unchanged.",
                  "compiled": "Compiled v2: rules 1-3 (seller on an active hold, duplicate invoice, amount vs the "
                              "sum of lines) resolved in code (task.resolved_facts) and written as facts; then "
                              + task.decisions_compiled_1e.__doc__.strip() + " The payout and its support notes, "
                              "unchanged. The model checks rules 4-6 and makes the final decision. Same system "
                              "prompt as the raw arm."}),
        "model": f"{model}@{revision}", "image": jd_mod.IMAGE, "market": a.market,
        "params": {"temperature": 0, "seed": 0, "max_tokens": task.MAX_TOKENS_1B, "guided_json": True},
        "output_schema": DESIGNS[design][1], "hashed_output_keys": task.OUTPUT_KEYS_1B,
        "generation_key_order": list(DESIGNS[design][1]["properties"]),  # this file sorts keys; the job doesn't
        "system_prompt_sha256": hashlib.sha256(DESIGNS[design][0].encode()).hexdigest(),
        **(_judgment_part(run) | _ops_1e(lanes, amt, a.run_id) if design == "1e" else {}),
        "inputs_cid": inputs_cid, "inputs_sha256": hashlib.sha256(raw).hexdigest(),
        "job_definition_cid": __import__("relay_verify.cid", fromlist=["x"]).cidv0_of_bytes(canonical(jd)),
        **({"shards": shard_defs} if shard_defs else {}),
        "scoring": f"exact match of {{decision, reason_code}} against data.truth(); per arm over all "
                   f"{len(run['payouts'])} payouts, "
                   "Wilson 95% CI; also the > $500 subset; paired discordant counts with an exact two-sided "
                   "binomial (sign) test",
        "bar": proof["bar"], "rule_snapshot_sha256": rules_mod.snapshot_sha256(proof),
        "routing": "compiled arm if its CI lower bound >= 0.90, else human review for payouts > $500",
        "rerun": proof["rerun"], "rerun_arm": "compiled",
        "nodes": "known-good node list per market from measured outcomes (relay_nosana/nosana/nodes.py), "
                 f"open market as fallback; in-container deadline {deadline_for(a.model) // 60} min",
        "cap_usd": PHASE1B_CAP_USD,
        **(_phase1c(a, _ops_1e(lanes, amt, a.run_id)["rerun_items"]) if a.model == "32b" else {}),
        "expectation": a.expectation or "compiled > raw (Phase 1: raw-style context, no reasoning, 4/30)",
        **({"why": a.why} if a.why else {}),
    }
    (P["base"] / "preregistration.json").write_text(json.dumps(prereg, indent=2, sort_keys=True) + "\n")
    save_state(a.run_id, {"run_id": a.run_id, "phase": "1b", "model_key": a.model, "compiler": a.compiler,
                          "design": design, "market": a.market,
                          "lanes": lanes, **({"shards": shards} if shards else {}), "prepared_at": time.time()})
    n_anch = sum(1 for v in lanes.values() if v == "payouts-need-proof")
    log(f"prepared {len(run['payouts'])} payouts x {len(task.ARMS)} arms = {len(lines)} lines; {n_anch} over $500; "
        f"inputs {P['inputs'].stat().st_size} bytes; job definition {len(canonical(jd))} bytes, "
        f"CID {prereg['job_definition_cid']}")


# ---------------------------------------------------------------- nosana

def batches(state: dict) -> list[tuple[str, list[str] | None]]:
    """(state key = job tag, payout ids or None for all). Run b: one job, "batch". Sharded runs: batch1..N."""
    shards = state.get("shards")
    return [("batch", None)] if not shards else [(f"batch{i}", pids) for i, pids in enumerate(shards, start=1)]


def _tried(conn, run_id: str) -> set[str]:
    return {r["node"] for r in conn.execute("SELECT node FROM jobs WHERE run_id=? AND node IS NOT NULL", (run_id,))}


def _rerun_any_market(a, state, P, conn, jobs, budget, by_id, run, weights, model_key) -> None:
    """Run g (amended): the re-run goes to a second node waiting in any 48 GB+ premium market. If none is free
    within rerun_window_s of the batch finishing, the re-run is recorded as pending: receipts say so, and the gate
    holds the payouts that need it until it runs."""
    rule = next(r for r in rules_mod.load() if r["id"] == "payouts-need-proof")
    amt = amounts(run)
    anchored = [(pid, amt[pid]) for pid, lane in state["lanes"].items() if lane == rule["id"]]
    sample = rules_mod.rerun_sample(rule, anchored, a.run_id)
    sub = [by_id[f"{pid}/compiled"] for pid in sample]
    first = {state[k]["account"]["node"] for k, _ in batches(state) if state[k].get("account")} | \
        {state[k].get("node") for k, _ in batches(state)} - {None}
    jd, _, inputs_cid = build_batch(sub, a.run_id, "rerun", model_key, state.get("design", "1b"))
    finished = max(state[k].get("finished_wall") or time.time() for k, _ in batches(state))
    until = finished + state["rerun_window_s"]
    tried: set[str] = set(first)
    rr = {"ok": False, "items": [f"{p}/compiled" for p in sample], "attempts": [], "window_until": until}
    while time.time() < until:
        pairs = nodes_mod.waiting_nodes(a.rpc, avoid=tried)
        if not pairs:
            log(f"re-run: no second node waiting in any 48 GB+ premium market; "
                f"{int(until - time.time())} s left in the window")
            time.sleep(60)
            continue
        market, node = pairs[0]
        tried.add(node)
        price = nodes_mod.PREMIUM_48GB_PLUS[market][1]
        log(f"re-run: node {node} waiting on {nodes_mod.PREMIUM_48GB_PLUS[market][0]} ({market}, ${price}/h)")
        n_prev = len(list(P["nosana"].glob("rerun_a*_post.json")))
        try:
            out = run_batch(conn=conn, run_id=a.run_id, tag="rerun", jobdef=jd, market=market, rpc=a.rpc, jobs=jobs,
                            budget=budget, fixtures=P["nosana"], image=jd_mod.IMAGE, weights=weights,
                            inputs_cid=inputs_cid, node_for_attempt=lambda i, n=node: n,
                            estimate_usd=round(1.15 * price, 3), max_attempts=n_prev + 1,
                            queue_wait_s=600, run_wait_s=deadline_for(model_key) + 300)
        except BudgetExceeded as e:
            log(f"re-run on {market} refused by the cap: {e}")
            continue
        rr["attempts"] += [r["job"] for r in out.attempts if r["job"] not in rr["attempts"]]
        if out.ok:
            rr.update(ok=True, job=out.job, jobdef_cid=out.jobdef_cid, inputs_cid=inputs_cid, account=out.account,
                      result_file=out.result_file, market=market, node=node)
            break
    if not rr["ok"]:
        rr.update(status="pending", why=f"no second node in a 48 GB+ premium market ran the re-run within "
                                        f"{state['rerun_window_s'] // 60} min of the batch finishing")
        log(f"re-run pending: {rr['why']}")
    state["rerun"] = rr
    save_state(a.run_id, state)


def cmd_nosana(a) -> None:
    P, state = paths(a.run_id), load_state(a.run_id)
    if state.get("market") and a.market != state["market"]:
        if a.market != jd_mod.MARKET_3090:
            sys.exit(f"--market {a.market} differs from the pre-registered market {state['market']}")
        a.market = state["market"]  # the default flag value: use the run's own market
    estimate = round(1.15 * MARKET_USD_PER_HOUR.get(a.market, 0.4), 3)
    run = json.loads(P["run"].read_text())
    lines = [json.loads(x) for x in P["inputs"].read_text().splitlines()]
    by_id = {x["item_id"]: x for x in lines}
    if state.get("arms"):  # run g (amended): only these arms are posted
        lines = [x for x in lines if x["arm"] in state["arms"]]
    conn = db(a.run_id)
    jobs = Jobs()
    budget = Budget(jobs, a.cap)
    # the cap counts every job since the baseline: all of Phase 1b by default; --baseline starts a new budget
    # (Phase 1c: runs e, f, g share $2 from settled 3.536, Sogol 2026-11-10)
    budget.baseline = a.baseline if a.baseline is not None else state.get("budget_baseline", PHASE1B_BASELINE)
    state["budget_baseline"] = budget.baseline
    state["known_nodes"] = {k: v for k, v in nodes_mod.known_nodes(a.market).items() if k != "table"}
    save_state(a.run_id, state)
    weights = "@".join(_model(state))
    model_key = state.get("model_key", "7b")

    def batch_node(attempt: int) -> str | None:
        if state.get("queue_check"):  # run g: post only to a node that is waiting in the market queue on Solana
            waiting = [n for m, n in nodes_mod.waiting_nodes(a.rpc, {a.market: nodes_mod.PREMIUM_48GB_PLUS[a.market]},
                                                             _tried(conn, a.run_id))]
            if not waiting:
                elsewhere = nodes_mod.waiting_nodes(a.rpc)
                sys.exit(f"no node waiting on {a.market}; not posting. 48 GB+ premium markets with nodes waiting: "
                         f"{elsewhere or 'none'}")
            log(f"batch attempt {attempt}: {waiting[0]} (waiting in the market queue on Solana)")
            return waiting[0]
        node, why = nodes_mod.pick(a.market, idle_nodes(a.market, set()), _tried(conn, a.run_id))
        log(f"batch attempt {attempt}: {node or 'open market'} ({why})")
        return node

    for key, pids in batches(state):
        if (state.get(key) or {}).get("ok"):
            continue
        sub = lines if pids is None else [x for x in lines if x["item_id"].split("/")[0] in set(pids)]
        jd, _, inputs_cid = build_batch(sub, a.run_id, key, model_key, state.get("design", "1b"))
        out = run_batch(conn=conn, run_id=a.run_id, tag=key, jobdef=jd, market=a.market, rpc=a.rpc, jobs=jobs,
                        budget=budget, fixtures=P["nosana"], image=jd_mod.IMAGE, weights=weights,
                        inputs_cid=inputs_cid, node_for_attempt=batch_node, estimate_usd=estimate, max_attempts=a.max_attempts,
                        queue_wait_s=900, run_wait_s=deadline_for(model_key) + 300)
        state[key] = {"ok": out.ok, "job": out.job, "jobdef_cid": out.jobdef_cid, "inputs_cid": inputs_cid,
                      "account": out.account, "attempts": [r["job"] for r in out.attempts],
                      "result_file": out.result_file, "finished_wall": time.time(),
                      **({"payouts": pids} if pids else {})}
        save_state(a.run_id, state)
        if not out.ok:
            sys.exit(f"{key} failed after {len(out.attempts)} attempts; see runs/{a.run_id}/nosana/")

    if state.get("rerun_window_s") and not (state.get("rerun") or {}).get("ok") and not a.batch_only:
        _rerun_any_market(a, state, P, conn, jobs, budget, by_id, run, weights, model_key)
    elif not (state.get("rerun") or {}).get("ok") and not a.batch_only:
        rule = next(r for r in rules_mod.load() if r["id"] == "payouts-need-proof")
        amt = amounts(run)
        anchored = [(pid, amt[pid]) for pid, lane in state["lanes"].items() if lane == rule["id"]]
        sample = rules_mod.rerun_sample(rule, anchored, a.run_id)
        sub = [by_id[f"{pid}/compiled"] for pid in sample]
        first = {state[k]["account"]["node"] for k, _ in batches(state)}
        bad = set(nodes_mod.known_nodes(a.market)["bad"])

        def rerun_node(attempt: int) -> str | None:
            avoid = _tried(conn, a.run_id) | first
            idle = idle_nodes(a.market, set())
            node, why = nodes_mod.pick(a.market, idle, avoid)
            if node is None:  # the open market could hand the re-run to the batch's own node: target instead
                rest = [n for n in idle if n not in avoid and n not in bad]
                if not rest:
                    raise RuntimeError("no idle second node")
                node, why = rest[0], "no known-good node idle: an idle node not known bad"
            log(f"rerun attempt {attempt}: {node} ({why})")
            return node

        jd, _, inputs_cid = build_batch(sub, a.run_id, "rerun", model_key, state.get("design", "1b"))
        out = run_batch(conn=conn, run_id=a.run_id, tag="rerun", jobdef=jd, market=a.market, rpc=a.rpc, jobs=jobs,
                        budget=budget, fixtures=P["nosana"], image=jd_mod.IMAGE, weights=weights,
                        inputs_cid=inputs_cid, node_for_attempt=rerun_node, estimate_usd=estimate, max_attempts=a.max_attempts,
                        queue_wait_s=900, run_wait_s=deadline_for(model_key) + 300)
        state["rerun"] = {"ok": out.ok, "job": out.job, "jobdef_cid": out.jobdef_cid, "inputs_cid": inputs_cid,
                          "account": out.account, "items": [f"{p}/compiled" for p in sample],
                          "attempts": [r["job"] for r in out.attempts], "result_file": out.result_file}
        save_state(a.run_id, state)
        if not out.ok:
            sys.exit(f"re-run failed after {len(out.attempts)} attempts")
    state["nosana_spent_usd"] = budget.spent()
    save_state(a.run_id, state)
    log(f"nosana done: batches {[state[k]['job'] for k, _ in batches(state)]}, re-run {(state.get('rerun') or {}).get('job')}, "
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


def _score(ok: dict[str, bool], pids: list[str], changed: set[str]) -> dict:
    k, n = sum(ok[p] for p in pids), len(pids)
    lo, hi = task.wilson(k, n)
    kc = sum(ok[p] for p in pids if p in changed)
    return {"correct": k, "n": n, "accuracy": round(k / n, 4), "ci_low": round(lo, 4), "ci_high": round(hi, 4),
            "on_note_dependent": f"{kc}/{len(changed)}",
            "on_the_rest": f"{k - kc}/{n - len(changed)}"}


def _columns_1e(P, run, arms, pids, truth) -> dict:
    """raw / compiled v2 / rules only / frontier on the same items, with the note-dependent split."""
    paid = run["paid_invoices"]
    by_id = {p["payout_id"]: p for p in run["payouts"]}
    changed = {p for p in pids if truth[p] != truth_rules_only(by_id[p], paid)}
    cols = {"raw": _score(arms["raw"]["_ok"], pids, changed) if "raw" in arms else "not posted (run g amendment)",
            "compiled_v2": _score(arms["compiled"]["_ok"], pids, changed),
            "rules_only": _score({p: truth_rules_only(by_id[p], paid) == truth[p] for p in pids}, pids, changed),
            # fourth no-model baseline (Sogol, 2026-11-10): rules + a keyword/regex reading of the notes
            "rules_regex": _score({p: truth_rules_regex(by_id[p], paid) == truth[p] for p in pids}, pids, changed),
            "keywords_naive": _score({p: truth_keywords_naive(by_id[p], paid) == truth[p] for p in pids}, pids,
                                     changed)}
    f = P["base"] / "frontier.jsonl"
    recs = {r["item_id"].split("/")[0]: r for r in map(json.loads, f.read_text().splitlines())} if f.exists() else {}
    cols["frontier"] = (_score({p: task.correct((recs.get(p) or {}).get("output"), truth[p]) for p in pids},
                               pids, changed) | {"errors": sum(1 for p in pids if (recs.get(p) or {}).get("error")),
                                                 "missing": sum(1 for p in pids if p not in recs),
                                                 "model": sorted({r.get("model") for r in recs.values()} - {None})}
                        if recs else "not run")
    return cols


FRONTIER_MODEL = "claude-opus-5-5"  # pinned with --model; each record also keeps the model the output reports


def cmd_frontier(a) -> None:
    """Declared frontier column (run e): headless Claude Code on each compiled-arm input, the compiled v2 system
    prompt and user message verbatim, no tools, model pinned. Resumable."""
    from . import claude_code
    P, state = paths(a.run_id), load_state(a.run_id)
    if state.get("design") != "1e":
        sys.exit("the frontier column is part of run e's design only")
    lines = [x for x in map(json.loads, P["inputs"].read_text().splitlines()) if x.get("arm") == "compiled"]
    if len(lines) != len(json.loads(P["run"].read_text())["truth"]):
        sys.exit(f"found {len(lines)} compiled-arm lines, expected one per payout")
    out = P["base"] / "frontier.jsonl"
    done = {json.loads(x)["item_id"] for x in out.read_text().splitlines()} if out.exists() else set()
    for line in lines:
        if line["item_id"] in done:
            continue
        rec = claude_code.call(line, task.OUTPUT_SCHEMA_1E, model=FRONTIER_MODEL)
        with out.open("a") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        log(f"frontier {line['item_id']}: {rec.get('output') or rec.get('error')}")


def _frontier_receipts(P, run, amt, rules, by_id) -> dict[str, dict]:
    """Declared receipts for the frontier column (run e): one per answered item, under the rule its amount
    selects. Over $500 that rule requires anchored, so the gate refuses these; they record the comparison."""
    f = P["base"] / "frontier.jsonl"
    out_dir = P["receipts"] / "frontier"
    if out_dir.exists():
        for old in out_dir.glob("*.json"):
            old.unlink()
    if not f.exists():
        return {}
    out_dir.mkdir(parents=True, exist_ok=True)
    recs = {}
    for rec in map(json.loads, f.read_text().splitlines()):
        if rec.get("output") is None:
            continue
        pid = rec["item_id"].split("/")[0]
        rule = rules_mod.rule_for(list(rules.values()), task.TASK, amt[pid])
        out = {k: rec["output"][k] for k in task.OUTPUT_KEYS_1B}  # hash only {decision, reason_code}, as anchored
        recs[pid] = issue.declared(
            item_id=rec["item_id"], input_line=by_id[rec["item_id"]], output=out,
            provider={"name": claude_code_name(), "model": rec["model"], "session_id": rec["session_id"],
                      "timestamp": rec["timestamp"]},
            rule=rule, rule_sha256=rules_mod.snapshot_sha256(rule), work_type=task.TASK, vendor=claude_code_name(),
            relay_checks=["output_schema"], cost_usd=0.0)
        (out_dir / f"{pid}.json").write_text(json.dumps(recs[pid], indent=2, sort_keys=True) + "\n")
    return recs


def claude_code_name() -> str:
    from . import claude_code
    return claude_code.NAME


def cmd_issue(a) -> None:
    P, state = paths(a.run_id), load_state(a.run_id)
    run = json.loads(P["run"].read_text())
    truth, amt = run["truth"], amounts(run)
    lines = [json.loads(x) for x in P["inputs"].read_text().splitlines()]
    by_id = {x["item_id"]: x for x in lines}
    results, rows = {}, {}
    for key, _ in batches(state):
        results[key] = json.loads((P["nosana"] / state[key]["result_file"]).read_text())
        rows.update(parse_outputs(results[key])[0])
    rr = state.get("rerun") or {}
    rerun_rows = parse_outputs(json.loads((P["nosana"] / rr["result_file"]).read_text()))[0] if rr.get("ok") else {}
    pids = sorted(truth)
    conn = db(a.run_id)
    conn.execute("DELETE FROM calls WHERE run_id=?", (a.run_id,))
    cost = sum(conn.execute("SELECT credits_before - credits_after c FROM jobs WHERE job=?",
                            (state[key]["job"],)).fetchone()["c"] or 0.0 for key, _ in batches(state))
    per_item = max(0.0, cost) / len(lines)
    rules = {r["id"]: r for r in rules_mod.load()}
    proof = rules["payouts-need-proof"]
    anchored = [pid for pid in pids if state["lanes"][pid] == proof["id"]]
    model, revision = _model(state)
    model_id = f"nosana:{model}@{revision[:12]}"

    arms = {}
    posted_arms = state.get("arms", task.ARMS)
    for arm in posted_arms:
        ok = {}
        for pid in pids:
            r = rows[f"{pid}/{arm}"]
            ok[pid] = task.correct(r["output"], truth[pid])
            log_call(conn, run_id=a.run_id, phase=task.TASK, task_type=task.TASK, instance_id=pid,
                     recipient_id=f"{model_id}:{arm}", mode="compiled" if arm == "compiled" else "raw",
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
    if state.get("design") == "1e":
        state["columns"] = _columns_1e(P, run, arms, pids, truth)
    if "raw" in arms:
        b = sum(arms["compiled"]["_ok"][p] and not arms["raw"]["_ok"][p] for p in pids)
        c = sum(arms["raw"]["_ok"][p] and not arms["compiled"]["_ok"][p] for p in pids)
        paired = {"compiled_right_raw_wrong": b, "raw_right_compiled_wrong": c, "sign_test_p": round(sign_test(b, c), 6)}
    else:
        paired = "raw arm not posted (run g amendment); run b measured raw vs compiled"
    for arm in arms.values():
        arm.pop("_ok")
    cleared = rules_mod.bar_cleared(proof, arms["compiled"]["ci_low"])
    raw_cleared = rules_mod.bar_cleared(proof, arms["raw"]["ci_low"]) if "raw" in arms else None
    state["arms"], state["paired"] = arms, paired
    state["bar"] = {"rule": proof["id"], "metric": proof["bar"]["metric"], "needed_ci_low": proof["bar"]["ci_lower_gte"],
                    "compiled_ci_low": arms["compiled"]["ci_low"], "compiled_cleared": cleared,
                    "raw_ci_low": arms["raw"]["ci_low"] if "raw" in arms else None, "raw_cleared": raw_cleared,
                    "frontier_ratio": "not measured in POC"}
    state["routing"] = ("compiled arm: payouts over $500 may be paid on a verified anchored receipt" if cleared
                        else "human review for payouts over $500 (on_no_open_model)")

    chains = {key: chain_for(job=state[key]["job"], tag=key, fixtures=P["nosana"], rpc=a.rpc)
              for key, _ in batches(state)}
    if rr.get("ok"):
        chains["rerun"] = chain_for(job=rr["job"], tag="rerun", fixtures=P["nosana"], rpc=a.rpc)
    state["chains"] = chains
    spots = {iid: issue.spot_check(rerun_job=rr["job"], rerun_node=chains["rerun"]["node"],
                                   output_sha256=rows[iid]["output_sha256"],
                                   rerun_output_sha256=rerun_rows[iid]["output_sha256"])
             for iid in rr.get("items", [])} if rr.get("ok") else {}
    if rr.get("status") == "pending":  # run g: no second node within the window; the gate holds these items
        spots = {iid: issue.pending_rerun(rr["why"]) for iid in rr["items"]}
        state["rerun_agreement"] = f"pending for {len(spots)} items: {rr['why']}"
    else:
        state["rerun_agreement"] = ({"n": len(spots), "agreed": sum(s["agreed"] for s in spots.values())} if spots
                                    else rr.get("why") or "not run (plan changed after the batch: see docs/phase1b-report.md)")

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
    receipts = {}
    for key, pids in batches(state):  # each receipt is anchored to the job that produced its output
        mine = {iid: x for iid, x in items.items() if pids is None or iid.split("/")[0] in set(pids)}
        receipts.update(issue.anchored_from_result(
            result=results[key], chain=chains[key], job_definition_cid=state[key]["jobdef_cid"],
            inputs_cid=state[key]["inputs_cid"], image=jd_mod.IMAGE, weights=f"{model}@{revision}",
            items=mine, rule=proof, rule_sha256=rules_mod.snapshot_sha256(proof), work_type=task.TASK,
            relay_checks={i: checks[i] for i in mine}, spot_checks={i: v for i, v in spots.items() if i in mine},
            cost_usd_per_item=per_item))
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
    declared = _frontier_receipts(P, run, amt, rules, by_id)
    state["receipts"] = {"anchored": len(receipts), "declared": len(declared)}
    by_item = {iid.split("/")[0]: r for iid, r in receipts.items()}
    state["review_queue"] = gate.write_queue(P["base"] / "review_queue.json", by_item, rules)
    save_state(a.run_id, state)
    for arm in posted_arms:
        x = arms[arm]
        log(f"{arm}: {x['correct']}/{x['n']} = {x['accuracy']} CI [{x['ci_low']}, {x['ci_high']}]; "
            f"> $500 {x['anchored']['correct']}/{x['anchored']['n']}; predicted {x['predicted']}")
    log(f"paired {paired}; bar compiled {cleared} (raw {raw_cleared}); routing: {state['routing']}; "
        f"re-run {state['rerun_agreement']}; receipts {state['receipts']}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["prepare", "nosana", "issue", "frontier"])
    p.add_argument("--run-id", required=True)
    p.add_argument("--market", default=jd_mod.MARKET_3090)
    p.add_argument("--rpc", default=os.environ.get("SOLANA_RPC", DEFAULT_RPC))
    p.add_argument("--cap", type=float, default=PHASE1B_CAP_USD)
    p.add_argument("--baseline", type=float, default=None,
                   help="nosana: settled credits the cap counts from (stored in the run's state)")
    p.add_argument("--max-attempts", type=int, default=4)
    p.add_argument("--batch-only", action="store_true", help="record the batch; skip the re-run")
    p.add_argument("--model", choices=sorted(MODELS), default="7b", help="prepare only: the pinned model")
    p.add_argument("--why", default="", help="prepare only: why this run exists (goes in the pre-registration)")
    p.add_argument("--expectation", default="", help="prepare only: the stated expectation")
    p.add_argument("--sample", choices=["1b", "1e"], default="1b", help="prepare only: run b's 60 or run e's 200")
    p.add_argument("--compiler", choices=["v1", "v2"], default="v1", help="prepare only: the compiled arm's compiler")
    a = p.parse_args(argv)
    {"prepare": cmd_prepare, "nosana": cmd_nosana, "issue": cmd_issue, "frontier": cmd_frontier}[a.cmd](a)


if __name__ == "__main__":
    main()
