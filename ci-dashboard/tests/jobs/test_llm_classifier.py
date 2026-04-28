from __future__ import annotations

import json

import httpx
import pytest

from ci_dashboard.common.config import LLMSettings
from ci_dashboard.jobs.llm_classifier import (
    OpenAICompatibleLLMClassifier,
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
