from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
import socket
from time import perf_counter, sleep
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .safety import (
    BudgetLedger,
    ConfigurationMismatchError,
    PromptPrivacyGuard,
    estimate_message_tokens,
    hash_json,
)


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_CHAT_COMPLETIONS_PATH = "/chat/completions"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class DeepSeekError(RuntimeError):
    """Base technical failure.  Never represents a scientific decision."""

    retryable = False

    def __init__(self, message: str, *, attempts: int = 1, status_code: int | None = None):
        super().__init__(message)
        self.attempts = attempts
        self.status_code = status_code


class DeepSeekCredentialError(DeepSeekError):
    pass


def require_deepseek_api_key() -> str:
    """Lê a credencial do ambiente; nunca a inclui no erro ou nos artefatos."""
    api_key = os.environ.get(DEEPSEEK_API_KEY_ENV)
    if not isinstance(api_key, str) or not api_key.strip():
        raise DeepSeekCredentialError(
            f"{DEEPSEEK_API_KEY_ENV} is not configured; its value is never read into logs or artifacts"
        )
    return api_key.strip()


class DeepSeekAuthenticationError(DeepSeekError):
    pass


class DeepSeekBalanceError(DeepSeekError):
    pass


class DeepSeekRateLimitError(DeepSeekError):
    retryable = True


class DeepSeekServerError(DeepSeekError):
    retryable = True


class DeepSeekTransportError(DeepSeekError):
    retryable = True


class DeepSeekHTTPError(DeepSeekError):
    pass


class DeepSeekOutputError(DeepSeekError):
    """Non-retryable model output that is not valid JSON Output."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 1,
        status_code: int | None = None,
        trace: Mapping[str, Any] | None = None,
        diagnostic: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, attempts=attempts, status_code=status_code)
        self.trace = json.loads(_canonical_json(trace)) if trace is not None else None
        self.diagnostic = json.loads(_canonical_json(diagnostic)) if diagnostic is not None else None


@dataclass(frozen=True)
class DeepSeekConfig:
    model: str = "deepseek-flash"
    base_url: str = DEEPSEEK_BASE_URL
    timeout_seconds: float = 30.0
    max_attempts: int = 3
    retry_base_seconds: float = 0.5
    temperature: float = 0.0
    max_tokens: int = 4096
    response_format: str = "json_object"

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be a nonempty string")
        if self.base_url.rstrip("/") != DEEPSEEK_BASE_URL:
            raise ValueError("DeepSeek base_url must be https://api.deepseek.com")
        if isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 8:
            raise ValueError("max_attempts must be between 1 and 8")
        if isinstance(self.retry_base_seconds, bool) or self.retry_base_seconds < 0:
            raise ValueError("retry_base_seconds must be nonnegative")
        if isinstance(self.temperature, bool) or not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if type(self.max_tokens) is not int or self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if self.response_format != "json_object":
            raise ValueError("only strict JSON Output is supported")

    def public_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "endpoint": DEEPSEEK_CHAT_COMPLETIONS_PATH,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.max_attempts,
            "retry_base_seconds": self.retry_base_seconds,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": self.response_format,
        }


@dataclass(frozen=True)
class HTTPResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class HTTPTransport(Protocol):
    def send(self, *, url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> HTTPResponse:
        ...


class StandardLibraryHTTPTransport:
    """Minimal stdlib transport; no dependency installation is required."""

    def send(self, *, url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> HTTPResponse:
        request = Request(url=url, data=body, headers=dict(headers), method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                return HTTPResponse(
                    status_code=int(response.status),
                    body=response.read(),
                    headers={key.lower(): value for key, value in response.headers.items()},
                )
        except HTTPError as error:
            return HTTPResponse(
                status_code=int(error.code),
                body=error.read(),
                headers={key.lower(): value for key, value in error.headers.items()},
            )


@dataclass(frozen=True)
class LLMJSONResponse:
    value: dict[str, Any]
    trace: dict[str, Any]


class DeepSeekClient:
    """Reusable JSON-only DeepSeek client with explicit retry classification."""

    def __init__(
        self,
        config: DeepSeekConfig | None = None,
        *,
        transport: HTTPTransport | None = None,
        privacy_guard: PromptPrivacyGuard | None = None,
        budget: BudgetLedger | None = None,
        sleep_fn: Callable[[float], None] = sleep,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self.config = config or DeepSeekConfig()
        self._transport = transport or StandardLibraryHTTPTransport()
        self._privacy_guard = privacy_guard or PromptPrivacyGuard()
        self._budget = budget
        self._sleep = sleep_fn
        self._clock = clock

    def complete_json(self, messages: list[dict[str, str]]) -> LLMJSONResponse:
        self._validate_messages(messages)
        privacy_preflight = self._privacy_guard.inspect(messages)
        estimated_input_tokens = estimate_message_tokens(messages)
        api_key = require_deepseek_api_key()

        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "response_format": {"type": self.config.response_format},
            "stream": False,
        }
        body = _canonical_json(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "SIC_UDESC-D2/1",
        }
        url = self.config.base_url.rstrip("/") + DEEPSEEK_CHAT_COMPLETIONS_PATH
        started = self._clock()
        response: HTTPResponse | None = None

        for attempt in range(1, self.config.max_attempts + 1):
            reservation_id = None
            if self._budget is not None:
                reservation_id = self._budget.reserve_attempt(
                    estimated_input_tokens=estimated_input_tokens,
                    output_reserve_tokens=self.config.max_tokens,
                )
            try:
                response = self._transport.send(
                    url=url,
                    headers=headers,
                    body=body,
                    timeout=self.config.timeout_seconds,
                )
            except (TimeoutError, socket.timeout, URLError, OSError) as error:
                if self._budget is not None:
                    self._budget.record_failure(reservation_id, error_type=type(error).__name__)
                failure = DeepSeekTransportError(
                    f"DeepSeek transport failed: {type(error).__name__}", attempts=attempt
                )
                if attempt == self.config.max_attempts:
                    raise failure from error
                self._backoff(attempt)
                continue

            failure = self._http_failure(response.status_code, attempt)
            if failure is None:
                break
            if self._budget is not None:
                self._budget.record_failure(reservation_id, error_type=type(failure).__name__)
            if not failure.retryable or attempt == self.config.max_attempts:
                raise failure
            self._backoff(attempt)

        if response is None:
            raise DeepSeekTransportError("DeepSeek transport produced no response")
        return self._parse_success(
            response,
            messages=messages,
            request_body=payload,
            attempts=attempt,
            latency_ms=round((self._clock() - started) * 1000, 3),
            privacy_preflight=privacy_preflight,
            reservation_id=reservation_id,
        )

    def budget_snapshot(self) -> dict[str, Any] | None:
        return self._budget.snapshot() if self._budget is not None else None

    @staticmethod
    def _validate_messages(messages: list[dict[str, str]]) -> None:
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a nonempty list")
        for message in messages:
            if set(message) != {"role", "content"}:
                raise ValueError("each message requires only role and content")
            if message["role"] not in {"system", "user", "assistant"}:
                raise ValueError("unsupported message role")
            if not isinstance(message["content"], str) or not message["content"]:
                raise ValueError("message content must be nonempty text")

    def _backoff(self, attempt: int) -> None:
        delay = min(self.config.retry_base_seconds * (2 ** (attempt - 1)), 8.0)
        if delay:
            self._sleep(delay)

    @staticmethod
    def _http_failure(status_code: int, attempt: int) -> DeepSeekError | None:
        if 200 <= status_code < 300:
            return None
        if status_code == 401:
            return DeepSeekAuthenticationError(
                "DeepSeek authentication failed (HTTP 401)", attempts=attempt, status_code=status_code
            )
        if status_code == 402:
            return DeepSeekBalanceError(
                "DeepSeek balance is insufficient (HTTP 402)", attempts=attempt, status_code=status_code
            )
        if status_code == 429:
            return DeepSeekRateLimitError(
                "DeepSeek rate limit reached (HTTP 429)", attempts=attempt, status_code=status_code
            )
        if 500 <= status_code <= 599:
            return DeepSeekServerError(
                f"DeepSeek server failure (HTTP {status_code})", attempts=attempt, status_code=status_code
            )
        return DeepSeekHTTPError(
            f"DeepSeek request rejected (HTTP {status_code})", attempts=attempt, status_code=status_code
        )

    def _parse_success(
        self,
        response: HTTPResponse,
        *,
        messages: list[dict[str, str]],
        request_body: dict[str, Any],
        attempts: int,
        latency_ms: float,
        privacy_preflight: dict[str, Any],
        reservation_id: int | None,
    ) -> LLMJSONResponse:
        envelope: dict[str, Any] | None = None
        try:
            parsed_envelope = json.loads(response.body.decode("utf-8"))
            envelope = parsed_envelope if isinstance(parsed_envelope, dict) else None
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            if self._budget is not None:
                self._budget.record_success(reservation_id, input_tokens=None, output_tokens=None)
            trace = self._response_trace(
                response=response,
                messages=messages,
                request_body=request_body,
                attempts=attempts,
                latency_ms=latency_ms,
                privacy_preflight=privacy_preflight,
                usage={},
                returned_model=None,
                provider_response_id=None,
                finish_reason=None,
            )
            raise DeepSeekOutputError(
                f"DeepSeek JSON Output was malformed: {type(error).__name__}",
                attempts=attempts,
                status_code=response.status_code,
                trace=trace,
                diagnostic=self._output_diagnostic(
                    stage="provider_envelope",
                    code="MALFORMED_PROVIDER_ENVELOPE_JSON",
                    error_type=type(error).__name__,
                ),
            ) from error
        if envelope is None:
            if self._budget is not None:
                self._budget.record_success(reservation_id, input_tokens=None, output_tokens=None)
            trace = self._response_trace(
                response=response,
                messages=messages,
                request_body=request_body,
                attempts=attempts,
                latency_ms=latency_ms,
                privacy_preflight=privacy_preflight,
                usage={},
                returned_model=None,
                provider_response_id=None,
                finish_reason=None,
            )
            raise DeepSeekOutputError(
                "DeepSeek provider envelope must be an object",
                attempts=attempts,
                status_code=response.status_code,
                trace=trace,
                diagnostic=self._output_diagnostic(
                    stage="provider_envelope",
                    code="NON_OBJECT_PROVIDER_ENVELOPE",
                    error_type="TypeError",
                    actual_type=type(parsed_envelope).__name__,
                ),
            )
        usage = envelope.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        if self._budget is not None:
            self._budget.record_success(
                reservation_id,
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            )
        returned_model = envelope.get("model")
        provider_response_id = envelope.get("id")
        choices = envelope.get("choices")
        first_choice = choices[0] if isinstance(choices, list) and choices else None
        first_choice = first_choice if isinstance(first_choice, dict) else None
        finish_reason_value = first_choice.get("finish_reason") if first_choice is not None else None
        finish_reason = finish_reason_value if isinstance(finish_reason_value, str) else None
        trace = self._response_trace(
            response=response,
            messages=messages,
            request_body=request_body,
            attempts=attempts,
            latency_ms=latency_ms,
            privacy_preflight=privacy_preflight,
            usage=usage,
            returned_model=returned_model if isinstance(returned_model, str) else None,
            provider_response_id=provider_response_id if isinstance(provider_response_id, str) else None,
            finish_reason=finish_reason,
        )
        if finish_reason == "length":
            raise DeepSeekOutputError(
                "DeepSeek JSON Output was truncated by the output token limit",
                attempts=attempts,
                status_code=response.status_code,
                trace=trace,
                diagnostic=self._output_diagnostic(
                    stage="provider_choice",
                    code="OUTPUT_TRUNCATED_BY_MAX_TOKENS",
                    error_type="DeepSeekOutputError",
                ),
            )
        try:
            content = first_choice["message"]["content"]
            value = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            code = (
                "MALFORMED_MESSAGE_CONTENT_JSON"
                if isinstance(error, json.JSONDecodeError)
                else "INVALID_PROVIDER_RESPONSE_SHAPE"
            )
            raise DeepSeekOutputError(
                f"DeepSeek JSON Output was malformed: {type(error).__name__}",
                attempts=attempts,
                status_code=response.status_code,
                trace=trace,
                diagnostic=self._output_diagnostic(
                    stage="message_content",
                    code=code,
                    error_type=type(error).__name__,
                ),
            ) from error
        if not isinstance(value, dict):
            raise DeepSeekOutputError(
                "DeepSeek JSON Output must be an object",
                attempts=attempts,
                status_code=response.status_code,
                trace=trace,
                diagnostic=self._output_diagnostic(
                    stage="message_content",
                    code="NON_OBJECT_JSON_OUTPUT",
                    error_type="TypeError",
                    actual_type=type(value).__name__,
                ),
            )
        if isinstance(returned_model, str) and returned_model != self.config.model:
            raise ConfigurationMismatchError(
                f"provider returned model {returned_model!r}, expected {self.config.model!r}"
            )
        return LLMJSONResponse(value=value, trace=trace)

    def _response_trace(
        self,
        *,
        response: HTTPResponse,
        messages: list[dict[str, str]],
        request_body: dict[str, Any],
        attempts: int,
        latency_ms: float,
        privacy_preflight: dict[str, Any],
        usage: Mapping[str, Any],
        returned_model: str | None,
        provider_response_id: str | None,
        finish_reason: str | None,
    ) -> dict[str, Any]:
        request_cost_usd: float | None = None
        if self._budget is not None:
            prompt_tokens = usage.get("prompt_tokens") if type(usage.get("prompt_tokens")) is int else 0
            completion_tokens = usage.get("completion_tokens") if type(usage.get("completion_tokens")) is int else 0
            request_cost_usd = self._budget.policy.pricing.cost(prompt_tokens, completion_tokens)
        return {
            "schema_version": "deepseek_llm_trace_v2",
            "provider": "deepseek",
            "requested_model": self.config.model,
            "returned_model": returned_model,
            "provider_response_id": provider_response_id,
            "finish_reason": finish_reason,
            "configuration": self.config.public_dict(),
            "configuration_sha256": hash_json(self.config.public_dict()),
            "prompt_sha256": _sha256_json(messages),
            "request_body_sha256": _sha256_json(request_body),
            "tokens": {
                "prompt": usage.get("prompt_tokens"),
                "completion": usage.get("completion_tokens"),
                "total": usage.get("total_tokens"),
            },
            "latency_ms": latency_ms,
            "attempts": attempts,
            "http_status": response.status_code,
            "privacy_preflight": privacy_preflight,
            "request_cost_usd": request_cost_usd,
            "budget": self.budget_snapshot(),
        }

    @staticmethod
    def _output_diagnostic(
        *,
        stage: str,
        code: str,
        error_type: str,
        actual_type: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "deepseek_output_diagnostic_v1",
            "stage": stage,
            "code": code,
            "error_type": error_type,
            "actual_type": actual_type,
            "raw_response_persisted": False,
            "reasoning_content_persisted": False,
            "retention_policy": "schema_diagnostics_only_v1",
        }
