"""
Tests for the dev-split / calibration sampling contract:
  - Dev split is drawn FIRST and persisted; calibration excludes those issue numbers.
  - Dev IDs are identical when calibration n changes from 50 to 100.
  - Leakage guard strips all "duplicate of #N" patterns unconditionally.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from relay_nosana.calibrate import _draw_stratified_sample
from relay_nosana.splits import load_or_create_dev_split, create_dev_split, load_dev_numbers
from relay_nosana.tasks.base import TaskInstance
from relay_nosana.tasks.ground_truth import remove_resolution_references, build_safe_input


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pr_closed_issues(n: int) -> list[dict]:
    return [{"number": i, "changedFiles": ["src/file.py"]} for i in range(1, n + 1)]


def _make_instances(n: int) -> list[TaskInstance]:
    return [
        TaskInstance(
            id=str(i),
            issue_number=i,
            task_type="localize",
            input_text=f"issue {i}",
            candidates=[],
            ground_truth=[],
        )
        for i in range(1, n + 1)
    ]


# ---------------------------------------------------------------------------
# Dev split creation and persistence
# ---------------------------------------------------------------------------

def test_create_dev_split_saves_to_disk(tmp_path):
    issues = _make_pr_closed_issues(300)
    dev_path = tmp_path / "dev.json"
    dev_nums = create_dev_split(issues, dev_path, seed=99, n=100)

    assert dev_path.exists()
    data = json.loads(dev_path.read_text())
    assert set(data["issue_numbers"]) == dev_nums
    assert data["seed"] == 99
    assert data["n"] == 100
    assert len(dev_nums) == 100


def test_load_or_create_returns_same_on_second_call(tmp_path):
    issues = _make_pr_closed_issues(300)
    dev_path = tmp_path / "dev.json"

    first = load_or_create_dev_split(issues, dev_path=dev_path, seed=99, n=100)
    second = load_or_create_dev_split(issues, dev_path=dev_path, seed=99, n=100)
    assert first == second


# ---------------------------------------------------------------------------
# Dev split stable when calibration size changes
# ---------------------------------------------------------------------------

def test_dev_split_identical_across_calibration_sizes(tmp_path):
    """
    Core contract: dev IDs loaded from disk must be identical whether
    calibration draws 50 or 100 instances.
    """
    issues = _make_pr_closed_issues(300)
    dev_path = tmp_path / "dev.json"

    # Step 1: create dev split (done once before any calibration)
    dev_nums = load_or_create_dev_split(issues, dev_path=dev_path, seed=99, n=100)
    assert len(dev_nums) == 100

    instances = _make_instances(300)

    # Calibration n=50
    cal_50, _ = _draw_stratified_sample(instances, n=50, seed=42, exclude_issue_numbers=dev_nums)
    # Calibration n=100
    cal_100, _ = _draw_stratified_sample(instances, n=100, seed=42, exclude_issue_numbers=dev_nums)

    # Dev IDs unchanged by either calibration draw
    dev_nums_reloaded = load_dev_numbers(dev_path)
    assert dev_nums == dev_nums_reloaded

    # No overlap with either calibration sample
    cal_50_ids = {i.issue_number for i in cal_50}
    cal_100_ids = {i.issue_number for i in cal_100}
    assert not (dev_nums & cal_50_ids), "Dev leaked into cal-50"
    assert not (dev_nums & cal_100_ids), "Dev leaked into cal-100"

    # The first 50 drawn for cal-100 equal the cal-50 draw (same pool, same seed)
    assert {i.id for i in cal_50} == {i.id for i in cal_100[:50]}


def test_calibration_does_not_touch_dev_file(tmp_path):
    """Calibration sampling must never write to dev.json."""
    issues = _make_pr_closed_issues(200)
    dev_path = tmp_path / "dev.json"
    dev_nums = create_dev_split(issues, dev_path, seed=99, n=100)
    mtime_before = dev_path.stat().st_mtime

    instances = _make_instances(200)
    _draw_stratified_sample(instances, n=50, seed=42, exclude_issue_numbers=dev_nums)

    assert dev_path.stat().st_mtime == mtime_before, "Calibration wrote to dev.json"


def test_dev_split_independent_of_input_order(tmp_path):
    issues = _make_pr_closed_issues(300)
    a = create_dev_split(issues, tmp_path / "a.json", seed=99, n=100)
    b = create_dev_split(list(reversed(issues)), tmp_path / "b.json", seed=99, n=100)
    assert a == b


def test_calibration_sample_independent_of_input_order():
    instances = _make_instances(300)
    dev = set(range(1, 301, 3))
    cal_a, _ = _draw_stratified_sample(instances, n=50, seed=42, exclude_issue_numbers=dev)
    cal_b, _ = _draw_stratified_sample(
        list(reversed(instances)), n=50, seed=42, exclude_issue_numbers=dev
    )
    assert [i.id for i in cal_a] == [i.id for i in cal_b]


def test_different_seeds_give_different_dev_splits(tmp_path):
    issues = _make_pr_closed_issues(300)
    a = create_dev_split(issues, tmp_path / "a.json", seed=99, n=50)
    b = create_dev_split(issues, tmp_path / "b.json", seed=42, n=50)
    assert a != b


# ---------------------------------------------------------------------------
# Leakage guard: "duplicate of #N" stripped from body unconditionally
# ---------------------------------------------------------------------------

def test_dup_of_stripped_without_canonical():
    text = "This is duplicate of #42 as noted above."
    result = remove_resolution_references(text)
    assert "duplicate of #42" not in result.lower()
    assert "[DUP_REMOVED]" in result


def test_dup_of_stripped_case_insensitive():
    text = "Duplicate of #99 — closing this."
    result = remove_resolution_references(text)
    assert "[DUP_REMOVED]" in result


def test_dup_of_stripped_with_canonical():
    text = "This is dup of #7 per maintainer."
    result = remove_resolution_references(text, canonical_issue=7)
    assert "dup of #7" not in result.lower()
    assert "#7" not in result


def test_build_safe_input_strips_dup_in_body():
    issue = {
        "title": "Login bug",
        "body": "This is duplicate of #55, closing.",
        "comments": {"nodes": []},
    }
    text = build_safe_input(issue, resolution_ts=None)
    assert "duplicate of #55" not in text.lower()
    assert "[DUP_REMOVED]" in text
