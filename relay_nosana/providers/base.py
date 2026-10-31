from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..compiler.compile import RecipientProfile


@dataclass
class Completion:
    text: str
    parsed: dict | None
    tokens_in: int
    tokens_out: int
    latency_ms: float
    error: str | None = None

    @property
    def parse_ok(self) -> bool:
        return self.parsed is not None and self.error is None


@runtime_checkable
class Recipient(Protocol):
    id: str
    profile: RecipientProfile

    async def complete(self, prompt: str, schema: dict) -> Completion: ...
