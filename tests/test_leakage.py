"""Tests for leakage guards (section 6.4): must assert truth never appears in inputs."""
import pytest
from relay_nosana.tasks.ground_truth import (
    build_safe_input,
    assert_no_leakage,
    filter_comments_before,
    remove_resolution_references,
)
from relay_nosana.tasks.label import LabelTask
from relay_nosana.tasks.duplicate import DuplicateTask
from relay_nosana.tasks.localize import LocalizeTask


# ---------------------------------------------------------------------------
# Unit tests for ground_truth helpers
# ---------------------------------------------------------------------------

def test_filter_comments_before_excludes_post_resolution():
    comments = [
        {"body": "early comment", "createdAt": "2023-01-01T10:00:00Z"},
        {"body": "late comment mentioning fix", "createdAt": "2023-01-05T10:00:00Z"},
    ]
    filtered = filter_comments_before(comments, "2023-01-03T00:00:00Z")
    assert len(filtered) == 1
    assert filtered[0]["body"] == "early comment"


def test_filter_comments_keeps_all_if_no_cutoff():
    comments = [
        {"body": "c1", "createdAt": "2023-01-01T00:00:00Z"},
        {"body": "c2", "createdAt": "2023-01-02T00:00:00Z"},
    ]
    assert len(filter_comments_before(comments, None)) == 2


def test_remove_resolution_references_fixes():
    text = "Closes #123 and fixes #456"
    cleaned = remove_resolution_references(text, closing_pr=None)
    assert "#123" not in cleaned
    assert "#456" not in cleaned


def test_remove_resolution_references_pr():
    text = "See pull request #789 for the fix"
    cleaned = remove_resolution_references(text, closing_pr=789)
    assert "#789" not in cleaned


def test_remove_resolution_references_dup_of():
    text = "Duplicate of #42 as noted above"
    cleaned = remove_resolution_references(text, canonical_issue=42)
    assert "duplicate of #42" not in cleaned.lower()
    assert "#42" not in cleaned


def test_assert_no_leakage_passes():
    text = "Title: crashes\n\nBody: Something went wrong unexpectedly."
    assert_no_leakage(text, truth_label="feature")   # "feature" absent → passes
    assert_no_leakage(text, closing_pr=999)           # "#999" absent → passes
    assert_no_leakage(text, canonical_issue=55)       # "#55" absent → passes


def test_assert_no_leakage_catches_label():
    text = "This is a bug issue"
    with pytest.raises(AssertionError, match="leaked"):
        assert_no_leakage(text, truth_label="bug")


def test_assert_no_leakage_catches_canonical_issue():
    text = "duplicate of #99"
    with pytest.raises(AssertionError):
        assert_no_leakage(text, canonical_issue=99)


def test_assert_no_leakage_catches_closing_pr():
    text = "Fixed by #200"
    with pytest.raises(AssertionError):
        assert_no_leakage(text, closing_pr=200)


# ---------------------------------------------------------------------------
# Integration: build_safe_input strips leakage
# ---------------------------------------------------------------------------

def _make_issue(number=1, label="bug", body="Error occurs", comments=None, canonical=None, pr_number=None):
    timeline = []
    if canonical:
        timeline.append({
            "__typename": "MarkedAsDuplicateEvent",
            "createdAt": "2023-01-10T00:00:00Z",
            "canonical": {"number": canonical},
        })
    if pr_number:
        timeline.append({
            "__typename": "ClosedEvent",
            "createdAt": "2023-01-10T00:00:00Z",
            "closer": {
                "__typename": "PullRequest",
                "number": pr_number,
                "merged": True,
                "mergedAt": "2023-01-10T00:00:00Z",
                "baseRefOid": "abc123",
                "changedFiles": ["src/foo.py"],
            },
        })
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": body,
        "state": "CLOSED",
        "createdAt": "2023-01-01T00:00:00Z",
        "closedAt": "2023-01-10T00:00:00Z",
        "repository": "test/repo",
        "author": {"login": "user1"},
        "labels": {"nodes": [{"name": label}]},
        "comments": {"nodes": comments or [
            {
                "body": f"Duplicate of #{canonical}" if canonical else f"Fixed by PR #{pr_number}",
                "createdAt": "2023-01-11T00:00:00Z",  # POST resolution
                "author": {"login": "bot"},
            }
        ]},
        "timelineItems": {"nodes": timeline},
    }


def test_label_task_no_label_in_input():
    """Label string must not appear in built input."""
    label_map = {"bug": "bug", "enhancement": "feature"}
    task = LabelTask(label_map)
    issue = _make_issue(label="bug", body="Something crashes with TypeError")
    repo_data = {"issues": [issue]}
    instances = task.build_instances(repo_data)
    assert instances, "Should have built at least one instance"
    for inst in instances:
        # The raw mapped label value "bug" should not appear in the safe input
        # (The guard strips label strings including the mapped values)
        assert_no_leakage(inst.input_text, truth_label=inst.ground_truth)


def test_duplicate_task_no_canonical_ref_in_input():
    """Canonical issue number must not appear as 'duplicate of #N' in built input."""
    issue = _make_issue(number=50, body="Same problem", canonical=10)
    older = _make_issue(number=10, body="Original problem", label="bug")
    repo_data = {"issues": [older, issue]}
    task = DuplicateTask(candidates_k=3, seed=42)
    instances = task.build_instances(repo_data)
    pos = [i for i in instances if i.ground_truth != "none"]
    for inst in pos:
        assert_no_leakage(inst.input_text, canonical_issue=inst.ground_truth)


def test_duplicate_post_resolution_comment_excluded():
    """Post-resolution comment with 'Duplicate of #N' must be stripped."""
    issue = _make_issue(number=50, body="Problem", canonical=10, comments=[
        {
            "body": "Duplicate of #10 — closing",
            "createdAt": "2023-01-11T00:00:00Z",  # after resolution
            "author": {"login": "bot"},
        }
    ])
    older = _make_issue(number=10, body="Original", label="bug")
    repo_data = {"issues": [older, issue]}
    task = DuplicateTask(candidates_k=3, seed=42)
    instances = task.build_instances(repo_data)
    pos = [i for i in instances if i.ground_truth != "none"]
    for inst in pos:
        assert "Duplicate of #10" not in inst.input_text
        assert "#10" not in inst.input_text


def test_localize_no_pr_number_in_input():
    """Closing PR number must not appear in built localize input."""
    issue = _make_issue(number=5, body="File needs update", pr_number=999, comments=[
        {
            "body": "PR #999 fixes this",
            "createdAt": "2023-01-11T00:00:00Z",  # post-resolution
            "author": {"login": "bot"},
        }
    ])
    repo_data = {
        "issues": [issue],
        "file_trees": {"test/repo/abc123": ["src/foo.py", "src/bar.py", "src/baz.py"]},
    }
    task = LocalizeTask(candidates_k=5)
    instances = task.build_instances(repo_data)
    for inst in instances:
        assert_no_leakage(inst.input_text, closing_pr=999)
