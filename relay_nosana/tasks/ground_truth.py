from __future__ import annotations

import re
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Leakage-guard helpers
# ---------------------------------------------------------------------------

_FIXES_RE = re.compile(
    r"\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s+#\d+",
    re.IGNORECASE,
)
_PR_LINK_RE = re.compile(
    r"(?:pull|pr)[s/\s#]*\d+|/pull/\d+",
    re.IGNORECASE,
)
_DUP_OF_RE = re.compile(
    r"duplicate\s+of\s+#(\d+)|dup(?:licate)?\s+of\s+#(\d+)",
    re.IGNORECASE,
)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def strip_labels_from_text(text: str, labels: list[str]) -> str:
    """Remove occurrences of any mapped label strings from text."""
    for lbl in labels:
        text = re.sub(re.escape(lbl), "[LABEL_REMOVED]", text, flags=re.IGNORECASE)
    return text


def filter_comments_before(
    comments: list[dict], cutoff: str | datetime
) -> list[dict]:
    """Keep only comments whose createdAt is strictly before *cutoff*."""
    if isinstance(cutoff, str):
        cutoff_dt = _parse_iso(cutoff)
    else:
        cutoff_dt = cutoff
    if cutoff_dt is None:
        return comments
    return [
        c for c in comments
        if (_parse_iso(c.get("createdAt")) or datetime.min.replace(tzinfo=cutoff_dt.tzinfo)) < cutoff_dt
    ]


def remove_resolution_references(
    text: str,
    *,
    canonical_issue: int | None = None,
    closing_pr: int | None = None,
) -> str:
    """Strip Fixes/Closes refs, PR links, and all duplicate-of mentions."""
    text = _FIXES_RE.sub("[REF_REMOVED]", text)
    text = _PR_LINK_RE.sub("[PR_REMOVED]", text)
    # Always strip all "duplicate of #N" patterns — they directly reveal the answer
    text = _DUP_OF_RE.sub("[DUP_REMOVED]", text)
    if canonical_issue is not None:
        # Additionally strip bare #N references to the specific canonical
        text = re.sub(rf"(?<!\d)#{canonical_issue}(?!\d)", "[DUP_REMOVED]", text)
    if closing_pr is not None:
        text = re.sub(rf"(?<!\d)#{closing_pr}(?!\d)", "[PR_REMOVED]", text)
    return text


def build_safe_input(
    issue: dict[str, Any],
    *,
    resolution_ts: str | None,
    label_strings: list[str] | None = None,
    canonical_issue: int | None = None,
    closing_pr: int | None = None,
) -> str:
    """
    Build leakage-free text from an issue for use as model input.
    Strips: labels, post-resolution comments, PR/duplicate references.
    """
    title = issue.get("title", "")
    body = issue.get("body", "") or ""
    comments = issue.get("comments", {}).get("nodes", [])

    # Keep only pre-resolution comments
    if resolution_ts:
        comments = filter_comments_before(comments, resolution_ts)

    # Build text
    parts = [f"Title: {title}", f"Body:\n{body}"]
    for c in comments:
        author = c.get("author", {}).get("login", "unknown")
        body_c = c.get("body", "") or ""
        parts.append(f"Comment by {author}:\n{body_c}")

    text = "\n\n".join(parts)

    # Apply leakage guards
    text = remove_resolution_references(
        text,
        canonical_issue=canonical_issue,
        closing_pr=closing_pr,
    )
    if label_strings:
        text = strip_labels_from_text(text, label_strings)

    return text


# ---------------------------------------------------------------------------
# Leakage assertion helpers (used in tests)
# ---------------------------------------------------------------------------

def assert_no_leakage(
    text: str,
    *,
    truth_label: str | None = None,
    canonical_issue: int | None = None,
    closing_pr: int | None = None,
) -> None:
    """Raise AssertionError if any ground-truth value appears verbatim in text."""
    if truth_label:
        assert truth_label.lower() not in text.lower(), (
            f"Label '{truth_label}' leaked into input"
        )
    if canonical_issue is not None:
        # The bare number "123" may appear, but "duplicate of #123" should not
        assert not re.search(
            rf"duplicate\s+of\s+#{canonical_issue}", text, re.IGNORECASE
        ), f"Canonical issue #{canonical_issue} leaked as duplicate reference"
        # Also check bare #N didn't slip through
        assert not re.search(
            rf"(?<![/\w])#{canonical_issue}(?!\d)", text
        ), f"Canonical issue #{canonical_issue} leaked as bare reference"
    if closing_pr is not None:
        assert not re.search(
            rf"(?<![/\w])#{closing_pr}(?!\d)", text
        ), f"Closing PR #{closing_pr} leaked into input"
