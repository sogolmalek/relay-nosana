"""Proof rules from relay.rules.yaml: which work needs which grade of receipt, the bar, re-runs."""

from __future__ import annotations

import hashlib
import random
from pathlib import Path

import yaml

from relay_verify.jcs import canonical, sha256_hex

RULES_PATH = Path(__file__).resolve().parent.parent / "relay.rules.yaml"


def load(path: Path = RULES_PATH) -> list[dict]:
    return yaml.safe_load(path.read_text())["rules"]


def snapshot_sha256(rule: dict) -> str:
    return sha256_hex(rule)


def _when(cond: dict | None, amount_usd: float) -> bool:
    if not cond:
        return True
    if "amount_usd_gt" in cond and not amount_usd > cond["amount_usd_gt"]:
        return False
    return True


def rule_for(rules: list[dict], work_type: str, amount_usd: float) -> dict:
    for r in rules:
        if r["work_type"] == work_type and _when(r.get("when"), amount_usd):
            return r
    raise LookupError(f"no rule for {work_type} at ${amount_usd}")


def bar_cleared(rule: dict, open_ci_lower: float) -> bool:
    """Absolute bar: the open model's accuracy against the deterministic ground truth, Wilson 95% CI
    lower bound >= bar.ci_lower_gte. (A frontier-ratio bar needs a frontier measurement; not in the POC.)"""
    bar = rule.get("bar") or {}
    if "ci_lower_gte" not in bar:
        raise ValueError(f"rule {rule.get('id')}: bar has no ci_lower_gte")
    return open_ci_lower >= bar["ci_lower_gte"]


def rerun_sample(rule: dict, items: list[tuple[str, float]], run_id: str) -> list[str]:
    """Risk-based re-run set: every item matching `always_if`, plus a seeded random sample so the total
    is max(sample_min, ceil(sample_rate * n)), capped at n. items: (item_id, amount_usd)."""
    cfg = rule.get("rerun") or {}
    always = sorted(i for i, amt in items if cfg.get("always_if") and _when(cfg["always_if"], amt))
    want = min(len(items), max(cfg.get("sample_min", 0), -(-len(items) * cfg.get("sample_rate", 0) // 1)))
    rest = sorted(i for i, _ in items if i not in always)
    rng = random.Random(int(hashlib.sha256(run_id.encode()).hexdigest(), 16))
    extra = rng.sample(rest, max(0, min(len(rest), int(want) - len(always))))
    return sorted(always + extra)


__all__ = ["load", "snapshot_sha256", "rule_for", "bar_cleared", "rerun_sample", "canonical"]
