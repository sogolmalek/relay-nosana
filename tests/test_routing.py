"""Routing logic tests on synthetic calibration results."""
import pytest
from relay_nosana.route import (
    compute_routing_table,
    compute_routable_share,
    RoutingDecision,
)


def _cell(task_type, mode, recipient_id, mean_score, ci_lo, ci_hi, n=100):
    return {
        "task_type": task_type,
        "mode": mode,
        "recipient_id": recipient_id,
        "n": n,
        "mean_score": mean_score,
        "ci_lo": ci_lo,
        "ci_hi": ci_hi,
        "parse_fail_rate": 0.02,
        "tokens_in_mean": 500,
        "tokens_out_mean": 50,
        "latency_p50_ms": 200,
        "latency_p95_ms": 800,
    }


FRONTIER_SCORE = 0.90
THRESHOLD = 0.9  # nosana lower_CI >= 0.90 × 0.90 = 0.81


def _make_cells(nosana_raw_ci_lo, nosana_compiled_ci_lo):
    return [
        # Frontier raw
        _cell("label", "raw", "frontier:claude-sonnet-4-6", FRONTIER_SCORE, 0.85, 0.95),
        # Frontier compiled
        _cell("label", "compiled", "frontier:claude-sonnet-4-6", FRONTIER_SCORE, 0.85, 0.95),
        # Nosana raw
        _cell("label", "raw", "nosana:llm-8b@market-a",
              nosana_raw_ci_lo + 0.02, nosana_raw_ci_lo, nosana_raw_ci_lo + 0.05),
        # Nosana compiled
        _cell("label", "compiled", "nosana:llm-8b@market-a",
              nosana_compiled_ci_lo + 0.02, nosana_compiled_ci_lo, nosana_compiled_ci_lo + 0.05),
    ]


def test_nosana_eligible_when_above_threshold():
    # CI lower bound = 0.83 >= 0.81 threshold
    cells = _make_cells(nosana_raw_ci_lo=0.83, nosana_compiled_ci_lo=0.85)
    decisions = compute_routing_table(cells, threshold_ratio=THRESHOLD)
    d = {(d.task_type, d.mode): d for d in decisions}
    assert d[("label", "raw")].routes_to_nosana
    assert d[("label", "compiled")].routes_to_nosana


def test_nosana_not_eligible_when_below_threshold():
    # CI lower bound = 0.75 < 0.81 threshold
    cells = _make_cells(nosana_raw_ci_lo=0.75, nosana_compiled_ci_lo=0.75)
    decisions = compute_routing_table(cells, threshold_ratio=THRESHOLD)
    d = {(d.task_type, d.mode): d for d in decisions}
    assert not d[("label", "raw")].routes_to_nosana
    assert d[("label", "raw")].chosen_recipient_id.startswith("frontier:")
    assert not d[("label", "compiled")].routes_to_nosana


def test_compiled_eligible_but_raw_not():
    """Compiler lift can make compiled eligible while raw is not."""
    cells = _make_cells(nosana_raw_ci_lo=0.75, nosana_compiled_ci_lo=0.85)
    decisions = compute_routing_table(cells, threshold_ratio=THRESHOLD)
    d = {(d.task_type, d.mode): d for d in decisions}
    assert not d[("label", "raw")].routes_to_nosana
    assert d[("label", "compiled")].routes_to_nosana


def test_no_model_eligible_routes_to_frontier():
    """When all Nosana CI lower bounds are below threshold, every task routes to frontier."""
    cells = [
        _cell("label", "raw", "frontier:claude", 0.90, 0.85, 0.95),
        _cell("label", "compiled", "frontier:claude", 0.90, 0.85, 0.95),
        _cell("label", "raw", "nosana:small@market", 0.60, 0.50, 0.70),
        _cell("label", "compiled", "nosana:small@market", 0.65, 0.55, 0.75),
    ]
    decisions = compute_routing_table(cells, threshold_ratio=0.9)
    for d in decisions:
        assert not d.routes_to_nosana
        assert d.chosen_recipient_id.startswith("frontier:")


def test_no_frontier_no_crash():
    """If there's no frontier cell, the routing still produces decisions."""
    cells = [
        _cell("label", "raw", "nosana:small@market", 0.80, 0.75, 0.85),
        _cell("label", "compiled", "nosana:small@market", 0.85, 0.82, 0.88),
    ]
    decisions = compute_routing_table(cells, threshold_ratio=0.9)
    # With no frontier score, threshold is None — all nosana models with a score should pass
    assert len(decisions) == 2


def test_routable_share_calculation():
    """Routable share counts instances by task type correctly."""
    decisions = [
        RoutingDecision("label", "raw", "nosana:x", 0.85, 0.80, 0.90, "ok", True),
        RoutingDecision("label", "compiled", "nosana:x", 0.88, 0.83, 0.92, "ok", True),
        RoutingDecision("duplicate", "raw", "frontier:y", 0.90, 0.86, 0.94, "no nosana", False),
        RoutingDecision("duplicate", "compiled", "nosana:x", 0.87, 0.82, 0.92, "ok", True),
    ]
    instances = {"label": 200, "duplicate": 150}
    shares = compute_routable_share(decisions, instances)
    # raw: label (200) routes to nosana, duplicate (150) does not → 200/350 ≈ 0.571
    assert abs(shares["raw"] - 200 / 350) < 0.01
    # compiled: both route to nosana → 350/350 = 1.0
    assert abs(shares["compiled"] - 1.0) < 0.01


def test_abs_floor_enforced():
    """Absolute floor blocks a model even if it passes the threshold ratio."""
    cells = [
        _cell("label", "raw", "frontier:claude", 0.50, 0.45, 0.55),
        _cell("label", "raw", "nosana:small@market", 0.48, 0.44, 0.52),
    ]
    # threshold_ratio=0.9 → threshold = 0.45; model CI_lo=0.44 fails
    # abs_floor=0.60 → also fails
    decisions = compute_routing_table(cells, threshold_ratio=0.9, abs_floor=0.60)
    d = {(d.task_type, d.mode): d for d in decisions}
    assert not d[("label", "raw")].routes_to_nosana
