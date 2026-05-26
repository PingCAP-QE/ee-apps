from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Protocol

import httpx

from ci_dashboard.common.config import LLMSettings
from ci_dashboard.common.models import ErrorClassification
from ci_dashboard.jobs.error_classification_guidance import ERROR_CLASSIFICATION_GUIDANCE

LOG = logging.getLogger(__name__)

DEFAULT_LLM_TIMEOUT_SECONDS = 180
DEFAULT_LLM_MAX_INPUT_CHARS = 24000
DEFAULT_LLM_RATE_LIMIT_RETRIES = 4
DEFAULT_LLM_RATE_LIMIT_BACKOFF_SECONDS = 5.0
MAX_LLM_RATE_LIMIT_BACKOFF_SECONDS = 60.0


class LLMClassifier(Protocol):
    def classify(self, *, log_text: str, build: Mapping[str, Any]) -> ErrorClassification:
        ...


@dataclass(frozen=True)
class NoopLLMClassifier:
    default_l1_category: str
    default_l2_subcategory: str

    def classify(self, *, log_text: str, build: Mapping[str, Any]) -> ErrorClassification:
        del log_text, build
        return ErrorClassification(
            l1_category=self.default_l1_category,
            l2_subcategory=self.default_l2_subcategory,
            source="llm:noop",
        )


@dataclass(frozen=True)
class OpenAICompatibleLLMClassifier:
    provider_name: str
    base_url: str
    model: str
    api_key: str
    reasoning_effort: str | None
    default_l1_category: str
    default_l2_subcategory: str
    allowed_classifications: tuple[tuple[str, str], ...]
    timeout_seconds: int = DEFAULT_LLM_TIMEOUT_SECONDS
    max_input_chars: int = DEFAULT_LLM_MAX_INPUT_CHARS
    transport: httpx.BaseTransport | None = None

    def classify(self, *, log_text: str, build: Mapping[str, Any]) -> ErrorClassification:
        response_content = self._post_chat_completion(
            _build_chat_payload(
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                allowed_classifications=self.allowed_classifications,
                default_l1_category=self.default_l1_category,
                default_l2_subcategory=self.default_l2_subcategory,
                log_text=_truncate_log_text(log_text, max_chars=self.max_input_chars),
                build=build,
            )
        )
        parsed = _extract_json_object(response_content)
        return _validate_classification(
            parsed,
            allowed_classifications=self.allowed_classifications,
            provider_name=self.provider_name,
        )

    def _post_chat_completion(self, payload: Mapping[str, Any]) -> str:
        endpoint = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        request_payload = dict(payload)
        request_payload["stream"] = True
        with httpx.Client(
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            for attempt in range(DEFAULT_LLM_RATE_LIMIT_RETRIES + 1):
                try:
                    with client.stream(
                        "POST",
                        endpoint,
                        headers=headers,
                        json=request_payload,
                    ) as response:
                        response.raise_for_status()
                        return _collect_stream_content(response)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != httpx.codes.TOO_MANY_REQUESTS:
                        raise
                    if attempt >= DEFAULT_LLM_RATE_LIMIT_RETRIES:
                        raise RuntimeError(
                            "LLM classification request retries exhausted "
                            f"after {DEFAULT_LLM_RATE_LIMIT_RETRIES + 1} attempts"
                        ) from exc
                    delay_seconds = _resolve_rate_limit_backoff_seconds(exc.response, attempt=attempt)
                    LOG.warning(
                        "LLM provider rate limited classification request; retrying",
                        extra={
                            "provider_name": self.provider_name,
                            "model": self.model,
                            "attempt": attempt + 1,
                            "delay_seconds": delay_seconds,
                        },
                    )
                    time.sleep(delay_seconds)
        raise AssertionError("unreachable")


def build_llm_classifier(
    settings: LLMSettings,
    *,
    default_l1_category: str,
    default_l2_subcategory: str,
    allowed_classifications: tuple[tuple[str, str], ...],
) -> LLMClassifier:
    provider = (settings.provider or "noop").strip().lower()
    if provider in {"", "noop", "none"}:
        return NoopLLMClassifier(
            default_l1_category=default_l1_category,
            default_l2_subcategory=default_l2_subcategory,
        )
    if provider in {"codex", "openai-compatible", "openai_compatible"}:
        base_url = _require_setting(settings.base_url, name="CI_DASHBOARD_LLM_BASE_URL")
        model = _require_setting(settings.model, name="CI_DASHBOARD_LLM_MODEL")
        api_key = _require_setting(settings.api_key, name="CI_DASHBOARD_LLM_API_KEY")
        return OpenAICompatibleLLMClassifier(
            provider_name=provider,
            base_url=base_url,
            model=model,
            api_key=api_key,
            reasoning_effort=_normalize_reasoning_effort(settings.reasoning_effort),
            default_l1_category=default_l1_category,
            default_l2_subcategory=default_l2_subcategory,
            allowed_classifications=allowed_classifications,
        )
    raise ValueError(
        f"unsupported CI_DASHBOARD_LLM_PROVIDER {settings.provider!r}; "
        "supported values are 'noop' and 'codex'"
    )


def _require_setting(value: str | None, *, name: str) -> str:
    resolved = (value or "").strip()
    if not resolved:
        raise ValueError(f"{name} is required when CI_DASHBOARD_LLM_PROVIDER is enabled")
    return resolved


def _truncate_log_text(log_text: str, *, max_chars: int) -> str:
    if len(log_text) <= max_chars:
        return log_text
    return f"[TRUNCATED TO LAST {max_chars} CHARS]\n{log_text[-max_chars:]}"


def _build_chat_payload(
    *,
    model: str,
    reasoning_effort: str | None,
    allowed_classifications: tuple[tuple[str, str], ...],
    default_l1_category: str,
    default_l2_subcategory: str,
    log_text: str,
    build: Mapping[str, Any],
) -> dict[str, Any]:
    allowed_lines = "\n".join(
        f"- {l1_category}/{l2_subcategory}" for l1_category, l2_subcategory in allowed_classifications
    )
    job_name = str(build.get("job_name") or "")
    url = str(build.get("url") or "")
    system_prompt = (
        "You classify Jenkins CI build failures into a fixed taxonomy. "
        "Return JSON only with keys 'l1' and 'l2'. "
        f"If the evidence is weak, return {default_l1_category}/{default_l2_subcategory}. "
        "Do not invent new categories."
    )
    user_prompt = (
        "The deterministic rule engine already found no exact rule match.\n"
        "Choose the best category from the allowed list below.\n\n"
        "Allowed categories:\n"
        f"{allowed_lines}\n\n"
        f"{ERROR_CLASSIFICATION_GUIDANCE}\n\n"
        "Build context:\n"
        f"- job_name: {job_name or '<unknown>'}\n"
        f"- url: {url or '<unknown>'}\n\n"
        "Redacted console log tail:\n"
        f"{log_text}\n"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
    return payload


def _normalize_reasoning_effort(value: str | None) -> str | None:
    resolved = (value or "").strip().lower()
    if not resolved:
        return None
    if resolved not in {"minimal", "low", "medium", "high"}:
        raise ValueError(
            "CI_DASHBOARD_LLM_REASONING_EFFORT must be one of: minimal, low, medium, high"
        )
    return resolved


def _should_retry_rate_limit(response: httpx.Response, *, attempt: int) -> bool:
    return (
        response.status_code == httpx.codes.TOO_MANY_REQUESTS
        and attempt < DEFAULT_LLM_RATE_LIMIT_RETRIES
    )


def _resolve_rate_limit_backoff_seconds(response: httpx.Response, *, attempt: int) -> float:
    retry_after = _parse_retry_after_seconds(response.headers.get("Retry-After"))
    if retry_after is not None:
        return retry_after
    return min(
        DEFAULT_LLM_RATE_LIMIT_BACKOFF_SECONDS * (2**attempt),
        MAX_LLM_RATE_LIMIT_BACKOFF_SECONDS,
    )


def _parse_retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return None


def _collect_stream_content(response: httpx.Response) -> str:
    parts: list[str] = []
    for event_payload in _iter_sse_data_payloads(response):
        if event_payload == "[DONE]":
            continue
        parsed = json.loads(event_payload)
        if not isinstance(parsed, Mapping):
            raise ValueError("LLM stream event JSON must be an object")
        parts.extend(_extract_stream_choice_content(parsed))
    content = "".join(parts).strip()
    if not content:
        raise ValueError("LLM stream did not contain content")
    return content


def _iter_sse_data_payloads(response: httpx.Response) -> Iterator[str]:
    data_lines: list[str] = []
    for line in response.iter_lines():
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data_line = line[5:]
        if data_line.startswith(" "):
            data_line = data_line[1:]
        data_lines.append(data_line)
    if data_lines:
        yield "\n".join(data_lines)


def _extract_stream_choice_content(payload: Mapping[str, Any]) -> list[str]:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return []
    parts: list[str] = []
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            continue
        content = delta.get("content")
        if content is None:
            continue
        parts.append(_coerce_message_content(content))
    return parts


def _coerce_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping):
                text_value = item.get("text")
                if text_value:
                    parts.append(str(text_value))
        if parts:
            return "\n".join(parts)
    raise ValueError("LLM response content is empty")


def _extract_json_object(content: str) -> Mapping[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            stripped = "\n".join(lines[1:-1]).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM response did not contain a JSON object")
    candidate = stripped[start : end + 1]
    parsed = json.loads(candidate)
    if not isinstance(parsed, Mapping):
        raise ValueError("LLM response JSON must be an object")
    return parsed


def _validate_classification(
    payload: Mapping[str, Any],
    *,
    allowed_classifications: tuple[tuple[str, str], ...],
    provider_name: str,
) -> ErrorClassification:
    l1_category = str(payload.get("l1") or "").strip().upper()
    l2_subcategory = str(payload.get("l2") or "").strip().upper()
    candidate = (l1_category, l2_subcategory)
    if candidate not in set(allowed_classifications):
        raise ValueError(f"LLM returned unsupported classification {candidate!r}")
    return ErrorClassification(
        l1_category=l1_category,
        l2_subcategory=l2_subcategory,
        source=f"llm:{provider_name}",
    )
