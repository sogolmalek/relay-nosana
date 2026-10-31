from __future__ import annotations

import json
import time

from .base import Completion
from ..compiler.compile import RecipientProfile


class FrontierRecipient:
    """Anthropic Claude for baseline/ceiling measurement and synthesis."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        context_len: int = 200000,
        api_key: str | None = None,
    ):
        self.id = f"frontier:{model}"
        self.profile = RecipientProfile(context_len=context_len, output_reserve=1024)
        self._model = model
        self._api_key = api_key

    def _get_client(self):
        import anthropic
        return anthropic.Anthropic(api_key=self._api_key)

    async def complete(self, prompt: str, schema: dict) -> Completion:
        import asyncio
        return await asyncio.get_event_loop().run_in_executor(None, self._complete_sync, prompt, schema)

    def _complete_sync(self, prompt: str, schema: dict) -> Completion:
        t0 = time.time()
        client = self._get_client()
        system = (
            "You are a precise issue-triage assistant. "
            f"Respond only with valid JSON matching: {json.dumps(schema, separators=(',', ':'))}"
        )
        try:
            resp = client.messages.create(
                model=self._model,
                max_tokens=512,
                temperature=0,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text if resp.content else ""
            tokens_in = resp.usage.input_tokens
            tokens_out = resp.usage.output_tokens
            latency_ms = (time.time() - t0) * 1000

            try:
                parsed = json.loads(text)
                return Completion(text=text, parsed=parsed, tokens_in=tokens_in,
                                  tokens_out=tokens_out, latency_ms=latency_ms)
            except json.JSONDecodeError:
                # Repair
                repair = f"Fix this JSON and return only the corrected JSON:\n{text}"
                resp2 = client.messages.create(
                    model=self._model,
                    max_tokens=512,
                    temperature=0,
                    system=system,
                    messages=[{"role": "user", "content": repair}],
                )
                text2 = resp2.content[0].text if resp2.content else ""
                tokens_in += resp2.usage.input_tokens
                tokens_out += resp2.usage.output_tokens
                latency_ms = (time.time() - t0) * 1000
                try:
                    parsed2 = json.loads(text2)
                    return Completion(text=text2, parsed=parsed2, tokens_in=tokens_in,
                                      tokens_out=tokens_out, latency_ms=latency_ms)
                except json.JSONDecodeError:
                    return Completion(text=text2, parsed=None, tokens_in=tokens_in,
                                      tokens_out=tokens_out, latency_ms=latency_ms,
                                      error="parse_failed_after_repair")
        except Exception as e:
            latency_ms = (time.time() - t0) * 1000
            return Completion(text="", parsed=None, tokens_in=0, tokens_out=0,
                              latency_ms=latency_ms, error=str(e))
