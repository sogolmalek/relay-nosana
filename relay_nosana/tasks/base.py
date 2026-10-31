from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class TaskInstance:
    id: str                        # "<owner>/<repo>#<number>:<task_type>"
    issue_number: int
    task_type: str
    input_text: str                # leakage-free raw text (title + body + pre-resolution comments)
    candidates: list[Any]          # type depends on task (labels, issue numbers, file paths)
    ground_truth: Any              # the answer
    meta: dict[str, Any] = field(default_factory=dict)  # extra task-specific data


@runtime_checkable
class TaskType(Protocol):
    name: str
    metric_name: str

    def build_instances(self, repo_data: Any) -> list[TaskInstance]: ...
    def output_schema(self) -> dict: ...
    def score(self, prediction: Any, truth: Any) -> float: ...
