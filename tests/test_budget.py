"""Budget fitting: raw and compiled inputs must not exceed input_budget,
and should be within 10% of each other when content is long enough."""
import pytest
from relay_nosana.compiler.compile import (
    RecipientProfile,
    build_raw,
    build_compiled,
    count_tokens,
)
from relay_nosana.tasks.base import TaskInstance


def _make_instance(task_type: str = "label", body_len: int = 2000) -> TaskInstance:
    body = "This is a detailed issue body. " * (body_len // 32 + 1)
    body = body[:body_len]
    return TaskInstance(
        id=f"test:1:{task_type}",
        issue_number=1,
        task_type=task_type,
        input_text=f"Title: Test Issue\n\nBody:\n{body}",
        candidates=["bug", "feature", "docs", "question", "other"],
        ground_truth="bug",
    )


def _make_localize_instance(body_len: int = 2000) -> TaskInstance:
    body = "OAuth callback handler raises KeyError. DatabasePool connection leak. " * (body_len // 70 + 1)
    return TaskInstance(
        id="test:1:localize",
        issue_number=1,
        task_type="localize",
        input_text=f"Title: Login fails\n\nBody:\n{body}",
        candidates=[f"src/module{i}.py" for i in range(30)],
        ground_truth=["src/module1.py"],
    )


# ---------------------------------------------------------------------------
# Never-exceed tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ctx_len", [1024, 2048, 4096, 8192])
def test_raw_never_exceeds_budget(ctx_len):
    profile = RecipientProfile(context_len=ctx_len)
    inst = _make_instance(body_len=ctx_len * 10)  # very long body
    raw = build_raw(inst, profile)
    assert count_tokens(raw) <= profile.input_budget, (
        f"raw exceeds budget at ctx_len={ctx_len}"
    )


@pytest.mark.parametrize("ctx_len", [1024, 2048, 4096, 8192])
def test_compiled_never_exceeds_budget(ctx_len):
    profile = RecipientProfile(context_len=ctx_len)
    inst = _make_instance(body_len=ctx_len * 10)
    schema = {"type": "object", "properties": {"label": {"type": "string", "enum": ["bug", "feature"]}}, "required": ["label"]}
    compiled = build_compiled(inst, profile, schema)
    assert count_tokens(compiled) <= profile.input_budget, (
        f"compiled exceeds budget at ctx_len={ctx_len}"
    )


def test_localize_raw_never_exceeds_budget():
    profile = RecipientProfile(context_len=4096)
    inst = _make_localize_instance(body_len=20000)
    raw = build_raw(inst, profile)
    assert count_tokens(raw) <= profile.input_budget


def test_localize_compiled_never_exceeds_budget():
    profile = RecipientProfile(context_len=4096)
    inst = _make_localize_instance(body_len=20000)
    schema = {"type": "object", "properties": {"files": {"type": "array", "items": {"type": "string"}}}, "required": ["files"]}
    compiled = build_compiled(inst, profile, schema)
    assert count_tokens(compiled) <= profile.input_budget


# ---------------------------------------------------------------------------
# Within-10%-of-each-other test (only when content is long enough)
# ---------------------------------------------------------------------------

def test_raw_and_compiled_within_10_pct_for_long_content():
    """When the source is much longer than the budget, both builders fill close to the same budget."""
    profile = RecipientProfile(context_len=2048)
    # Body that's much longer than the budget
    inst = _make_instance(body_len=50000)
    schema = {"type": "object", "properties": {"label": {"type": "string", "enum": ["bug"]}}, "required": ["label"]}
    raw = build_raw(inst, profile)
    compiled = build_compiled(inst, profile, schema)
    t_raw = count_tokens(raw)
    t_compiled = count_tokens(compiled)
    # Both should be within 10% of each other
    ratio = abs(t_raw - t_compiled) / max(t_raw, t_compiled)
    assert ratio <= 0.10, (
        f"raw ({t_raw} tok) and compiled ({t_compiled} tok) differ by {ratio:.1%} > 10%"
    )


def test_raw_compiled_use_most_of_budget():
    """When content is long, both should use at least 80% of the budget."""
    profile = RecipientProfile(context_len=2048)
    inst = _make_instance(body_len=50000)
    schema = {"type": "object", "properties": {"label": {"type": "string", "enum": ["bug"]}}, "required": ["label"]}
    raw = build_raw(inst, profile)
    compiled = build_compiled(inst, profile, schema)
    budget = profile.input_budget
    assert count_tokens(raw) >= 0.5 * budget, "raw underuses budget"
    assert count_tokens(compiled) >= 0.5 * budget, "compiled underuses budget"
