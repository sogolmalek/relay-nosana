"""Build the replay data for the POC app (docs/app/runs/) from committed runs (Phase 2a, replay mode).

    .venv/bin/python scripts/build_app_data.py

Copies each run's receipts and review queue byte for byte, and writes run.json: the payouts, the decisions in
force, the rules, each payout's lane, ground truth and model output, the jobs (with their post and finish
transactions), and the measured accuracy against the bar. Nothing here is recomputed by the browser's
verifier: the browser checks the receipts against Solana and IPFS itself.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from relay_nosana import rules as rules_mod  # noqa: E402
from relay_verify.outputs import parse_outputs  # noqa: E402

APP = ROOT / "docs" / "app" / "runs"
RUNS = {
    "payouts-2026-11-10": {"title": "Phase 1: one real payout run", "date": "2026-11-10",
                           "design": "30 payouts, every seller hold in the context (Phase 1 prompt)",
                           "arm": None, "report": "docs/phase1-report.md"},
    "payouts-2026-11-10b": {"title": "Phase 1b, run b: raw vs compiled context (pre-registered)", "date": "2026-11-10",
                            "design": "60 payouts, two arms in one job; receipts for the compiled arm",
                            "arm": "compiled", "report": "docs/phase1b-report.md"},
}


def _job(state: dict, key: str, cost: float | None) -> dict:
    c, a = state["chains"][key], state[key]["account"]
    return {"tag": key, "job": c["job"], "node": c["node"], "market": c["market"], "post_tx": c["post_tx"],
            "finish_tx": c["finish_tx"], "post_ix": c["post_ix"], "job_definition_cid": c["ipfs_job"],
            "result_cid": c["ipfs_result"], "listed_at": c["listed_at"], "finished_at": c["finished_at"],
            "price_per_s": a.get("price"), "cost_usd": cost}


def _costs(run_dir: Path) -> dict[str, float]:
    f = run_dir / "ledger" / "jobs.json"
    if not f.exists():
        return {}
    return {r["job"]: round(max(0.0, (r["credits_before"] or 0) - (r["credits_after"] or 0)), 4)
            for r in json.loads(f.read_text()) if r.get("credits_after") is not None}


def build(run_id: str, meta: dict) -> dict:
    src = ROOT / "runs" / run_id
    dst = APP / run_id
    state = json.loads((src / "state.json").read_text())
    run = json.loads((src / "payout_run.json").read_text())
    nos = src / "nosana"
    keys = [k for k in state if k == "batch" or (k.startswith("batch") and k[5:].isdigit())]
    rows = {}
    for k in sorted(keys):
        rows.update(parse_outputs(json.loads((nos / state[k]["result_file"]).read_text()))[0])
    rerun_rows = parse_outputs(json.loads((nos / state["rerun"]["result_file"]).read_text()))[0]
    declared = {}
    if (src / "declared.jsonl").exists():
        declared = {r["item_id"]: r for r in map(json.loads, (src / "declared.jsonl").read_text().splitlines())}
    keep = (dst / "payouts.json").read_bytes() if (dst / "payouts.json").exists() else None  # devnet record
    if dst.exists():
        shutil.rmtree(dst)
    (dst / "receipts").mkdir(parents=True)
    receipts = {}
    for p in sorted((src / "receipts").glob("*.json")):
        shutil.copyfile(p, dst / "receipts" / p.name)
        receipts[p.stem] = json.loads(p.read_text())
    shutil.copyfile(src / "review_queue.json", dst / "review_queue.json")
    rules = rules_mod.load()
    payouts = []
    for p in run["payouts"]:
        pid = p["payout_id"]
        iid = f"{pid}/{meta['arm']}" if meta["arm"] else pid
        r = receipts.get(pid)
        out = {"payout_id": pid, "seller_id": p["seller_id"], "invoice_id": p["invoice_id"],
               "amount_usd": p["amount_cents"] / 100, "lane": state["lanes"][pid], "truth": run["truth"][pid],
               "open_model_output": rows[iid]["output"],
               "rerun_output": rerun_rows[iid]["output"] if iid in rerun_rows else None,
               "declared_output": (declared.get(pid) or {}).get("output"),
               "receipt": f"receipts/{pid}.json" if r else None, "grade": r["grade"] if r else None,
               "notes": [{k: v for k, v in n.items() if k != "intent"} for n in p.get("notes", [])]}
        if meta["arm"]:
            out["raw_output"] = rows[f"{pid}/raw"]["output"]
        payouts.append(out)
    costs = _costs(src)
    if meta["arm"]:
        acc, bar = state["arms"]["compiled"], state["bar"]
        measured = {"model": "Qwen2.5-7B-Instruct (compiled arm)", "correct": acc["correct"], "n": acc["n"],
                    "accuracy": acc["accuracy"], "ci_low": acc["ci_low"], "ci_high": acc["ci_high"],
                    "over_500": acc["anchored"], "raw": {k: state["arms"]["raw"][k] for k in
                                                         ("correct", "n", "accuracy", "ci_low", "ci_high")}}
    else:
        key = next(k for k in state["accuracy"] if k.startswith("nosana:") and "anchored" not in k)
        acc = state["accuracy"][key]
        measured = {"model": "Qwen2.5-7B-Instruct", "correct": acc["correct"], "n": acc["n"],
                    "accuracy": acc["accuracy"], "ci_low": acc["ci_low"], "ci_high": acc["ci_high"],
                    "over_500": state["accuracy"][key + " (anchored lane, > $500)"],
                    "declared_lane": state["accuracy"].get("claude-code:claude-opus-5-5")}
    data = {
        "run_id": run_id, **{k: v for k, v in meta.items() if k != "arm"}, "mode": "replay", "synthetic": True,
        "note": run.get("note"), "run_date": run["run_date"], "decisions": run["decisions"],
        "rules": rules, "model": receipts[next(k for k, v in receipts.items() if v["grade"] == "anchored")]["model"],
        "jobs": [_job(state, k, costs.get(state[k]["job"])) for k in sorted(keys) + ["rerun"]],
        "nosana_spent_usd": state.get("nosana_spent_usd"),
        "bar": {"needed_ci_low": state["bar"]["needed_ci_low"], "metric": state["bar"]["metric"],
                "cleared": state["bar"].get("cleared", state["bar"].get("compiled_cleared")), "measured": measured},
        "rerun_agreement": state["rerun_agreement"],
        "rerun_items": [x.split("/")[0] for x in state["rerun"]["items"]],
        "verify_cli": state.get("verify"),
        "columns": state.get("columns"),  # run e design: raw / compiled v2 / rules only / rules + regex
        "caveat": meta.get("caveat"),
        "payouts": payouts,
    }
    if keep is not None:
        (dst / "payouts.json").write_bytes(keep)
    (dst / "run.json").write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    return {"run_id": run_id, "title": meta["title"], "date": meta["date"], "payouts": len(payouts),
            "receipts": len(receipts)}


def main():
    APP.mkdir(parents=True, exist_ok=True)
    index = [build(r, m) for r, m in RUNS.items()]
    (APP / "index.json").write_text(json.dumps(index, indent=1) + "\n")
    print(json.dumps(index, indent=1))


if __name__ == "__main__":
    main()
