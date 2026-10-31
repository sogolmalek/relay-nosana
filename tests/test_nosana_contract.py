"""
Nosana provider contract tests against recorded responses.
Also tests that deployments are always stopped on exception.
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from relay_nosana.providers.nosana import NosanaClient, NosanaRecipient

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# NosanaClient contract tests (recorded responses)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_balance_parses_correctly():
    """Balance response: available = assigned - reserved - settled."""
    recorded = _load_fixture("nosana_balance.json")

    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = recorded
        mock_resp.raise_for_status = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_ctx

        client = NosanaClient(api_key="test-key")
        bal = await client.get_balance()
        avail = await client.available_credits()

    assert "assignedCredits" in bal
    expected = (
        float(recorded["assignedCredits"])
        - float(recorded["reservedCredits"])
        - float(recorded["settledCredits"])
    )
    assert abs(avail - expected) < 0.0001


@pytest.mark.asyncio
async def test_create_deployment_returns_id():
    recorded = _load_fixture("nosana_create_deployment.json")

    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = recorded
        mock_resp.raise_for_status = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.post = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_ctx

        client = NosanaClient(api_key="test-key")
        result = await client.create_deployment({"name": "relay-poc-test", "market": "m1"})

    dep_id = result.get("id") or result.get("deploymentId")
    assert dep_id is not None
    assert isinstance(dep_id, str)
    assert len(dep_id) > 0


@pytest.mark.asyncio
async def test_get_deployment_extracts_endpoint():
    recorded = _load_fixture("nosana_get_deployment.json")

    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = recorded
        mock_resp.raise_for_status = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_ctx.get = AsyncMock(return_value=mock_resp)
        mock_cls.return_value = mock_ctx

        client = NosanaClient(api_key="test-key")
        dep = await client.get_deployment("dep-abc123")

    assert "endpoints" in dep
    endpoints = dep["endpoints"]
    assert isinstance(endpoints, list)
    if endpoints:
        assert "url" in endpoints[0]


# ---------------------------------------------------------------------------
# Teardown-on-exception test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_teardown_called_on_exception():
    """
    If an exception occurs during batch inference, _teardown must still be called.
    This proves the 'always stopped in a finally block' contract.
    """
    # Build a minimal mock model config
    model_cfg = MagicMock()
    model_cfg.name = "test-model"
    model_cfg.hf_id = "test/model"
    model_cfg.market = "market-x"
    model_cfg.max_model_len = 8192
    model_cfg.quant = None

    recipient = NosanaRecipient(
        model_cfg,
        api_key="fake-key",
        max_credits_per_run=100.0,
    )

    # Patch _deploy to succeed
    deploy_called = []
    teardown_called = []

    async def fake_deploy():
        deploy_called.append(True)
        recipient._deployment_id = "dep-test-001"
        recipient._base_url = "http://fake-endpoint"
        recipient._oai_client = MagicMock()
        recipient._semaphore = asyncio.Semaphore(16)
        recipient._t_requested = 1000.0
        recipient._t_ready = 1060.0

    async def fake_teardown():
        teardown_called.append(True)

    recipient._deploy = fake_deploy
    recipient._teardown = fake_teardown

    with pytest.raises(RuntimeError, match="simulated failure"):
        async with recipient:
            raise RuntimeError("simulated failure")

    assert deploy_called, "_deploy was not called"
    assert teardown_called, "_teardown was not called — deployment not stopped on exception!"


@pytest.mark.asyncio
async def test_teardown_called_on_normal_exit():
    """_teardown is called on normal exit too."""
    model_cfg = MagicMock()
    model_cfg.name = "test-model"
    model_cfg.hf_id = "test/model"
    model_cfg.market = "market-x"
    model_cfg.max_model_len = 8192
    model_cfg.quant = None

    recipient = NosanaRecipient(model_cfg, api_key="fake", max_credits_per_run=100.0)

    teardown_called = []

    async def fake_deploy():
        recipient._deployment_id = "dep-test-002"
        recipient._oai_client = MagicMock()
        recipient._semaphore = asyncio.Semaphore(1)

    async def fake_teardown():
        teardown_called.append(True)

    recipient._deploy = fake_deploy
    recipient._teardown = fake_teardown

    async with recipient:
        pass  # no exception

    assert teardown_called
