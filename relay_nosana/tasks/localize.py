from __future__ import annotations

import fnmatch
from typing import Any

from .base import TaskInstance
from .ground_truth import build_safe_input


def _matches_any_glob(path: str, globs: list[str]) -> bool:
    for g in globs:
        if fnmatch.fnmatch(path, g):
            return True
    return False


def _get_closing_pr(issue: dict) -> dict | None:
    for item in issue.get("timelineItems", {}).get("nodes", []):
        if item.get("__typename") == "ClosedEvent":
            closer = item.get("closer")
            if closer and closer.get("__typename") == "PullRequest" and closer.get("merged"):
                return closer
    return None


class LocalizeTask:
    name = "localize"
    metric_name = "hit_at_5"

    def __init__(
        self,
        exclude_globs: list[str] | None = None,
        candidates_k: int = 30,
        retrieval: str = "plain",
    ):
        self.exclude_globs = exclude_globs or [
            "**/test*/**", "**/*_test.*", "docs/**", "*.md"
        ]
        self.candidates_k = candidates_k
        if retrieval not in ("plain", "rrf"):
            raise ValueError(f"retrieval must be 'plain' or 'rrf', got {retrieval!r}")
        self.retrieval = retrieval

    def _filter_files(self, files: list[str]) -> list[str]:
        return [f for f in files if not _matches_any_glob(f, self.exclude_globs)]

    def build_instances(self, repo_data: Any) -> list[TaskInstance]:
        from ..compiler.retrieval import BM25Index, RRFFusedBM25Index
        from ..compiler.extractors import extract_signals, extract_code_refs
        from ..github.ingest import tree_scope

        issues = repo_data.get("issues", [])
        # Keyed "<owner>/<repo>/<baseRefOid>"; there is no HEAD/"default" fallback.
        file_trees: dict[str, list[str]] = repo_data.get("file_trees", {})

        instances = []
        for issue in issues:
            if issue.get("state") != "CLOSED":
                continue
            pr = _get_closing_pr(issue)
            if pr is None:
                continue

            pr_number = pr.get("number")
            changed_files: list[str] = pr.get("changedFiles", [])
            truth_files = self._filter_files(changed_files)
            if not truth_files:
                continue

            base_ref = pr.get("baseRefOid")
            repository = issue.get("repository")
            if not base_ref or not repository:
                raise ValueError(
                    f"Issue #{issue['number']}: closing PR #{pr_number} has "
                    f"baseRefOid={base_ref!r}, repository={repository!r}; both are "
                    "required to select the base-commit tree."
                )
            tree_key = f"{repository}/{base_ref}"
            all_files = file_trees.get(tree_key)
            if not all_files:
                raise ValueError(
                    f"Issue #{issue['number']}: base-commit tree {tree_key!r} is "
                    f"{'empty' if all_files is not None else 'missing'}. Fetch it with "
                    "fetch_file_tree before building localize instances."
                )

            # Truth must be reachable from the listing (src/-only unless pre-src/)
            scope = tree_scope(all_files)
            truth_files = [f for f in truth_files if f.startswith(scope)]
            if not truth_files:
                continue

            candidate_pool = self._filter_files(all_files)
            if not candidate_pool:
                continue  # every file is test/docs — skip legitimately

            # Build leakage-free input text first (needed for extractor input)
            resolution_ts = pr.get("mergedAt") or issue.get("closedAt")
            input_text = build_safe_input(
                issue,
                resolution_ts=resolution_ts,
                closing_pr=pr_number,
            )

            if self.retrieval == "rrf":
                # RRF-fused BM25 (path-token BM25 ⊕ mention-signal list, k=60, cap=10)
                raw_text = f"{issue['title']}\n{issue.get('body','') or ''}"
                index = RRFFusedBM25Index(
                    candidate_pool,
                    signals=extract_signals(raw_text),
                    code_refs=extract_code_refs(raw_text),
                )
                n_boosted = index.mention_count
            else:
                index = BM25Index(candidate_pool)
                n_boosted = 0
            query = f"{issue['title']} {issue.get('body','') or ''}"
            top_k_idx = index.top_k(query, k=self.candidates_k)
            candidates = [candidate_pool[i] for i in top_k_idx]

            # candidate_recall: ≥1 truth file present in candidate set
            candidate_recall = any(f in set(candidates) for f in truth_files)

            instances.append(TaskInstance(
                id=f"{issue['number']}:localize",
                issue_number=issue["number"],
                task_type=self.name,
                input_text=input_text,
                candidates=candidates,
                ground_truth=truth_files,
                meta={
                    "pr_number": pr_number,
                    "candidate_recall": candidate_recall,  # retrieval succeeded?
                    "base_ref": base_ref,
                    "retrieval": self.retrieval,
                    "n_boosted": n_boosted,
                },
            ))
        return instances

    def output_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 5,
                }
            },
            "required": ["files"],
            "additionalProperties": False,
        }

    def score(self, prediction: Any, truth: Any) -> float:
        """hit@5 overall: 1 if ≥1 predicted file is in truth set."""
        pred_files = _parse_pred(prediction)[:5]
        truth_set = set(truth) if isinstance(truth, list) else {truth}
        return 1.0 if any(f in truth_set for f in pred_files) else 0.0

    def score_given_recall(self, prediction: Any, truth: Any, instance: TaskInstance) -> float | None:
        """
        hit@5 conditional on candidate_recall.
        Returns None for instances where no truth file is in the candidate set
        (those are retrieval misses, not model misses).
        """
        if not instance.meta.get("candidate_recall", True):
            return None
        return self.score(prediction, truth)

    def recall_at_5(self, prediction: Any, truth: Any) -> float:
        """Recall@5: fraction of truth files captured in top-5 predictions."""
        pred_files = _parse_pred(prediction)[:5]
        truth_set = set(truth) if isinstance(truth, list) else {truth}
        if not truth_set:
            return 0.0
        hits = sum(1 for f in pred_files if f in truth_set)
        return hits / len(truth_set)


def _parse_pred(prediction: Any) -> list[str]:
    if isinstance(prediction, dict):
        return prediction.get("files", [])
    if isinstance(prediction, list):
        return prediction
    return []
