from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
from jinja2 import Template
from openai import AsyncOpenAI

from .base import Completion
from ..compiler.compile import RecipientProfile

NOSANA_BASE = "https://api.nosana.com"
_JOB_TEMPLATE_PATH = Path(__file__).parent.parent.parent / "jobs" / "vllm.json.j2"


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


class NosanaClient:
    """Low-level async client for the Nosana REST API.

    base_url defaults to https://api.nosana.com (per learn.nosana.com/api/intro.html).
    Override via relay.nosana.yaml nosana.base_url if the first balance call fails.
    """

    def __init__(self, api_key: str, base_url: str = NOSANA_BASE):
        self._key = api_key
        self._base = base_url.rstrip("/")

    def _headers(self) -> dict:
        return _auth_headers(self._key)

    async def get_balance(self) -> dict:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{self._base}/credits/balance", headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def available_credits(self) -> float:
        """Available credit in USD. The balance fields are dollars, not cents: measured live in Phase 0
        (assignedCredits 10 = $10; a $0.044 job moved reservedCredits by 0.044). See docs/phase0-report.md."""
        bal = await self.get_balance()
        assigned = float(bal.get("assignedCredits", 0))
        reserved = float(bal.get("reservedCredits", 0))
        settled = float(bal.get("settledCredits", 0))
        return assigned - reserved - settled

    async def create_deployment(self, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{self._base}/deployments/create",  # confirmed: POST /deployments/create
                headers=self._headers(),
                json=payload,
            )
            r.raise_for_status()
            return r.json()

    async def start_deployment(self, deployment_id: str) -> dict:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{self._base}/deployments/{deployment_id}/start",
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    async def get_deployment(self, deployment_id: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(
                f"{self._base}/deployments/{deployment_id}",
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    async def stop_deployment(self, deployment_id: str) -> dict:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{self._base}/deployments/{deployment_id}/stop",
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    async def archive_deployment(self, deployment_id: str) -> dict:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{self._base}/deployments/{deployment_id}/archive",
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    async def list_deployments(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{self._base}/deployments", headers=self._headers())
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return data
            return data.get("deployments", data.get("items", []))

    async def list_markets(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{self._base}/markets", headers=self._headers())
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return data
            return data.get("markets", data.get("items", []))


def _render_job_definition(
    hf_id: str,
    model_name: str,
    max_model_len: int,
    quant: str | None,
    resources: list[dict] | None = None,
) -> dict:
    template_path = _JOB_TEMPLATE_PATH
    if template_path.exists():
        tmpl = Template(template_path.read_text())
        rendered = tmpl.render(
            hf_id=hf_id,
            model_name=model_name,
            max_model_len=max_model_len,
            quant=quant or "",
            resources=resources or [],
        )
        return json.loads(rendered)
    # Fallback inline template
    cmd = [
        "--model", hf_id,
        "--served-model-name", model_name,
        "--max-model-len", str(max_model_len),
        "--port", "8000",
    ]
    if quant:
        cmd += ["--quantization", quant]
    job = {
        "ops": [{
            "type": "container",
            "image": "vllm/vllm-openai:latest",
            "gpu": True,
            "expose": 8000,
            "cmd": cmd,
        }],
    }
    if resources:
        job["ops"][0]["resources"] = resources
    return job


async def _wait_for_ready(
    client: NosanaClient,
    deployment_id: str,
    timeout_min: int = 30,
) -> str:
    """Poll until endpoint URL is live and /v1/models returns 200. Returns base URL."""
    deadline = time.time() + timeout_min * 60
    interval = 10
    while time.time() < deadline:
        await asyncio.sleep(interval)
        interval = min(interval * 1.5, 60)
        try:
            data = await client.get_deployment(deployment_id)
            endpoints = data.get("endpoints", [])
            if endpoints:
                url = endpoints[0].get("url", "")
                if url:
                    # Probe the vLLM health endpoint
                    try:
                        async with httpx.AsyncClient(timeout=10) as hc:
                            r = await hc.get(f"{url}/v1/models")
                            if r.status_code == 200:
                                return url
                    except Exception:
                        pass
        except Exception:
            pass
    raise TimeoutError(f"Deployment {deployment_id} did not become ready in {timeout_min} min")


class NosanaRecipient:
    """
    Full lifecycle: create → start → wait-ready → batch → stop.
    Always stops in a finally block.
    """

    def __init__(
        self,
        model_cfg: Any,  # NosanaModelConfig
        api_key: str,
        concurrency: int = 16,
        ready_timeout_min: int = 30,
        deployment_timeout_min: int = 120,
        max_credits_per_run: float = 10.0,
        base_url: str = NOSANA_BASE,
    ):
        self._cfg = model_cfg
        self._client = NosanaClient(api_key, base_url=base_url)
        self._concurrency = concurrency
        self._ready_timeout = ready_timeout_min
        self._deploy_timeout = deployment_timeout_min
        self._max_credits = max_credits_per_run
        self._oai_client: AsyncOpenAI | None = None
        self._deployment_id: str | None = None
        self._base_url: str | None = None
        self._t_requested: float | None = None
        self._t_ready: float | None = None
        self._t_stopped: float | None = None
        self._credits_before: float | None = None
        self._credits_after: float | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._supports_guided: bool = False

        self.id = f"nosana:{model_cfg.name}@{model_cfg.market}"
        self.profile = RecipientProfile(context_len=model_cfg.max_model_len)

    async def __aenter__(self) -> "NosanaRecipient":
        await self._deploy()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._teardown()

    async def _deploy(self) -> None:
        self._credits_before = await self._client.available_credits()
        if self._credits_before < self._max_credits:
            raise RuntimeError(
                f"Insufficient Nosana credits: {self._credits_before:.2f} < {self._max_credits}"
            )

        job_def = _render_job_definition(
            hf_id=self._cfg.hf_id,
            model_name=self._cfg.name,
            max_model_len=self._cfg.max_model_len,
            quant=self._cfg.quant,
        )
        payload = {
            "name": f"relay-poc-{self._cfg.name}",
            "market": self._cfg.market,
            "timeout": self._deploy_timeout,
            "replicas": 1,
            "strategy": "SIMPLE",
            "job_definition": job_def,
        }
        self._t_requested = time.time()
        resp = await self._client.create_deployment(payload)
        self._deployment_id = resp.get("id") or resp.get("deploymentId")
        if not self._deployment_id:
            raise RuntimeError(f"No deployment ID in response: {resp}")

        await self._client.start_deployment(self._deployment_id)
        self._base_url = await _wait_for_ready(
            self._client, self._deployment_id, self._ready_timeout
        )
        self._t_ready = time.time()
        self._semaphore = asyncio.Semaphore(self._concurrency)

        # Detect guided decoding support
        try:
            async with httpx.AsyncClient(timeout=10) as hc:
                r = await hc.get(f"{self._base_url}/v1/models")
                self._supports_guided = True  # vLLM >= 0.4 supports it
        except Exception:
            pass

        self._oai_client = AsyncOpenAI(
            api_key="nosana",
            base_url=f"{self._base_url}/v1",
        )

    async def _teardown(self) -> None:
        if self._deployment_id:
            try:
                await self._client.stop_deployment(self._deployment_id)
                self._t_stopped = time.time()
                try:
                    await self._client.archive_deployment(self._deployment_id)
                except Exception:
                    pass
                try:
                    self._credits_after = await self._client.available_credits()
                except Exception:
                    pass
            except Exception:
                pass

    @property
    def gpu_hours(self) -> float | None:
        if self._t_requested and self._t_stopped:
            return (self._t_stopped - self._t_requested) / 3600.0
        return None

    @property
    def startup_seconds(self) -> float | None:
        if self._t_requested and self._t_ready:
            return self._t_ready - self._t_requested
        return None

    async def complete(self, prompt: str, schema: dict) -> Completion:
        if self._oai_client is None or self._semaphore is None:
            raise RuntimeError("NosanaRecipient not started — use as async context manager")

        async with self._semaphore:
            return await self._do_complete(prompt, schema)

    async def _do_complete(self, prompt: str, schema: dict, retry: bool = True) -> Completion:
        t0 = time.time()
        try:
            kwargs: dict = dict(
                model=self._cfg.name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                seed=42,
                max_tokens=512,
            )
            if self._supports_guided:
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "output", "schema": schema, "strict": True},
                }
            else:
                kwargs["response_format"] = {"type": "json_object"}

            resp = await self._oai_client.chat.completions.create(**kwargs)
            text = resp.choices[0].message.content or ""
            tokens_in = resp.usage.prompt_tokens if resp.usage else len(prompt) // 4
            tokens_out = resp.usage.completion_tokens if resp.usage else len(text) // 4
            latency_ms = (time.time() - t0) * 1000

            try:
                parsed = json.loads(text)
                return Completion(text=text, parsed=parsed, tokens_in=tokens_in,
                                  tokens_out=tokens_out, latency_ms=latency_ms)
            except json.JSONDecodeError:
                if retry:
                    repair = f"Fix to valid JSON:\n{text}"
                    return await self._do_complete(repair, schema, retry=False)
                return Completion(text=text, parsed=None, tokens_in=tokens_in,
                                  tokens_out=tokens_out, latency_ms=latency_ms,
                                  error="parse_failed")
        except Exception as e:
            # Exponential backoff on 5xx
            if retry and ("5" in str(e)[:3] or "timeout" in str(e).lower()):
                await asyncio.sleep(5)
                return await self._do_complete(prompt, schema, retry=False)
            latency_ms = (time.time() - t0) * 1000
            return Completion(text="", parsed=None, tokens_in=0, tokens_out=0,
                              latency_ms=latency_ms, error=str(e))
