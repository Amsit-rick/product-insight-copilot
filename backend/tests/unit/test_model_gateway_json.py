"""真实模型 JSON 输出的保守解析与健康计数。"""

from __future__ import annotations

from app.services.model_gateway import ModelGateway, _parse_json_object


def test_parse_plain_json():
    assert _parse_json_object('{"a": 1}') == {"a": 1}


def test_parse_json_in_markdown_fence():
    assert _parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_with_surrounding_prose_and_nested_braces():
    text = '先说明一下：{"a": {"b": 2}, "c": [1, 2]} 以上是结果。'
    assert _parse_json_object(text) == {"a": {"b": 2}, "c": [1, 2]}


def test_parse_json_with_trailing_comma_is_repaired():
    assert _parse_json_object('{"a": 1, "b": [2, 3,],}') == {"a": 1, "b": [2, 3]}


def test_parse_rejects_non_object_or_truncated_json():
    assert _parse_json_object("[1, 2, 3]") is None
    assert _parse_json_object('{"a": 1') is None
    assert _parse_json_object("完全不是 JSON") is None


def test_gateway_stats_track_fallback_without_model():
    settings = ModelGateway().settings.model_copy(
        update={"llm_provider": "stub", "llm_api_key": ""}
    )
    gateway = ModelGateway(settings)

    data, usage = gateway.chat_json("system", "user", fallback=lambda: {"fallback": True})

    assert data == {"fallback": True}
    assert usage.stub is True
    stats = gateway.stats()
    assert stats["chat_json_calls"] == 1
    assert stats["fallbacks"] == 1
    assert stats["chat_calls"] == 0


def test_gateway_reset_stats():
    gateway = ModelGateway()
    gateway._bump_stat("chat_json_calls")
    assert gateway.stats()["chat_json_calls"] == 1
    gateway.reset_stats()
    assert gateway.stats()["chat_json_calls"] == 0
