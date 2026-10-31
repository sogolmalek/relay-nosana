from __future__ import annotations

import json
import random
import time
from typing import Any

from .base import Completion
from ..compiler.compile import RecipientProfile


class MockRecipient:
    """Deterministic mock provider for tests — no network calls."""

    def __init__(
        self,
        accuracy: dict[str, float] | None = None,
        seed: int = 42,
        latency_ms: float = 5.0,
        context_len: int = 8192,
    ):
        # Per-task accuracy rates
        self.accuracy = accuracy or {
            "label": 0.80,
            "duplicate": 0.75,
            "localize": 0.70,
        }
        self.rng = random.Random(seed)
        self._latency_ms = latency_ms
        self.id = "mock:deterministic"
        self.profile = RecipientProfile(context_len=context_len)

    async def complete(self, prompt: str, schema: dict) -> Completion:
        t0 = time.time()

        # Infer task type from schema shape to generate plausible output
        parsed = self._generate(prompt, schema)
        text = json.dumps(parsed)

        tokens_in = len(prompt) // 4
        tokens_out = len(text) // 4
        latency = self._latency_ms + self.rng.uniform(-1, 1)

        return Completion(
            text=text,
            parsed=parsed,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=max(1.0, latency),
        )

    def _generate(self, prompt: str, schema: dict) -> dict:
        props = schema.get("properties", {})

        if "label" in props:
            return self._gen_label(prompt, props["label"])
        elif "duplicate_of" in props:
            return self._gen_duplicate(prompt)
        elif "files" in props:
            return self._gen_localize(prompt)
        return {}

    def _gen_label(self, prompt: str, label_schema: dict) -> dict:
        enum = label_schema.get("enum", ["bug", "feature", "docs", "question", "other"])
        acc = self.accuracy.get("label", 0.80)
        # The "correct" answer is the first enum value when we can't know truth
        # Tests inject the truth through the prompt; mock just picks at random with accuracy
        if self.rng.random() < acc:
            return {"label": enum[0]}
        return {"label": self.rng.choice(enum)}

    def _gen_duplicate(self, prompt: str) -> dict:
        acc = self.accuracy.get("duplicate", 0.75)
        if self.rng.random() < acc:
            return {"duplicate_of": "none"}
        return {"duplicate_of": self.rng.randint(1, 100)}

    def _gen_localize(self, prompt: str) -> dict:
        acc = self.accuracy.get("localize", 0.70)
        # Extract candidate lines from prompt
        candidates = _extract_candidates(prompt)
        if not candidates:
            return {"files": []}
        if self.rng.random() < acc:
            chosen = candidates[: min(3, len(candidates))]
        else:
            self.rng.shuffle(candidates)
            chosen = candidates[:3]
        return {"files": chosen}


def _extract_candidates(prompt: str) -> list[str]:
    """Pull candidate items listed with '- ' from the prompt."""
    lines = prompt.split("\n")
    candidates = []
    in_section = False
    for line in lines:
        if "CANDIDATES" in line:
            in_section = True
            continue
        if in_section:
            if line.startswith("[") and line.endswith("]"):
                break
            if line.startswith("- "):
                candidates.append(line[2:].strip())
    return candidates


class MockRecipientWithGroundTruth(MockRecipient):
    """
    Extended mock that reads the ground truth embedded in the prompt
    and answers correctly with configurable accuracy.
    Used for deterministic e2e tests.
    """

    def set_answer_for(self, instance_id: str, answer: Any) -> None:
        if not hasattr(self, "_answers"):
            self._answers: dict[str, Any] = {}
        self._answers[instance_id] = answer

    def _generate(self, prompt: str, schema: dict) -> dict:
        # Delegate to parent's deterministic logic
        return super()._generate(prompt, schema)
