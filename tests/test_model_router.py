"""Unit tests for model_router.

Semantics after the v3.3.8 mode merge: DeepSeek serves a single chat model and
normalises ``model_type`` to "default". This proxy serves a single model too —
``/v1/models`` lists one name, and reasoning is configured with ``THINKING``
rather than chosen by picking a different model name. Route shorthands are
*capability presets* (thinking on/off); legacy names are aliases.
"""
import json

from model_router import (
    DEFAULT_MODEL_TYPE,
    LEGACY_MODEL_ALIASES,
    ModelRouter,
    canonical_model_name,
)


def test_empty_routes():
    r = ModelRouter("{}")
    assert r.models == []
    d = r.route_for("anything")
    assert d.matched_model is None
    assert d.model_type == "default"


def test_advertised_models_are_the_single_configured_name():
    # /v1/models must list exactly one model; the server injects MODEL_NAME.
    r = ModelRouter("", models=["deepseek-chat"])
    assert r.models == ["deepseek-chat"]


def test_legacy_names_are_accepted_but_do_not_change_behaviour():
    # A client asking for deepseek-reasoner still gets served — with no
    # capability override, so THINKING decides, exactly like deepseek-chat.
    r = ModelRouter("", models=["deepseek-chat"])
    for name in ("deepseek-chat", "deepseek-reasoner"):
        d = r.route_for(name)
        assert d.thinking is None and d.search is None
        assert d.model_type == DEFAULT_MODEL_TYPE
    assert set(LEGACY_MODEL_ALIASES) >= {"deepseek-reasoner"}
    assert canonical_model_name("deepseek-reasoner") == "deepseek-chat"


def test_legacy_names_honour_an_explicit_route_on_the_canonical_name():
    r = ModelRouter(json.dumps({"deepseek-chat": "thinking"}), models=["an-alias"])
    assert r.route_for("deepseek-reasoner").thinking is True


def test_configured_routes_take_precedence_over_fallback_names():
    r = ModelRouter(json.dumps({"foo": "default"}), models=["deepseek-chat"])
    assert r.models == ["foo"]


def test_string_shorthand_is_a_thinking_preset():
    r = ModelRouter(json.dumps({"deepseek-chat": "default", "deepseek-reasoner": "thinking"}))
    d = r.route_for("deepseek-reasoner")
    assert d.matched_model == "deepseek-reasoner"
    assert d.thinking is True
    # model_type is a protocol field now — every route reports "default".
    assert d.model_type == DEFAULT_MODEL_TYPE
    d = r.route_for("deepseek-chat")
    assert d.thinking is False
    assert d.model_type == DEFAULT_MODEL_TYPE


def test_legacy_expert_and_quick_shorthands_still_work():
    # Pre-3.3.7 .env files used "expert"/"quick"; they must keep working and now
    # mean thinking on/off (there is no separate expert model any more).
    r = ModelRouter(json.dumps({"a": "expert", "b": "quick"}))
    assert r.route_for("a").thinking is True
    assert r.route_for("b").thinking is False


def test_dict_route_with_thinking_and_search():
    raw = json.dumps({
        "deepseek-reasoner": {
            "thinking": "enabled",
            "search": "disabled",
        }
    })
    r = ModelRouter(raw)
    d = r.route_for("deepseek-reasoner")
    assert d.thinking is True
    assert d.search is False
    assert d.model_type == DEFAULT_MODEL_TYPE


def test_dict_route_may_override_model_type_protocol_field():
    # Kept so a future upstream re-enable (e.g. vision) is a config change only.
    raw = json.dumps({"vision-probe": {"model_type": "expert", "thinking": "disabled"}})
    r = ModelRouter(raw)
    d = r.route_for("vision-probe")
    assert d.model_type == "expert"
    assert d.thinking is False


def test_unknown_model_returns_default_decision():
    r = ModelRouter(json.dumps({"foo": "expert"}))
    d = r.route_for("bar")
    assert d.matched_model is None
    assert d.model_type == "default"
    assert d.thinking is None


def test_invalid_json_is_ignored():
    r = ModelRouter("not-json")
    assert r.models == []


def test_invalid_route_value_ignored():
    raw = json.dumps({"foo": "unknown-mode", "bar": "default"})
    r = ModelRouter(raw)
    assert r.models == ["bar"]


def test_optional_fields_default_to_none():
    r = ModelRouter(json.dumps({"foo": "expert"}))
    d = r.route_for("foo")
    assert d.search is None


def test_shorthand_is_case_insensitive():
    r = ModelRouter(json.dumps({"a": "Thinking", "b": " DEFAULT "}))
    assert r.route_for("a").thinking is True
    assert r.route_for("b").thinking is False
