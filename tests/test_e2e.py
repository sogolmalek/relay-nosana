"""End-to-end test: relay demo on the mock provider."""
import asyncio
import json
from pathlib import Path

import pytest

from relay_nosana.config import Settings, reset_settings
from relay_nosana.providers.mock import MockRecipient
from relay_nosana.tasks.label import LabelTask
from relay_nosana.tasks.duplicate import DuplicateTask
from relay_nosana.tasks.localize import LocalizeTask
from relay_nosana.calibrate import calibrate
from relay_nosana.route import compute_routing_table, compute_routable_share, save_routing_table
from relay_nosana.run import full_run
from relay_nosana.report.build import build_report


def _mock_data():
    issues = []
    label_cycle = ["bug", "enhancement", "documentation", "question", "bug"]
    for i in range(1, 21):
        lbl = label_cycle[(i - 1) % len(label_cycle)]
        issues.append({
            "number": i,
            "title": f"Issue {i}: {'crash in module' if lbl=='bug' else 'request to improve'}",
            "body": (
                f"{'Error: TypeError at line 42' if lbl=='bug' else 'Feature request for'} "
                f"module{i}.py\nVersion: 2.{i}.0"
            ),
            "state": "CLOSED",
            "createdAt": f"2023-0{(i % 9) + 1}-01T00:00:00Z",
            "closedAt": f"2023-0{(i % 9) + 1}-15T00:00:00Z",
            "repository": "mock/repo",
            "author": {"login": f"user{i}"},
            "labels": {"nodes": [{"name": lbl}]},
            "comments": {"nodes": []},
            "timelineItems": {"nodes": [
                {
                    "__typename": "ClosedEvent",
                    "createdAt": f"2023-0{(i % 9) + 1}-15T00:00:00Z",
                    "closer": {
                        "__typename": "PullRequest",
                        "number": 100 + i,
                        "merged": True,
                        "mergedAt": f"2023-0{(i % 9) + 1}-15T00:00:00Z",
                        "baseRefOid": f"abc{i:04d}",
                        "changedFiles": [f"src/module{i}.py"],
                    },
                }
            ]},
        })
    # Duplicates
    for i in range(21, 26):
        issues.append({
            "number": i,
            "title": f"Same as issue {i - 10}",
            "body": f"Duplicate of issue {i - 10}",
            "state": "CLOSED",
            "createdAt": "2023-06-01T00:00:00Z",
            "closedAt": "2023-06-02T00:00:00Z",
            "repository": "mock/repo",
            "author": {"login": "user"},
            "labels": {"nodes": [{"name": "duplicate"}]},
            "comments": {"nodes": []},
            "timelineItems": {"nodes": [
                {
                    "__typename": "MarkedAsDuplicateEvent",
                    "createdAt": "2023-06-02T00:00:00Z",
                    "canonical": {"number": i - 10},
                }
            ]},
        })

    tree = [f"src/module{i}.py" for i in range(1, 21)] + ["tests/test_module.py", "docs/guide.md"]
    file_trees = {f"mock/repo/abc{i:04d}": tree for i in range(1, 21)}
    return {"issues": issues, "file_trees": file_trees}


@pytest.mark.asyncio
async def test_full_pipeline_mock(tmp_path):
    """Full demo pipeline: calibrate → route → run → report on mock provider."""
    reset_settings()
    data = _mock_data()

    label_map = {
        "bug": "bug", "enhancement": "feature",
        "documentation": "docs", "question": "question",
    }
    tasks = [
        LabelTask(label_map),
        DuplicateTask(candidates_k=3, seed=42),
        LocalizeTask(candidates_k=10),
    ]

    instances_by_task = {t.name: t.build_instances(data) for t in tasks}

    # At least some instances should be built
    total = sum(len(v) for v in instances_by_task.values())
    assert total > 0, "No instances built from mock data"

    recipient = MockRecipient(seed=42)
    run_id = "test-e2e-001"

    # Calibrate on up to 5 instances per task
    cal_result = await calibrate(
        tasks=tasks,
        instances_by_task=instances_by_task,
        recipients=[recipient],
        n_per_task=5,
        seed=42,
        bootstrap=10,
        run_id=run_id,
        all_issues=data["issues"],
        db_path=tmp_path / "ledger.sqlite",
    )

    assert "cells" in cal_result
    assert len(cal_result["cells"]) > 0
    for cell in cal_result["cells"]:
        assert cell["n"] <= 5
        assert cell["parse_fail_rate"] == 0.0, "Mock provider should never fail to parse"

    # Route
    decisions = compute_routing_table(cal_result["cells"], threshold_ratio=0.5)
    instances_count = {k: len(v) for k, v in instances_by_task.items()}
    shares = compute_routable_share(decisions, instances_count)
    # With threshold=0.5 and mock accuracy ~0.75-0.80, Nosana should be eligible
    assert isinstance(shares["raw"], float)
    assert isinstance(shares["compiled"], float)

    rt_path = tmp_path / "routing_table.json"
    save_routing_table(decisions, shares, rt_path)
    assert rt_path.exists()

    # Full run
    cal_ids: set[str] = set()
    for ids in cal_result.get("cal_ids_by_task", {}).values():
        cal_ids.update(ids)

    run_instances = {
        k: [i for i in v if i.id not in cal_ids]
        for k, v in instances_by_task.items()
    }

    run_result = await full_run(
        tasks=tasks,
        instances_by_task=run_instances,
        routing_decisions=decisions,
        recipients_by_id={recipient.id: recipient},
        run_id=run_id,
        all_issues=data["issues"],
        db_path=tmp_path / "ledger.sqlite",
    )
    assert "cells" in run_result

    # Report
    (tmp_path / run_id).mkdir(parents=True, exist_ok=True)
    import shutil
    src = Path(f"out/{run_id}")
    if src.exists():
        shutil.copytree(src, tmp_path / run_id, dirs_exist_ok=True)

    # Write cal and routing data to tmp_path for report
    cal_out = tmp_path / run_id / "calibration.json"
    cal_out.parent.mkdir(parents=True, exist_ok=True)
    cal_out.write_text(json.dumps(cal_result, indent=2))
    shutil.copy(rt_path, tmp_path / run_id / "routing_table.json")

    report_path = build_report(run_id, "mock/repo", out_dir=tmp_path / run_id)
    assert report_path.exists()
    html = report_path.read_text()
    assert "Relay" in html
    assert "mock/repo" in html

    # Verify calibration.json was written
    assert cal_out.exists()
    cal_data = json.loads(cal_out.read_text())
    assert "cells" in cal_data


@pytest.mark.asyncio
async def test_calibration_cells_have_required_fields(tmp_path):
    data = _mock_data()
    tasks = [LabelTask({"bug": "bug", "enhancement": "feature"})]
    instances_by_task = {t.name: t.build_instances(data) for t in tasks}
    recipient = MockRecipient(seed=42)

    cal_result = await calibrate(
        tasks=tasks,
        instances_by_task=instances_by_task,
        recipients=[recipient],
        n_per_task=3,
        seed=42,
        bootstrap=10,
        run_id="test-fields",
        all_issues=data["issues"],
        db_path=tmp_path / "ledger.sqlite",
    )

    for cell in cal_result["cells"]:
        assert "task_type" in cell
        assert "mode" in cell
        assert "recipient_id" in cell
        assert "n" in cell
        assert "mean_score" in cell
        assert "ci_lo" in cell
        assert "ci_hi" in cell
        assert "parse_fail_rate" in cell
        assert "tokens_in_mean" in cell
        assert "latency_p50_ms" in cell
        assert "latency_p95_ms" in cell


@pytest.mark.asyncio
async def test_mock_scores_non_negative(tmp_path):
    """Mock provider scores must be in [0, 1]."""
    data = _mock_data()
    tasks = [
        LabelTask({"bug": "bug", "enhancement": "feature"}),
        DuplicateTask(candidates_k=3, seed=42),
        LocalizeTask(candidates_k=10),
    ]
    instances_by_task = {t.name: t.build_instances(data) for t in tasks}
    recipient = MockRecipient(seed=42)

    cal_result = await calibrate(
        tasks=tasks,
        instances_by_task=instances_by_task,
        recipients=[recipient],
        n_per_task=5,
        seed=42,
        bootstrap=10,
        run_id="test-scores",
        all_issues=data["issues"],
        db_path=tmp_path / "ledger.sqlite",
    )

    for cell in cal_result["cells"]:
        s = cell.get("mean_score")
        if s is not None:
            assert 0.0 <= s <= 1.0
