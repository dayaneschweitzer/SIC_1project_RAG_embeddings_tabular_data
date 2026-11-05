"""Transporte JSON com retentativas limitadas por classe de erro."""
from __future__ import annotations

from time import perf_counter

from .deepseek_client import (
    DeepSeekClient, DeepSeekConfig, DeepSeekOutputError,
    DeepSeekRateLimitError, DeepSeekServerError, LLMJSONResponse,
)


def _public_trace(trace, attempts, elapsed):
    tokens = trace.get("tokens") or {}
    return {
        **trace,
        "input_tokens": tokens.get("prompt"),
        "output_tokens": tokens.get("completion"),
        "total_tokens": tokens.get("total"),
        "usage": {
            "prompt_tokens": tokens.get("prompt"),
            "completion_tokens": tokens.get("completion"),
            "total_tokens": tokens.get("total"),
        },
        "attempts": attempts,
        "latency_ms": elapsed,
        "logical_call_count": 1,
    }


class ParityDeepSeekClient:
    """Uma decisão JSON; até duas tentativas somente para HTTP 429 e 5xx.

    JSON inválido e transporte ambíguo não recebem nova tentativa.
    """

    def __init__(self):
        self._client = DeepSeekClient(DeepSeekConfig(
            model="deepseek-flash", timeout_seconds=30, max_attempts=1,
            retry_base_seconds=0, temperature=0, max_tokens=4096,
        ))

    def complete_json(self, messages):
        started = perf_counter()
        for attempt in (1, 2):
            try:
                response = self._client.complete_json(messages)
            except (DeepSeekRateLimitError, DeepSeekServerError):
                if attempt == 2:
                    raise
            except DeepSeekOutputError as error:
                error.trace = _public_trace(
                    error.trace or {}, attempt, (perf_counter() - started) * 1000,
                )
                raise
            else:
                return LLMJSONResponse(
                    value=response.value,
                    trace=_public_trace(response.trace, attempt, (perf_counter() - started) * 1000),
                )
        raise RuntimeError("DEEPSEEK_TRANSPORT_DID_NOT_CLOSE")
