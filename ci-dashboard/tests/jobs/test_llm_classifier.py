from __future__ import annotations

import json

import httpx
import pytest

import ci_dashboard.jobs.llm_classifier as llm_classifier_module
from ci_dashboard.common.config import LLMSettings
from ci_dashboard.jobs.error_classification_guidance import ERROR_CLASSIFICATION_GUIDANCE
from ci_dashboard.jobs.llm_classifier import (
    NoopLLMClassifier,
    OpenAICompatibleLLMClassifier,
    _build_chat_payload,
    _collect_stream_content,
    _coerce_message_content,
    _extract_json_object,
    _extract_stream_choice_content,
    _normalize_reasoning_effort,
    _parse_retry_after_seconds,
    _resolve_rate_limit_backoff_seconds,
    _should_retry_rate_limit,
    _truncate_log_text,
    _validate_classification,
    _iter_sse_data_payloads,
    build_llm_classifier,
)


def test_openai_compatible_llm_classifier_posts_to_configured_base_url() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text=(
                'data: {"choices":[{"delta":{"reasoning_content":"checking failure"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"{\\"l1\\":\\"INFRA\\","}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"\\"l2\\":\\"NETWORK\\"}"}}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    classifier = OpenAICompatibleLLMClassifier(
        provider_name="codex",
        base_url="https://api-vip.codex-for.me/v1",
        model="gpt-5-codex",
        api_key="test-key",
        reasoning_effort="high",
        default_l1_category="OTHERS",
        default_l2_subcategory="UNCLASSIFIED",
        allowed_classifications=(
            ("INFRA", "NETWORK"),
            ("OTHERS", "UNCLASSIFIED"),
        ),
        transport=httpx.MockTransport(handler),
    )

    result = classifier.classify(
        log_text="dial tcp 10.0.0.1:443: i/o timeout",
        build={
            "job_name": "ghpr_check2",
            "url": "https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/101/",
        },
    )

    assert result.l1_category == "INFRA"
    assert result.l2_subcategory == "NETWORK"
    assert result.source == "llm:codex"
    assert captured["url"] == "https://api-vip.codex-for.me/v1/chat/completions"
    assert captured["auth"] == "Bearer test-key"
    assert captured["body"] == {
        "model": "gpt-5-codex",
        "reasoning_effort": "high",
        "stream": True,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You classify Jenkins CI build failures into a fixed taxonomy. "
                    "Return JSON only with keys 'l1' and 'l2'. "
                    "If the evidence is weak, return OTHERS/UNCLASSIFIED. "
                    "Do not invent new categories."
                ),
            },
            {
                "role": "user",
                "content": (
                    "The deterministic rule engine already found no exact rule match.\n"
                    "Choose the best category from the allowed list below.\n\n"
                    "Allowed categories:\n"
                    "- INFRA/NETWORK\n"
                    "- OTHERS/UNCLASSIFIED\n\n"
                    f"{ERROR_CLASSIFICATION_GUIDANCE}\n\n"
                    "Build context:\n"
                    "- job_name: ghpr_check2\n"
                    "- url: https://prow.tidb.net/jenkins/job/pingcap/job/tidb/job/ghpr_check2/101/\n\n"
                    "Redacted console log tail:\n"
                    "dial tcp 10.0.0.1:443: i/o timeout\n"
                ),
            },
        ],
    }


def test_openai_compatible_llm_classifier_rejects_unknown_label() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text=(
                'data: {"choices":[{"delta":{"content":"{\\"l1\\":\\"NEW\\",\\"l2\\":\\"CATEGORY\\"}"}}]}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    classifier = OpenAICompatibleLLMClassifier(
        provider_name="codex",
        base_url="https://api-vip.codex-for.me/v1",
        model="gpt-5-codex",
        api_key="test-key",
        reasoning_effort=None,
        default_l1_category="OTHERS",
        default_l2_subcategory="UNCLASSIFIED",
        allowed_classifications=(("INFRA", "NETWORK"), ("OTHERS", "UNCLASSIFIED")),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ValueError, match="unsupported classification"):
        classifier.classify(log_text="unknown failure", build={})


def test_openai_compatible_llm_classifier_ignores_reasoning_only_chunks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text=(
                'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\n'
                'data: {"choices":[{"delta":{"reasoning_content":"still thinking"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"{\\"l1\\":\\"OTHERS\\",\\"l2\\":\\"UNCLASSIFIED\\"}"}}]}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    classifier = OpenAICompatibleLLMClassifier(
        provider_name="codex",
        base_url="https://api-vip.codex-for.me/v1",
        model="gpt-5-codex",
        api_key="test-key",
        reasoning_effort=None,
        default_l1_category="OTHERS",
        default_l2_subcategory="UNCLASSIFIED",
        allowed_classifications=(("INFRA", "NETWORK"), ("OTHERS", "UNCLASSIFIED")),
        transport=httpx.MockTransport(handler),
    )

    result = classifier.classify(log_text="unknown failure", build={})

    assert result.l1_category == "OTHERS"
    assert result.l2_subcategory == "UNCLASSIFIED"


def test_openai_compatible_llm_classifier_retries_on_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleep_calls: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                request=request,
                headers={"Retry-After": "7"},
                text="rate limited",
            )
        return httpx.Response(
            200,
            request=request,
            headers={"Content-Type": "text/event-stream"},
            text=(
                'data: {"choices":[{"delta":{"content":"{\\"l1\\":\\"OTHERS\\",\\"l2\\":\\"UNCLASSIFIED\\"}"}}]}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    monkeypatch.setattr(llm_classifier_module.time, "sleep", sleep_calls.append)

    classifier = OpenAICompatibleLLMClassifier(
        provider_name="codex",
        base_url="https://api-vip.codex-for.me/v1",
        model="gpt-5-codex",
        api_key="test-key",
        reasoning_effort=None,
        default_l1_category="OTHERS",
        default_l2_subcategory="UNCLASSIFIED",
        allowed_classifications=(("INFRA", "NETWORK"), ("OTHERS", "UNCLASSIFIED")),
        transport=httpx.MockTransport(handler),
    )

    result = classifier.classify(log_text="unknown failure", build={})

    assert result.l1_category == "OTHERS"
    assert result.l2_subcategory == "UNCLASSIFIED"
    assert attempts == 2
    assert sleep_calls == [7.0]


def test_iter_sse_data_payloads_preserves_significant_whitespace() -> None:
    response = httpx.Response(
        200,
        text=(
            ": keep-alive\n"
            "data:  first line  \n"
            "data: second line\n\n"
            "data: [DONE]\n\n"
        ),
    )

    assert list(_iter_sse_data_payloads(response)) == [" first line  \nsecond line", "[DONE]"]


def test_build_llm_classifier_requires_base_url_for_codex() -> None:
    with pytest.raises(ValueError, match="CI_DASHBOARD_LLM_BASE_URL"):
        build_llm_classifier(
            LLMSettings(
                provider="codex",
                model="gpt-5-codex",
                api_key="test-key",
                base_url=None,
            ),
            default_l1_category="OTHERS",
            default_l2_subcategory="UNCLASSIFIED",
            allowed_classifications=(("OTHERS", "UNCLASSIFIED"),),
        )


def test_build_llm_classifier_passes_reasoning_effort() -> None:
    classifier = build_llm_classifier(
        LLMSettings(
            provider="codex",
            model="gpt-5.4",
            api_key="test-key",
            base_url="https://api-vip.codex-for.me/v1",
            reasoning_effort="high",
        ),
        default_l1_category="OTHERS",
        default_l2_subcategory="UNCLASSIFIED",
        allowed_classifications=(("OTHERS", "UNCLASSIFIED"),),
    )

    assert isinstance(classifier, OpenAICompatibleLLMClassifier)
    assert classifier.model == "gpt-5.4"
    assert classifier.reasoning_effort == "high"
    assert classifier.timeout_seconds == 180


def test_build_llm_classifier_rejects_invalid_reasoning_effort() -> None:
    with pytest.raises(ValueError, match="CI_DASHBOARD_LLM_REASONING_EFFORT"):
        build_llm_classifier(
            LLMSettings(
                provider="codex",
                model="gpt-5.4",
                api_key="test-key",
                base_url="https://api-vip.codex-for.me/v1",
                reasoning_effort="extreme",
            ),
            default_l1_category="OTHERS",
            default_l2_subcategory="UNCLASSIFIED",
            allowed_classifications=(("OTHERS", "UNCLASSIFIED"),),
        )


def test_noop_llm_classifier_returns_default_classification() -> None:
    classifier = NoopLLMClassifier(
        default_l1_category="OTHERS",
        default_l2_subcategory="UNCLASSIFIED",
    )

    result = classifier.classify(log_text="ignored", build={"job_name": "job"})

    assert result.l1_category == "OTHERS"
    assert result.l2_subcategory == "UNCLASSIFIED"
    assert result.source == "llm:noop"


def test_llm_helper_functions_cover_validation_and_truncation() -> None:
    assert _truncate_log_text("short", max_chars=10) == "short"
    assert _truncate_log_text("abcdefghijklmnopqrstuvwxyz", max_chars=5) == "[TRUNCATED TO LAST 5 CHARS]\nvwxyz"
    assert _normalize_reasoning_effort(None) is None
    assert _normalize_reasoning_effort(" HIGH ") == "high"
    assert _parse_retry_after_seconds(None) is None
    assert _parse_retry_after_seconds("   ") is None
    assert _parse_retry_after_seconds("7.5") == 7.5
    assert _parse_retry_after_seconds("-3") == 0.0
    assert _parse_retry_after_seconds("not-a-number") is None
    assert _resolve_rate_limit_backoff_seconds(httpx.Response(429, headers={}), attempt=0) == 5.0
    assert _resolve_rate_limit_backoff_seconds(httpx.Response(429, headers={}), attempt=4) == 60.0
    assert _resolve_rate_limit_backoff_seconds(httpx.Response(429, headers={"Retry-After": "12"}), attempt=0) == 12.0
    assert _should_retry_rate_limit(httpx.Response(429), attempt=0) is True
    assert _should_retry_rate_limit(httpx.Response(429), attempt=4) is False
    assert _should_retry_rate_limit(httpx.Response(500), attempt=0) is False


def test_chat_payload_and_content_parsing_helpers() -> None:
    payload = _build_chat_payload(
        model="gpt-5.4",
        reasoning_effort=None,
        allowed_classifications=(("INFRA", "NETWORK"), ("OTHERS", "UNCLASSIFIED")),
        default_l1_category="OTHERS",
        default_l2_subcategory="UNCLASSIFIED",
        log_text="tail",
        build={},
    )
    assert payload["model"] == "gpt-5.4"
    assert "reasoning_effort" not in payload
    assert "<unknown>" in payload["messages"][1]["content"]
    assert ERROR_CLASSIFICATION_GUIDANCE in payload["messages"][1]["content"]

    assert _extract_stream_choice_content({"choices": [{"delta": {"content": "hello"}}]}) == ["hello"]
    assert _extract_stream_choice_content({"choices": [{"delta": {"content": [{"text": "line-1"}, {"text": "line-2"}]}}]}) == [
        "line-1\nline-2"
    ]
    assert _extract_stream_choice_content({"choices": [{"delta": {"content": None}}, "bad"]}) == []
    assert _extract_stream_choice_content({"choices": "bad"}) == []
    assert _coerce_message_content("text") == "text"
    with pytest.raises(ValueError, match="empty"):
        _coerce_message_content([])


def test_json_object_helpers_accept_fenced_json_and_reject_bad_payloads() -> None:
    assert _extract_json_object('```json\n{"l1":"INFRA","l2":"NETWORK"}\n```') == {
        "l1": "INFRA",
        "l2": "NETWORK",
    }
    assert _extract_json_object('prefix {"l1":"INFRA","l2":"NETWORK"} suffix') == {
        "l1": "INFRA",
        "l2": "NETWORK",
    }
    with pytest.raises(ValueError, match="did not contain a JSON object"):
        _extract_json_object("no json here")
    with pytest.raises(ValueError, match="did not contain a JSON object"):
        _extract_json_object("[1,2,3]")

    classification = _validate_classification(
        {"l1": " infra ", "l2": " network "},
        allowed_classifications=(("INFRA", "NETWORK"),),
        provider_name="codex",
    )
    assert classification.l1_category == "INFRA"
    assert classification.l2_subcategory == "NETWORK"
    with pytest.raises(ValueError, match="unsupported classification"):
        _validate_classification(
            {"l1": "APP", "l2": "BUG"},
            allowed_classifications=(("INFRA", "NETWORK"),),
            provider_name="codex",
        )


def test_collect_stream_content_rejects_empty_or_non_object_events() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        _collect_stream_content(
            httpx.Response(
                200,
                text='data: ["bad"]\n\n',
            )
        )

    with pytest.raises(ValueError, match="did not contain content"):
        _collect_stream_content(
            httpx.Response(
                200,
                text='data: {"choices":[{"delta":{"reasoning_content":"only-thoughts"}}]}\n\ndata: [DONE]\n\n',
            )
        )


def test_openai_compatible_llm_classifier_rate_limit_failures_surface_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep_calls: list[float] = []
    attempts = {"count": 0}

    def retry_forever(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(429, request=request, text="rate limited")

    classifier = OpenAICompatibleLLMClassifier(
        provider_name="codex",
        base_url="https://api-vip.codex-for.me/v1",
        model="gpt-5-codex",
        api_key="test-key",
        reasoning_effort=None,
        default_l1_category="OTHERS",
        default_l2_subcategory="UNCLASSIFIED",
        allowed_classifications=(("OTHERS", "UNCLASSIFIED"),),
        transport=httpx.MockTransport(retry_forever),
    )
    monkeypatch.setattr(llm_classifier_module.time, "sleep", sleep_calls.append)

    with pytest.raises(RuntimeError, match="retries exhausted after 5 attempts") as exc_info:
        classifier.classify(log_text="rate limited", build={})

    assert attempts["count"] == 5
    assert sleep_calls == [5.0, 10.0, 20.0, 40.0]
    assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)


def test_openai_compatible_llm_classifier_does_not_retry_non_429_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request, text="server error")

    classifier = OpenAICompatibleLLMClassifier(
        provider_name="codex",
        base_url="https://api-vip.codex-for.me/v1",
        model="gpt-5-codex",
        api_key="test-key",
        reasoning_effort=None,
        default_l1_category="OTHERS",
        default_l2_subcategory="UNCLASSIFIED",
        allowed_classifications=(("OTHERS", "UNCLASSIFIED"),),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(httpx.HTTPStatusError):
        classifier.classify(log_text="server error", build={})


def test_build_llm_classifier_supports_noop_aliases_and_rejects_unknown_provider() -> None:
    assert isinstance(
        build_llm_classifier(
            LLMSettings(provider=" none "),
            default_l1_category="OTHERS",
            default_l2_subcategory="UNCLASSIFIED",
            allowed_classifications=(("OTHERS", "UNCLASSIFIED"),),
        ),
        NoopLLMClassifier,
    )
    with pytest.raises(ValueError, match="unsupported CI_DASHBOARD_LLM_PROVIDER"):
        build_llm_classifier(
            LLMSettings(provider="gemini"),
            default_l1_category="OTHERS",
            default_l2_subcategory="UNCLASSIFIED",
            allowed_classifications=(("OTHERS", "UNCLASSIFIED"),),
        )
