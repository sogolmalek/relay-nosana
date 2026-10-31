from __future__ import annotations

from typing import Any

from .base import TaskInstance
from .ground_truth import build_safe_input


class LabelTask:
    name = "label"
    metric_name = "accuracy"

    def __init__(self, label_map: dict[str, str], taxonomy: list[str] | None = None):
        self.label_map = {k.lower(): v for k, v in label_map.items()}
        self.taxonomy = taxonomy or sorted(set(label_map.values()))

    def _map_labels(self, raw_labels: list[str]) -> list[str]:
        mapped = []
        for lbl in raw_labels:
            key = lbl.lower().strip()
            if key in self.label_map:
                mapped.append(self.label_map[key])
        return list(set(mapped))

    def build_instances(self, repo_data: Any) -> list[TaskInstance]:
        issues = repo_data.get("issues", [])
        instances = []
        for issue in issues:
            if issue.get("state") != "CLOSED":
                continue
            raw_labels = [n["name"] for n in issue.get("labels", {}).get("nodes", [])]
            mapped = self._map_labels(raw_labels)
            if len(mapped) != 1:
                continue  # need exactly one mapped label
            truth = mapped[0]

            # Find label-assignment timestamp as resolution cutoff
            # Use closedAt as a conservative cutoff
            resolution_ts = issue.get("closedAt")

            input_text = build_safe_input(
                issue,
                resolution_ts=resolution_ts,
                label_strings=list(self.label_map.values()) + list(self.label_map.keys()),
                canonical_issue=None,
                closing_pr=None,
            )

            instances.append(TaskInstance(
                id=f"{issue.get('number')}:label",
                issue_number=issue["number"],
                task_type=self.name,
                input_text=input_text,
                candidates=self.taxonomy,
                ground_truth=truth,
                meta={"raw_labels": raw_labels},
            ))
        return instances

    def output_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "label": {"type": "string", "enum": self.taxonomy},
            },
            "required": ["label"],
            "additionalProperties": False,
        }

    def score(self, prediction: Any, truth: Any) -> float:
        if isinstance(prediction, dict):
            pred_label = prediction.get("label", "")
        else:
            pred_label = str(prediction)
        return 1.0 if pred_label == truth else 0.0
