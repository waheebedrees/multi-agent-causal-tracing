
from __future__ import annotations

import hashlib
import os
from typing import Any, Protocol, runtime_checkable
from dotenv import load_dotenv

load_dotenv()

PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o-mini":            (0.15, 0.60),
    "gpt-4o":                 (2.50, 10.00),
    "gpt-4-turbo":           (10.00, 30.00),
    "gpt-3.5-turbo":          (0.50, 1.50),
    # Groq
    "llama-3.1-8b-instant":   (0.05, 0.08),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "mixtral-8x7b-32768":     (0.24, 0.24),
    "openai/gpt-oss-120b":    (0.15, 0.75),      # ← add this
    # Anthropic (via OpenRouter or direct OpenAI-compat)

    "claude-3-5-sonnet":      (3.00, 15.00),
    "claude-3-5-haiku":       (0.80, 4.00),
    # Local / self-hosted — no cost accounting
    "llama3":                 (0.0, 0.0),
    "qwen2.5":                (0.0, 0.0),
}


def _price_for(model: str) -> tuple[float, float]:
    return PRICING.get(model, (0.0, 0.0))


@runtime_checkable
class LLMGateway(Protocol):
    """Anything with a `model` attribute and a `complete()` method."""

    model: str

    def complete(self, prompt: str, system: str | None = None) -> dict[str, Any]:
        """Return a dict with at least:
            model, prompt_hash, output_hash, cost_usd, tokens, cache_hit, text
        """
        ...
        
class MockLLMGateway:
    """Stable, offline, reproducible. Same interface as the real gateway."""

    def __init__(self, model: str = "gpt-4o-mini",
                 cost_usd: float = 0.0021,
                 tokens: int = 1200) -> None:
        self.model = model
        self._cost = cost_usd
        self._tokens = tokens

    def complete(self, prompt: str, system: str | None = None) -> dict[str, Any]:
        seed = f"{system or ''}|{prompt}"
        return {
            "model": self.model,
            "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest()[:12],
            "output_hash": hashlib.sha256((seed + "::out").encode()).hexdigest()[:12],
            "cost_usd": self._cost,
            "tokens": self._tokens,
            "cache_hit": False,
            "text": f"[mock] echo: {prompt[:60]}",
        }



class OpenAICompatibleGateway:
    """Works with any provider that speaks the OpenAI chat completions API."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        max_tokens: int = 200,
        temperature: float = 0.0,
    ) -> None:
        from openai import OpenAI  # imported lazily — optional dependency

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)
        self.model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

    def complete(self, prompt: str, system: str | None = None) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        text = (resp.choices[0].message.content or "").strip()
        usage = resp.usage
        pt = getattr(usage, "prompt_tokens", 0) if usage else 0
        ct = getattr(usage, "completion_tokens", 0) if usage else 0
        tt = getattr(usage, "total_tokens", pt + ct) if usage else 0
        in_price, out_price = _price_for(self.model)
        cost = (pt * in_price + ct * out_price) / 1_000_000
        return {
            "model": self.model,
            "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest()[:12],
            "output_hash": hashlib.sha256(text.encode()).hexdigest()[:12],
            "cost_usd": round(cost, 6),
            "tokens": tt,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "cache_hit": False,
            "text": text,
        }



def get_gateway(model: str | None = None) -> LLMGateway:
    """Return the best gateway available given the current environment.

    Order of preference:
        1. Real OpenAI-compatible gateway if a key is present
        2. Mock gateway otherwise
    """
    api_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    base_url = (
        os.environ.get("LLM_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
    )
    resolved_model = (
        model
        or os.environ.get("LLM_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or "gpt-4o-mini"
    )

    if not api_key:
        return MockLLMGateway(model=resolved_model)

    try:
        return OpenAICompatibleGateway(
            model=resolved_model, api_key=api_key, base_url=base_url,
        )
    except ImportError:
        # `openai` isn't installed — degrade to mock rather than crash
        return MockLLMGateway(model=resolved_model)
