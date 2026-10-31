from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np

from .compiler.compile import RecipientProfile, build_raw, build_compiled
from .ledger import open_db, log_call
from .providers.base import Recipient
from .tasks.base import TaskInstance, TaskType


def _bootstrap_ci(scores: list[float], n_boot: int = 1000, alpha: float = 0.05) -> tuple[float, float]:
    if not scores:
        return 0.0, 0.0
    arr = np.array(scores)
    rng = np.random.default_rng(42)
    boot = rng.choice(arr, size=(n_boot, len(arr)), replace=True).mean(axis=1)
    lo = float(np.percentile(boot, 100 * alpha / 2))
    hi = float(np.percentile(boot, 100 * (1 - alpha / 2)))
    return lo, hi


def _draw_stratified_sample(
    instances: list[TaskInstance],
    n: int,
    seed: int,
    task_type: str | None = None,
    exclude_issue_numbers: set[int] | None = None,
) -> tuple[list[TaskInstance], list[TaskInstance]]:
    """
    Return (calibration_sample, rest).

    Dev-split issues are excluded from the pool before sampling so that
    calibration size changes (50→100) never pull in dev instances.
    """
    pool = sorted(
        (i for i in instances if i.issue_number not in (exclude_issue_numbers or set())),
        key=lambda i: (i.issue_number, i.id),  # input-order independent draw
    )
    rng = random.Random(seed)
    rng.shuffle(pool)
    cal = pool[:n]
    rest = pool[n:]
    return cal, rest


async def run_cell(
    instances: list[TaskInstance],
    recipient: Recipient,
    task: TaskType,
    mode: str,
    run_id: str,
    phase: str,
    schema: dict,
    all_issues: list[dict] | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Run all instances for one (recipient, mode) cell. Returns cell stats."""
    scores: list[float] = []
    scores_given_recall: list[float] = []  # localize: hit@5 conditional on candidate_recall
    candidate_recalls: list[bool] = []     # localize: retrieval hit/miss
    parse_oks: list[bool] = []
    tokens_in_list: list[int] = []
    tokens_out_list: list[int] = []
    latencies: list[float] = []

    # Detect if this is the localize task (has score_given_recall method)
    is_localize = hasattr(task, "score_given_recall")

    with open_db(db_path) as conn:
        for inst in instances:
            if mode == "raw":
                prompt = build_raw(inst, recipient.profile)
            else:
                prompt = build_compiled(inst, recipient.profile, schema, all_issues)

            completion = await recipient.complete(prompt, schema)

            score: float | None = None
            if completion.parsed is not None:
                score = task.score(completion.parsed, inst.ground_truth)
                scores.append(score)

                if is_localize:
                    cand_recall = inst.meta.get("candidate_recall", True)
                    candidate_recalls.append(cand_recall)
                    cond = task.score_given_recall(completion.parsed, inst.ground_truth, inst)
                    if cond is not None:
                        scores_given_recall.append(cond)

            parse_oks.append(completion.parse_ok)
            tokens_in_list.append(completion.tokens_in)
            tokens_out_list.append(completion.tokens_out)
            latencies.append(completion.latency_ms)

            log_call(
                conn,
                run_id=run_id,
                phase=phase,
                task_type=task.name,
                instance_id=inst.id,
                recipient_id=recipient.id,
                mode=mode,
                tokens_in=completion.tokens_in,
                tokens_out=completion.tokens_out,
                latency_ms=completion.latency_ms,
                cost_usd=0.0,
                score=score,
                parse_ok=completion.parse_ok,
                error=completion.error,
            )

    ci_lo, ci_hi = _bootstrap_ci(scores)
    latencies_arr = np.array(latencies) if latencies else np.array([0.0])

    cell: dict[str, Any] = {
        "recipient_id": recipient.id,
        "task_type": task.name,
        "mode": mode,
        "n": len(instances),
        "mean_score": float(np.mean(scores)) if scores else None,
        "ci_lo": ci_lo,
        "ci_hi": ci_hi,
        "parse_fail_rate": 1.0 - (sum(parse_oks) / len(parse_oks)) if parse_oks else 0.0,
        "tokens_in_mean": float(np.mean(tokens_in_list)) if tokens_in_list else 0,
        "tokens_out_mean": float(np.mean(tokens_out_list)) if tokens_out_list else 0,
        "latency_p50_ms": float(np.percentile(latencies_arr, 50)),
        "latency_p95_ms": float(np.percentile(latencies_arr, 95)),
    }

    # Localize-specific breakdown: retrieval miss vs model miss
    if is_localize and candidate_recalls:
        n_with_recall = sum(candidate_recalls)
        retrieval_recall_rate = n_with_recall / len(candidate_recalls)
        cell["candidate_recall_rate"] = retrieval_recall_rate
        # hit@5 conditional on retrieval success
        cond_ci_lo, cond_ci_hi = _bootstrap_ci(scores_given_recall)
        cell["hit_at_5_given_recall"] = float(np.mean(scores_given_recall)) if scores_given_recall else None
        cell["hit_at_5_given_recall_ci_lo"] = cond_ci_lo
        cell["hit_at_5_given_recall_ci_hi"] = cond_ci_hi
        # Decomposition note: hit@5_overall ≈ retrieval_recall × hit@5_given_recall

    return cell


async def calibrate(
    tasks: list[TaskType],
    instances_by_task: dict[str, list[TaskInstance]],
    recipients: list[Recipient],
    n_per_task: int,
    seed: int,
    bootstrap: int,
    run_id: str | None = None,
    all_issues: list[dict] | None = None,
    db_path: Path | None = None,
    dev_issue_numbers: set[int] | None = None,
) -> dict[str, Any]:
    """
    Run the full calibration matrix.
    Returns calibration results and calibration sample IDs (to hold out from routing).
    """
    if run_id is None:
        run_id = f"cal-{uuid.uuid4().hex[:8]}"

    results: list[dict] = []
    cal_ids_by_task: dict[str, set[str]] = {}

    for task in tasks:
        instances = instances_by_task.get(task.name, [])
        cal_sample, _ = _draw_stratified_sample(
            instances, min(n_per_task, len(instances)), seed, task.name,
            exclude_issue_numbers=dev_issue_numbers,
        )
        cal_ids_by_task[task.name] = {inst.id for inst in cal_sample}
        schema = task.output_schema()

        for recipient in recipients:
            for mode in ("raw", "compiled"):
                cell = await run_cell(
                    cal_sample, recipient, task, mode, run_id, "calibrate",
                    schema, all_issues, db_path,
                )
                results.append(cell)

    out_dir = Path(f"out/{run_id}")
    out_dir.mkdir(parents=True, exist_ok=True)
    cal_path = out_dir / "calibration.json"
    cal_path.write_text(json.dumps({"run_id": run_id, "cells": results}, indent=2))

    return {
        "run_id": run_id,
        "cells": results,
        "cal_ids_by_task": {k: list(v) for k, v in cal_ids_by_task.items()},
    }
