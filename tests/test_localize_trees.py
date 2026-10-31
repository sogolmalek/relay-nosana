"""
LocalizeTask must use the per-instance base-commit tree, never a fallback:
  - missing tree, HEAD/"default"-only trees, or a missing repository raise
  - truth is restricted to the listing's scope (src/ for src-only listings)
  - plain BM25 is the default retrieval
"""
from __future__ import annotations

import pytest

from relay_nosana.tasks.localize import LocalizeTask


def _issue(changed: list[str], repository: str | None = "o/r", base_ref: str | None = "sha1") -> dict:
    issue = {
        "number": 7, "title": "crash in foo", "body": "foo breaks", "state": "CLOSED",
        "createdAt": "2023-01-01T00:00:00Z", "closedAt": "2023-01-10T00:00:00Z",
        "labels": {"nodes": []}, "comments": {"nodes": []},
        "timelineItems": {"nodes": [{
            "__typename": "ClosedEvent",
            "closer": {
                "__typename": "PullRequest", "number": 70, "merged": True,
                "mergedAt": "2023-01-10T00:00:00Z",
                "baseRefOid": base_ref, "changedFiles": changed,
            },
        }]},
    }
    if repository is not None:
        issue["repository"] = repository
    return issue


SRC_TREE = ["src/pkg/foo.py", "src/pkg/bar.py", "src/pkg/baz.py"]


def test_missing_base_tree_raises():
    data = {"issues": [_issue(["src/pkg/foo.py"])], "file_trees": {"o/r/othersha": SRC_TREE}}
    with pytest.raises(ValueError, match="missing"):
        LocalizeTask().build_instances(data)


def test_default_key_is_not_a_fallback():
    data = {"issues": [_issue(["src/pkg/foo.py"])], "file_trees": {"default": SRC_TREE}}
    with pytest.raises(ValueError, match="missing"):
        LocalizeTask().build_instances(data)


def test_empty_base_tree_raises():
    data = {"issues": [_issue(["src/pkg/foo.py"])], "file_trees": {"o/r/sha1": []}}
    with pytest.raises(ValueError, match="empty"):
        LocalizeTask().build_instances(data)


@pytest.mark.parametrize("repository,base_ref", [(None, "sha1"), ("o/r", None)])
def test_missing_repository_or_base_ref_raises(repository, base_ref):
    data = {"issues": [_issue(["src/pkg/foo.py"], repository, base_ref)], "file_trees": {"o/r/sha1": SRC_TREE}}
    with pytest.raises(ValueError, match="required"):
        LocalizeTask().build_instances(data)


def test_truth_restricted_to_src_scope():
    changed = ["src/pkg/foo.py", "examples/run_foo.py", "utils/check.py"]
    data = {"issues": [_issue(changed)], "file_trees": {"o/r/sha1": SRC_TREE}}
    (inst,) = LocalizeTask().build_instances(data)
    assert inst.ground_truth == ["src/pkg/foo.py"]
    assert inst.meta["base_ref"] == "sha1"


def test_instance_skipped_when_no_truth_in_scope():
    data = {"issues": [_issue(["examples/run_foo.py"])], "file_trees": {"o/r/sha1": SRC_TREE}}
    assert LocalizeTask().build_instances(data) == []


def test_root_listing_keeps_all_truth():
    tree = ["pkg/foo.py", "examples/run_foo.py"]  # pre-src/ commit: root listing
    data = {"issues": [_issue(["pkg/foo.py", "examples/run_foo.py"])], "file_trees": {"o/r/sha1": tree}}
    (inst,) = LocalizeTask().build_instances(data)
    assert inst.ground_truth == ["pkg/foo.py", "examples/run_foo.py"]


def test_plain_retrieval_is_default():
    data = {"issues": [_issue(["src/pkg/foo.py"])], "file_trees": {"o/r/sha1": SRC_TREE}}
    (inst,) = LocalizeTask().build_instances(data)
    assert inst.meta["retrieval"] == "plain"
    (inst_rrf,) = LocalizeTask(retrieval="rrf").build_instances(data)
    assert inst_rrf.meta["retrieval"] == "rrf"
