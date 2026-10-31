"""Duplicate rule: label, MarkedAsDuplicateEvent, or a precise maintainer comment."""
from __future__ import annotations

import pytest

from relay_nosana.github.ingest import duplicate_canonical, is_duplicate_issue


def _issue(number=100, body="", comments=(), labels=(), event_canonical=None) -> dict:
    timeline = []
    if event_canonical is not None:
        timeline.append({"__typename": "MarkedAsDuplicateEvent", "canonical": {"number": event_canonical}})
    return {
        "number": number,
        "body": body,
        "labels": {"nodes": [{"name": l} for l in labels]},
        "comments": {"nodes": [{"body": b, "authorAssociation": a} for b, a in comments]},
        "timelineItems": {"nodes": timeline},
    }


def test_label_and_event_count():
    assert is_duplicate_issue(_issue(labels=["duplicate"]))
    assert is_duplicate_issue(_issue(event_canonical=5))
    assert duplicate_canonical(_issue(event_canonical=5)) == 5


@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_precise_maintainer_comment_counts(assoc):
    issue = _issue(comments=[("Thanks! This is a duplicate of #42.", assoc)])
    assert is_duplicate_issue(issue)
    assert duplicate_canonical(issue) == 42


def test_body_never_counts():
    assert not is_duplicate_issue(_issue(body="Duplicate of #42"))


@pytest.mark.parametrize("assoc", ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", None])
def test_non_maintainer_comment_does_not_count(assoc):
    assert not is_duplicate_issue(_issue(comments=[("Duplicate of #42", assoc)]))


@pytest.mark.parametrize("text", [
    "Duplicate of #42?",                      # question
    "Maybe a duplicate of #42",               # hedge
    "Possible duplicate of #42",              # hedge
    "Seems like a duplicate of #42",          # hedge
    "This is not a duplicate of #42",         # negation
    "Duplicate of #142",                      # canonical newer than issue #100
    "Duplicate of #100",                      # self-reference
])
def test_imprecise_maintainer_comment_rejected(text):
    assert not is_duplicate_issue(_issue(comments=[(text, "MEMBER")]))


def test_later_precise_sentence_still_counts():
    issue = _issue(comments=[("Is this a duplicate of #41? Yes: duplicate of #42.", "MEMBER")])
    assert duplicate_canonical(issue) == 42
