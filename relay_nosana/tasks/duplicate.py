from __future__ import annotations

import random
from typing import Any

from .base import TaskInstance
from .ground_truth import build_safe_input


from ..github.ingest import duplicate_canonical

_DUP_LABELS = {"duplicate", "duplicates", "dup"}


def _extract_canonical(issue: dict) -> int | None:
    """Canonical issue: MarkedAsDuplicateEvent, else a precise maintainer
    comment (same rule as is_duplicate_issue; issue bodies never count)."""
    return duplicate_canonical(issue)


class DuplicateTask:
    name = "duplicate"
    metric_name = "accuracy"

    def __init__(self, candidates_k: int = 5, seed: int = 42):
        self.candidates_k = candidates_k
        self.rng = random.Random(seed)

    def build_instances(self, repo_data: Any) -> list[TaskInstance]:
        from ..compiler.retrieval import BM25Index

        issues = repo_data.get("issues", [])
        issue_by_number: dict[int, dict] = {i["number"]: i for i in issues}

        positives: list[TaskInstance] = []
        non_dup_pool: list[dict] = []

        for issue in issues:
            raw_labels = [n["name"].lower() for n in issue.get("labels", {}).get("nodes", [])]
            is_dup_labeled = any(l in _DUP_LABELS for l in raw_labels)

            canonical = _extract_canonical(issue)

            if canonical is None and is_dup_labeled:
                # Can't determine canonical; skip
                continue

            if canonical is not None and canonical in issue_by_number:
                # Build BM25 candidate set (older issues only)
                older = [
                    i for i in issues
                    if i["number"] < issue["number"]
                ]
                index = BM25Index([
                    f"{i['title']} {i.get('body','') or ''}" for i in older
                ])
                query = f"{issue['title']} {issue.get('body','') or ''}"
                top_k = index.top_k(query, k=self.candidates_k - 1)
                candidate_numbers = [older[idx]["number"] for idx in top_k]

                # Guarantee canonical is in candidates
                if canonical not in candidate_numbers:
                    candidate_numbers = [canonical] + candidate_numbers[: self.candidates_k - 1]
                else:
                    candidate_numbers = candidate_numbers[: self.candidates_k]

                # Find the duplicate-marked event timestamp
                resolution_ts = None
                for item in issue.get("timelineItems", {}).get("nodes", []):
                    if item.get("__typename") == "MarkedAsDuplicateEvent":
                        resolution_ts = item.get("createdAt")
                        break
                if resolution_ts is None:
                    resolution_ts = issue.get("closedAt")

                input_text = build_safe_input(
                    issue,
                    resolution_ts=resolution_ts,
                    canonical_issue=canonical,
                    closing_pr=None,
                )
                positives.append(TaskInstance(
                    id=f"{issue['number']}:duplicate",
                    issue_number=issue["number"],
                    task_type=self.name,
                    input_text=input_text,
                    candidates=candidate_numbers,
                    ground_truth=canonical,
                    meta={"canonical": canonical, "bm25_recall": canonical in candidate_numbers},
                ))
            else:
                # Candidate for negative pool
                non_dup_pool.append(issue)

        # Build equal-count negatives
        self.rng.shuffle(non_dup_pool)
        negatives_raw = non_dup_pool[: len(positives)]

        instances = list(positives)
        for issue in negatives_raw:
            older = [i for i in issues if i["number"] < issue["number"]]
            candidate_pool = older if older else [i for i in issues if i["number"] != issue["number"]]
            if not candidate_pool:
                continue
            index = BM25Index([
                f"{i['title']} {i.get('body','') or ''}" for i in candidate_pool
            ])
            query = f"{issue['title']} {issue.get('body','') or ''}"
            top_k = index.top_k(query, k=self.candidates_k)
            candidate_numbers = [candidate_pool[idx]["number"] for idx in top_k]

            input_text = build_safe_input(
                issue,
                resolution_ts=issue.get("closedAt"),
                canonical_issue=None,
                closing_pr=None,
            )
            instances.append(TaskInstance(
                id=f"{issue['number']}:duplicate:neg",
                issue_number=issue["number"],
                task_type=self.name,
                input_text=input_text,
                candidates=candidate_numbers,
                ground_truth="none",
                meta={"is_negative": True},
            ))

        return instances

    def output_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "duplicate_of": {
                    "oneOf": [
                        {"type": "integer"},
                        {"type": "string", "const": "none"},
                    ]
                }
            },
            "required": ["duplicate_of"],
            "additionalProperties": False,
        }

    def score(self, prediction: Any, truth: Any) -> float:
        if isinstance(prediction, dict):
            pred = prediction.get("duplicate_of", "none")
        else:
            pred = prediction

        if truth == "none":
            return 1.0 if str(pred).lower() == "none" else 0.0
        try:
            return 1.0 if int(pred) == int(truth) else 0.0
        except (ValueError, TypeError):
            return 0.0
