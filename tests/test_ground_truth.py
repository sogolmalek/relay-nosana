"""Ground-truth extraction tests against recorded GitHub fixtures."""
import json
from pathlib import Path

import pytest

from relay_nosana.tasks.label import LabelTask
from relay_nosana.tasks.duplicate import DuplicateTask
from relay_nosana.tasks.localize import LocalizeTask

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    p = FIXTURES / name
    return json.loads(p.read_text())


# ---------------------------------------------------------------------------
# Label ground truth
# ---------------------------------------------------------------------------

def test_label_extracts_mapped_label():
    data = _load_fixture("label_issues.json")
    task = LabelTask({"bug": "bug", "enhancement": "feature", "documentation": "docs"})
    instances = task.build_instances(data)
    assert len(instances) > 0
    for inst in instances:
        assert inst.ground_truth in ("bug", "feature", "docs", "question", "other")


def test_label_skips_unlabeled_issues():
    data = {"issues": [
        {
            "number": 1, "title": "foo", "body": "bar", "state": "CLOSED",
            "createdAt": "2023-01-01T00:00:00Z", "closedAt": "2023-01-02T00:00:00Z",
            "labels": {"nodes": []},
            "comments": {"nodes": []},
            "timelineItems": {"nodes": []},
        }
    ]}
    task = LabelTask({"bug": "bug"})
    instances = task.build_instances(data)
    assert instances == []


def test_label_skips_multi_label_issues():
    """Issues with >1 mapped label are skipped (ambiguous truth)."""
    data = {"issues": [
        {
            "number": 2, "title": "Two labels", "body": "...", "state": "CLOSED",
            "createdAt": "2023-01-01T00:00:00Z", "closedAt": "2023-01-02T00:00:00Z",
            "labels": {"nodes": [{"name": "bug"}, {"name": "enhancement"}]},
            "comments": {"nodes": []},
            "timelineItems": {"nodes": []},
        }
    ]}
    task = LabelTask({"bug": "bug", "enhancement": "feature"})
    instances = task.build_instances(data)
    assert instances == []


# ---------------------------------------------------------------------------
# Duplicate ground truth
# ---------------------------------------------------------------------------

def test_duplicate_extracts_canonical_from_timeline():
    data = _load_fixture("duplicate_issues.json")
    task = DuplicateTask(candidates_k=3, seed=42)
    instances = task.build_instances(data)
    positives = [i for i in instances if i.ground_truth != "none"]
    assert len(positives) > 0
    for inst in positives:
        assert isinstance(inst.ground_truth, int)
        # Canonical must be in candidates
        assert inst.ground_truth in inst.candidates


def test_duplicate_negatives_equal_positives():
    data = _load_fixture("duplicate_issues.json")
    task = DuplicateTask(candidates_k=3, seed=42)
    instances = task.build_instances(data)
    positives = [i for i in instances if i.ground_truth != "none"]
    negatives = [i for i in instances if i.ground_truth == "none"]
    assert len(negatives) == len(positives)


# ---------------------------------------------------------------------------
# Localize ground truth
# ---------------------------------------------------------------------------

def test_localize_extracts_pr_files():
    data = _load_fixture("localize_issues.json")
    task = LocalizeTask(candidates_k=10)
    instances = task.build_instances(data)
    assert len(instances) > 0
    for inst in instances:
        assert isinstance(inst.ground_truth, list)
        assert len(inst.ground_truth) > 0


def test_localize_excludes_test_and_doc_files():
    issue = {
        "number": 1, "title": "Bug", "body": "crash", "state": "CLOSED",
        "createdAt": "2023-01-01T00:00:00Z", "closedAt": "2023-01-10T00:00:00Z",
        "repository": "test/repo",
        "labels": {"nodes": []},
        "comments": {"nodes": []},
        "timelineItems": {"nodes": [{
            "__typename": "ClosedEvent",
            "closer": {
                "__typename": "PullRequest",
                "number": 100,
                "merged": True,
                "mergedAt": "2023-01-10T00:00:00Z",
                "baseRefOid": "abc",
                "changedFiles": ["src/foo.py", "tests/test_foo.py", "docs/guide.md"],
            }
        }]},
    }
    data = {
        "issues": [issue],
        "file_trees": {"test/repo/abc": ["src/foo.py", "tests/test_foo.py", "docs/guide.md", "src/bar.py"]},
    }
    task = LocalizeTask(exclude_globs=["tests/**", "docs/**", "*.md"], candidates_k=10)
    instances = task.build_instances(data)
    assert len(instances) == 1
    inst = instances[0]
    # Truth should only contain src/foo.py (not test or doc files)
    assert "src/foo.py" in inst.ground_truth
    assert "tests/test_foo.py" not in inst.ground_truth
    assert "docs/guide.md" not in inst.ground_truth
