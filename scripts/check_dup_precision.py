#!/usr/bin/env python3
"""
Precision audit of text-only duplicates (no label, no MarkedAsDuplicateEvent).

Production rule (relay_nosana.github.ingest.maintainer_dup_match):
  - maintainer comments only (OWNER | MEMBER | COLLABORATOR); issue bodies never count
  - reject negation ("not a duplicate"), hedges (might, maybe, possible/possibly,
    perhaps, could be, seems like a dup) and questions (sentence ends with ?)
  - #N must reference an OLDER issue (N < issue["number"])

Prints what the raw regex matches, what the rule rejects, 15 accepted examples,
and the final counts from count_duplicates_full.

Usage:
    .venv/bin/python scripts/check_dup_precision.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from relay_nosana.github.ingest import (
    _DUP_OF_RE, _MAINTAINER_ASSOC, _dup_event_canonical, _has_dup_label,
    count_duplicates_full, maintainer_dup_match,
)


def main() -> None:
    cache = Path("out/cache/huggingface__transformers/issues.json")
    if not cache.exists():
        sys.exit("Run ingest first.")
    issues = json.loads(cache.read_text())
    print(f"Loaded {len(issues)} issues.", flush=True)

    text_only = [i for i in issues if not _has_dup_label(i) and _dup_event_canonical(i) is None]
    raw_body = [i for i in text_only if _DUP_OF_RE.search(i.get("body") or "")]
    raw_comment = [
        i for i in text_only
        if any(
            c.get("authorAssociation") in _MAINTAINER_ASSOC and _DUP_OF_RE.search(c.get("body") or "")
            for c in i.get("comments", {}).get("nodes", [])
        )
    ]
    accepted = [(i, m) for i in text_only if (m := maintainer_dup_match(i))]
    accepted_nums = {i["number"] for i, _ in accepted}
    rejected = [i for i in raw_comment if i["number"] not in accepted_nums]

    print(f"\nRaw regex matches (text-only issues): body {len(raw_body)} (excluded by rule), "
          f"maintainer comment {len(raw_comment)}")
    print(f"Precise maintainer-comment matches   : {len(accepted)}  ({len(rejected)} rejected)")

    print("\n── Rejected maintainer-comment matches ──")
    for i in rejected:
        c = next(c for c in i["comments"]["nodes"]
                 if c.get("authorAssociation") in _MAINTAINER_ASSOC and _DUP_OF_RE.search(c.get("body") or ""))
        print(f"  #{i['number']}: \"{_DUP_OF_RE.search(c['body']).group(0)}\" in: {c['body'][:100]!r}")

    print(f"\n── {min(15, len(accepted))} of {len(accepted)} accepted ──")
    for i, (canonical, sent, c) in accepted[:15]:
        print(f"  #{i['number']} → #{canonical}  {(c.get('author') or {}).get('login', '?')} "
              f"({c['authorAssociation']}): \"{sent[:100]}\"")

    stats = count_duplicates_full(issues)
    print("\n── Final counts (count_duplicates_full) ──")
    for k in ("by_label", "by_event", "by_comment_only", "total"):
        print(f"  {k:16}: {stats[k]}")
    ok = stats["total"] >= 50
    print(f"\n  {'✅' if ok else '❌'} Duplicate task is "
          f"{'IN headline' if ok else 'EXPLORATORY'} (threshold: 50 positives)")


if __name__ == "__main__":
    main()
