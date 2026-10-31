"""Test that ingest resumes from partial cache after an interruption."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

from relay_nosana.github.ingest import fetch_issues


def _make_issue(number: int) -> dict:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "",
        "state": "OPEN",
        "createdAt": f"2023-01-{number:02d}T00:00:00Z",
        "closedAt": None,
        "author": {"login": "user"},
        "labels": {"nodes": []},
        "comments": {"nodes": []},
        "timelineItems": {"nodes": []},
    }


def _page(issues, cursor_out, has_next):
    return {
        "data": {
            "repository": {
                "issues": {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor_out},
                    "nodes": issues,
                }
            }
        }
    }, None  # (payload, rl_remaining)


PAGE1 = [_make_issue(i) for i in range(1, 4)]   # issues 1-3
PAGE2 = [_make_issue(i) for i in range(4, 7)]   # issues 4-6


@pytest.mark.asyncio
async def test_resume_fetches_only_missing_pages(tmp_path):
    """
    Simulate: first run fetches page 1 then raises; second run should
    load the cached page-1 issues and fetch only page 2.
    """
    # Build a mock that delivers page1 on cursor=None and page2 on cursor="cur1"
    call_log = []

    async def mock_graphql(client, token, query, variables):
        cursor = variables.get("cursor")
        call_log.append(cursor)
        if cursor is None:
            return _page(PAGE1, "cur1", has_next=True)
        if cursor == "cur1":
            return _page(PAGE2, None, has_next=False)
        raise AssertionError(f"Unexpected cursor: {cursor}")

    with patch("relay_nosana.github.ingest._graphql", side_effect=mock_graphql):
        # --- First run: patch so it crashes after page 1 ---
        first_call_count = [0]
        orig_mock = mock_graphql

        async def crashing_graphql(client, token, query, variables):
            first_call_count[0] += 1
            result = await orig_mock(client, token, query, variables)
            if first_call_count[0] >= 1:
                # After first successful page write, simulate crash on next iteration
                # by raising on second call
                if first_call_count[0] > 1:
                    raise RuntimeError("simulated interruption")
            return result

        with patch("relay_nosana.github.ingest._graphql", side_effect=crashing_graphql):
            with pytest.raises(RuntimeError, match="simulated interruption"):
                await fetch_issues("owner", "repo", "tok", cache_dir=tmp_path)

    # Cache should have page-1 issues saved atomically
    issues_path = tmp_path / "issues.json"
    state_path = tmp_path / "state.json"
    assert issues_path.exists(), "issues.json not written after page 1"
    saved = json.loads(issues_path.read_text())
    assert len(saved) == 3, f"Expected 3 issues after page 1, got {len(saved)}"

    state = json.loads(state_path.read_text())
    assert state["cursor"] == "cur1"
    assert state["pages_done"] == 1
    assert not state["complete"]

    # --- Second run: should resume from cursor "cur1", fetch only page 2 ---
    second_calls = []

    async def resuming_graphql(client, token, query, variables):
        second_calls.append(variables.get("cursor"))
        cursor = variables.get("cursor")
        if cursor == "cur1":
            return _page(PAGE2, None, has_next=False)
        raise AssertionError(f"Unexpected cursor on resume: {cursor}")

    with patch("relay_nosana.github.ingest._graphql", side_effect=resuming_graphql):
        issues = await fetch_issues("owner", "repo", "tok", cache_dir=tmp_path)

    assert issues is not None
    assert len(issues) == 6, f"Expected 6 total issues after resume, got {len(issues)}"
    assert second_calls == ["cur1"], "Second run should start from saved cursor"

    state2 = json.loads(state_path.read_text())
    assert state2["complete"] is True


@pytest.mark.asyncio
async def test_complete_state_returns_immediately(tmp_path):
    """Once state marks complete, fetch_issues returns cache without any HTTP calls."""
    issues_data = [_make_issue(i) for i in range(1, 6)]
    (tmp_path / "issues.json").write_text(json.dumps(issues_data))
    (tmp_path / "state.json").write_text(json.dumps({"complete": True, "pages_done": 1}))

    called = []

    async def should_not_be_called(*args, **kwargs):
        called.append(True)
        raise AssertionError("HTTP called on completed ingest")

    with patch("relay_nosana.github.ingest._graphql", side_effect=should_not_be_called):
        issues = await fetch_issues("owner", "repo", "tok", cache_dir=tmp_path)

    assert len(issues) == 5
    assert called == []
