from __future__ import annotations

import json
import time

import httpx
from openai import AsyncOpenAI

from .base import Completion
from ..compiler.compile import RecipientProfile


class LocalRecipient:
    """Any OpenAI-compatible base URL (Ollama, local vLLM)."""

    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:11434/v1",
        context_len: int = 8192,
        api_key: str = "local",
    ):
        self.id = f"local:{model}@{base_url}"
        self.profile = RecipientProfile(context_len=context_len)
        self._model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def complete(self, prompt: str, schema: dict) -> Completion:
        t0 = time.time()
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                seed=42,
                response_format={"type": "json_object"},
                max_tokens=512,
            )
            text = resp.choices[0].message.content or ""
            tokens_in = resp.usage.prompt_tokens if resp.usage else len(prompt) // 4
            tokens_out = resp.usage.completion_tokens if resp.usage else len(text) // 4
            latency_ms = (time.time() - t0) * 1000

            try:
                parsed = json.loads(text)
                return Completion(text=text, parsed=parsed, tokens_in=tokens_in,
                                  tokens_out=tokens_out, latency_ms=latency_ms)
            except json.JSONDecodeError:
                # Retry with repair prompt
                repair_prompt = f"The following is not valid JSON. Fix it and return only valid JSON:\n{text}"
                resp2 = await self._client.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "user", "content": repair_prompt}],
                    temperature=0,
                    seed=42,
                    response_format={"type": "json_object"},
                    max_tokens=512,
                )
                text2 = resp2.choices[0].message.content or ""
                try:
                    parsed2 = json.loads(text2)
                    latency_ms = (time.time() - t0) * 1000
                    return Completion(text=text2, parsed=parsed2, tokens_in=tokens_in,
                                      tokens_out=tokens_out, latency_ms=latency_ms)
                except json.JSONDecodeError:
                    latency_ms = (time.time() - t0) * 1000
                    return Completion(text=text2, parsed=None, tokens_in=tokens_in,
                                      tokens_out=tokens_out, latency_ms=latency_ms,
                                      error="parse_failed_after_repair")
        except Exception as e:
            latency_ms = (time.time() - t0) * 1000
            return Completion(text="", parsed=None, tokens_in=0, tokens_out=0,
                              latency_ms=latency_ms, error=str(e))
