from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .calibrate import run_cell
from .ledger import open_db
from .providers.base import Recipient
from .route import RoutingDecision
from .tasks.base import TaskInstance, TaskType


async def full_run(
    tasks: list[TaskType],
    instances_by_task: dict[str, list[TaskInstance]],
    routing_decisions: list[RoutingDecision],
    recipients_by_id: dict[str, Recipient],
    open_instances_by_task: dict[str, list[TaskInstance]] | None = None,
    run_id: str | None = None,
    all_issues: list[dict] | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Apply routing table to all non-calibration instances.
    Also run on open issues (no ground truth) for suggestions.
    """
    if run_id is None:
        run_id = f"run-{uuid.uuid4().hex[:8]}"

    out_dir = Path(f"out/{run_id}")
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    decisions_compiled = {d.task_type: d for d in routing_decisions if d.mode == "compiled"}

    task_by_name = {t.name: t for t in tasks}

    for task_name, instances in instances_by_task.items():
        task = task_by_name.get(task_name)
        if not task:
            continue
        decision = decisions_compiled.get(task_name)
        if not decision or not decision.chosen_recipient_id:
            continue

        recipient = recipients_by_id.get(decision.chosen_recipient_id)
        if not recipient:
            continue

        schema = task.output_schema()
        mode = "compiled"

        cell = await run_cell(
            instances, recipient, task, mode, run_id, "run",
            schema, all_issues, db_path,
        )
        results.append(cell)

    # Suggestions for open issues
    suggestions: list[dict] = []
    if open_instances_by_task:
        for task_name, open_insts in open_instances_by_task.items():
            task = task_by_name.get(task_name)
            if not task:
                continue
            decision = decisions_compiled.get(task_name)
            if not decision or not decision.chosen_recipient_id:
                continue
            recipient = recipients_by_id.get(decision.chosen_recipient_id)
            if not recipient:
                continue
            schema = task.output_schema()
            for inst in open_insts:
                from .compiler.compile import build_compiled
                prompt = build_compiled(inst, recipient.profile, schema, all_issues)
                completion = await recipient.complete(prompt, schema)
                if completion.parsed:
                    suggestions.append({
                        "issue_number": inst.issue_number,
                        "task_type": task_name,
                        "prediction": completion.parsed,
                    })

    suggestions_path = out_dir / "suggestions.jsonl"
    with open(suggestions_path, "w") as f:
        for s in suggestions:
            f.write(json.dumps(s) + "\n")

    run_summary = {"run_id": run_id, "cells": results}
    (out_dir / "run_summary.json").write_text(json.dumps(run_summary, indent=2))
    return run_summary
