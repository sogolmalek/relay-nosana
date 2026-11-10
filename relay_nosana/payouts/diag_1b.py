"""Phase 1b diagnostic job: 4 of the exact Phase 1 input lines through batch_runner/diag.py on Nosana.

    python -m relay_nosana.payouts.diag_1b run     # one small job on a known-good node (credits)
    python -m relay_nosana.payouts.diag_1b report  # read the node-pinned result into diag_report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from relay_verify.outputs import stdout_lines
from relay_verify.solana import DEFAULT_RPC

from ..lane import Budget, log, run_batch
from ..nosana import jobdef as jd_mod
from ..nosana import nodes as nodes_mod
from ..nosana.jobs import Jobs, idle_nodes
from . import task
from .run_poc import ROOT, db

RUN_ID = "diag-2026-11-10"
SOURCE_RUN = "payouts-2026-11-10"
ITEMS = ["P-0104", "P-0106", "P-0107", "P-0110"]  # ok (obvious approve), seller hold, duplicate, stale price
PHASE1B_BASELINE = 3.405  # settled credits when Phase 1b started (Phase 1 report)
BASE = ROOT / "runs" / RUN_ID


def lines() -> list[dict]:
    src = [json.loads(x) for x in (ROOT / "runs" / SOURCE_RUN / "inputs.jsonl").read_text().splitlines()]
    return [x for x in src if x["item_id"] in ITEMS]


def build():
    ls = lines()
    raw = jd_mod.inputs_bytes(ls)
    return jd_mod.build(ls, task.OUTPUT_SCHEMA, RUN_ID, "diag", task.TASK, phase="1b-diag", deadline_s=1200,
                        runner_path=ROOT / "batch_runner" / "diag.py", results={"done": "RELAY_DIAG_DONE=(1)"},
                        extra_env={"RELAY_EXPECT_INPUTS_SHA256": hashlib.sha256(raw).hexdigest()})


def cmd_run(a) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    conn = db(RUN_ID)
    jobs = Jobs()
    budget = Budget(jobs, a.cap)
    budget.baseline = PHASE1B_BASELINE
    tried = {r["node"] for r in conn.execute("SELECT node FROM jobs WHERE run_id=? AND node IS NOT NULL", (RUN_ID,))}

    def node_for(attempt: int):
        node, why = nodes_mod.pick(a.market, idle_nodes(a.market, set()), tried)
        log(f"diag attempt {attempt}: {node or 'open market'} ({why})")
        return node

    jd, _, inputs_cid = build()
    out = run_batch(conn=conn, run_id=RUN_ID, tag="diag", jobdef=jd, market=a.market, rpc=a.rpc, jobs=jobs,
                    budget=budget, fixtures=BASE / "nosana", image=jd_mod.IMAGE,
                    weights=f"{jd_mod.MODEL}@{jd_mod.REVISION}", inputs_cid=inputs_cid, node_for_attempt=node_for,
                    max_attempts=3, queue_wait_s=900, run_wait_s=1500)
    (BASE / "state.json").write_text(json.dumps({"ok": out.ok, "job": out.job, "result_file": out.result_file,
                                                  "jobdef_cid": out.jobdef_cid, "account": out.account,
                                                  "spent_phase1b_usd": budget.spent()}, indent=2) + "\n")
    log(f"diag {'OK' if out.ok else 'FAILED'}: {out.job}; Phase 1b spend ${budget.spent():.3f}")


def cmd_report(a) -> None:
    st = json.loads((BASE / "state.json").read_text())
    res = json.loads((BASE / "nosana" / st["result_file"]).read_text())
    rep: dict = {"job": st["job"], "node": (st.get("account") or {}).get("node"), "lines": {}}
    for line in stdout_lines(res):
        if line.startswith("RELAY_DIAG "):
            _, tag, body = line.split(" ", 2)
            obj = json.loads(body)
            if tag in ("inputs", "engine"):
                rep[tag] = obj
            else:
                rep["lines"].setdefault(obj["item_id"], {})[tag] = obj
    (BASE / "diag_report.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n")
    log(f"inputs {rep.get('inputs', {}).get('match')}; engine {rep.get('engine')}")
    for iid, d in rep["lines"].items():
        log(f"{iid}: prompt {d['prompt']['tokens']} tok | guided {d['chat_guided']['text']!r} | "
            f"free {d['chat_free']['text'][:120]!r} | generate {d['generate_guided']['text']!r}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("cmd", choices=["run", "report"])
    p.add_argument("--market", default=jd_mod.MARKET_3090)
    p.add_argument("--rpc", default=os.environ.get("SOLANA_RPC", DEFAULT_RPC))
    p.add_argument("--cap", type=float, default=2.0)
    a = p.parse_args(argv)
    {"run": cmd_run, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
