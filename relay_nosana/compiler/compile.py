from __future__ import annotations

import json

import tiktoken

from ..tasks.base import TaskInstance
from .packet import TaskPacket
from .extractors import extract_signals, extract_code_refs, extractive_summary
from .retrieval import BM25Index

try:
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENC = None


def count_tokens(text: str) -> int:
    if _ENC:
        return len(_ENC.encode(text))
    # Fallback: rough character-based estimate
    return len(text) // 4


def truncate_to_budget(text: str, budget: int) -> str:
    if count_tokens(text) <= budget:
        return text
    # Binary-search for the right char cutoff
    lo, hi = 0, len(text)
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if count_tokens(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid
    return text[:lo]


class RecipientProfile:
    def __init__(self, context_len: int = 8192, output_reserve: int = 512, prompt_reserve: int = 256):
        self.context_len = context_len
        self.output_reserve = output_reserve
        self.prompt_reserve = prompt_reserve

    @property
    def input_budget(self) -> int:
        return self.context_len - self.output_reserve - self.prompt_reserve


def build_raw(instance: TaskInstance, profile: RecipientProfile) -> str:
    """
    Raw mode: title + body + pre-resolution comments, truncated to budget.
    Candidates appended as plain list.
    """
    base = instance.input_text
    candidates_text = "\n\nCANDIDATES:\n" + "\n".join(
        f"- {c}" for c in instance.candidates
    )
    full = base + candidates_text
    return truncate_to_budget(full, profile.input_budget)


def _task_instruction(task_type: str, schema: dict) -> tuple[str, str]:
    """Return (task line, constraints text)."""
    instructions = {
        "label": "Classify this issue into exactly one label from the candidates list.",
        "duplicate": "Determine if this issue is a duplicate. If so, identify the canonical issue number from the candidates list; otherwise output 'none'.",
        "localize": "Predict up to 5 source files (from the candidates list) most likely to need changes to resolve this issue.",
    }
    task_line = instructions.get(task_type, f"Complete the {task_type} task.")
    constraints = f"Respond with valid JSON matching this schema: {json.dumps(schema, separators=(',', ':'))}"
    return task_line, constraints


def build_compiled(
    instance: TaskInstance,
    profile: RecipientProfile,
    schema: dict,
    all_issues: list[dict] | None = None,
) -> str:
    """
    Compiled mode: builds a TaskPacket and serializes it within budget.
    Field fill order (highest → lowest priority):
      task, constraints, candidates, signals, code_refs, summary, related
    """
    task_line, constraints = _task_instruction(instance.task_type, schema)

    signals = extract_signals(instance.input_text)
    code_refs = extract_code_refs(instance.input_text)
    summary = extractive_summary(instance.input_text)

    # Build related issues via BM25
    related: list[str] = []
    if all_issues:
        corpus = [
            f"{i.get('number','')} {i.get('title','')} {i.get('body','') or ''}"
            for i in all_issues
            if i.get("number") != instance.issue_number
        ]
        if corpus:
            idx = BM25Index(corpus)
            top = idx.top_k(instance.input_text, k=5)
            for i in top:
                iss = all_issues[i] if i < len(all_issues) else None
                if iss:
                    related.append(f"#{iss['number']}: {iss.get('title','')[:80]}")

    candidates_text = [str(c) for c in instance.candidates]

    # Budget fitting: fill fields in priority order
    budget = profile.input_budget
    packet = TaskPacket(
        task=task_line,
        constraints=constraints,
    )

    # Mandatory fields first
    base_text = packet.to_text()
    used = count_tokens(base_text)
    remaining = budget - used

    # Candidates
    cand_lines = [f"- {c}" for c in candidates_text]
    cand_block = "\n".join(cand_lines)
    if count_tokens(cand_block) <= remaining:
        packet.candidates = candidates_text
    else:
        # Fit as many as possible
        fitted = []
        for c in candidates_text:
            if count_tokens("\n".join(fitted + [f"- {c}"])) <= remaining - 50:
                fitted.append(c)
        packet.candidates = fitted
    used = count_tokens(packet.to_text())
    remaining = budget - used

    # Signals
    sig_fitted = []
    for s in signals:
        if count_tokens("\n".join(sig_fitted + [s])) <= remaining - 50:
            sig_fitted.append(s)
    packet.signals = sig_fitted
    used = count_tokens(packet.to_text())
    remaining = budget - used

    # Code refs
    ref_fitted = []
    for r in code_refs:
        if count_tokens("\n".join(ref_fitted + [r])) <= remaining - 50:
            ref_fitted.append(r)
    packet.code_refs = ref_fitted
    used = count_tokens(packet.to_text())
    remaining = budget - used

    # Summary
    if count_tokens(summary) <= remaining - 20:
        packet.summary = summary
    else:
        packet.summary = truncate_to_budget(summary, remaining - 20)
    used = count_tokens(packet.to_text())
    remaining = budget - used

    # Related
    rel_fitted = []
    for r in related:
        if count_tokens("\n".join(rel_fitted + [r])) <= remaining - 20:
            rel_fitted.append(r)
    packet.related = rel_fitted
    used = count_tokens(packet.to_text())
    remaining = budget - used

    # Context (raw issue text) — lowest priority, fills remaining budget
    if remaining > 50:
        packet.context = truncate_to_budget(instance.input_text, remaining - 20)

    return packet.to_text()
