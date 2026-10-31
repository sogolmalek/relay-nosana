from __future__ import annotations

from pydantic import BaseModel, Field


class TaskPacket(BaseModel):
    task: str = ""
    summary: str = ""
    signals: list[str] = Field(default_factory=list)
    code_refs: list[str] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    constraints: str = ""
    context: str = ""  # raw issue text (truncated) — lowest-priority budget filler

    def to_text(self) -> str:
        """Serialize as compact, clearly sectioned text."""
        parts: list[str] = []
        if self.task:
            parts.append(f"[TASK]\n{self.task}")
        if self.constraints:
            parts.append(f"[OUTPUT FORMAT]\n{self.constraints}")
        if self.candidates:
            parts.append("[CANDIDATES]\n" + "\n".join(f"- {c}" for c in self.candidates))
        if self.signals:
            parts.append("[SIGNALS]\n" + "\n".join(f"- {s}" for s in self.signals))
        if self.code_refs:
            parts.append("[CODE REFS]\n" + "\n".join(f"- {r}" for r in self.code_refs))
        if self.summary:
            parts.append(f"[SUMMARY]\n{self.summary}")
        if self.related:
            parts.append("[RELATED ISSUES]\n" + "\n".join(f"- {r}" for r in self.related))
        if self.context:
            parts.append(f"[ISSUE TEXT]\n{self.context}")
        return "\n\n".join(parts)
