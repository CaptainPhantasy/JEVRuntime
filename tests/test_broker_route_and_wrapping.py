"""Optional judgment broker route, plain JEV-unavailable guidance and text wrapping."""

import json

import httpx2
import pytest

from jev_runtime import cli
from jev_runtime.config import broker_route
from jev_runtime.judgment import TypeSafeJudge, judgment_client

OPENROUTER = {
    "provider": "openrouter",
    "base_url": "https://openrouter.ai",
    "model": "typesafe/jev-1.13",
    "api_key_env": "OPENROUTER_API_KEY",
}


def test_no_broker_by_default():
    assert broker_route(OPENROUTER, {}) is None


def test_broker_route_needs_token_and_safe_endpoint():
    with pytest.raises(ValueError, match="JEV_BROKER_TOKEN"):
        broker_route(OPENROUTER, {"JEV_BROKER_URL": "http://127.0.0.1:9000/proxy/x"})
    with pytest.raises(ValueError, match="HTTPS or a loopback"):
        broker_route(
            OPENROUTER,
            {"JEV_BROKER_URL": "http://broker.example/proxy", "JEV_BROKER_TOKEN": "t"},
        )
    with pytest.raises(ValueError, match="credentials"):
        broker_route(
            OPENROUTER,
            {"JEV_BROKER_URL": "https://u:p@broker.example/proxy", "JEV_BROKER_TOKEN": "t"},
        )
    with pytest.raises(ValueError, match="openrouter"):
        broker_route(
            {**OPENROUTER, "provider": "typesafe"},
            {"JEV_BROKER_URL": "https://broker.example/p", "JEV_BROKER_TOKEN": "t"},
        )
    assert broker_route(
        OPENROUTER,
        {"JEV_BROKER_URL": "https://broker.example/proxy/openrouter/", "JEV_BROKER_TOKEN": "t"},
    ) == ("https://broker.example/proxy/openrouter", "t")


def test_broker_url_is_used_as_is_with_lease_bearer():
    seen = []

    def handle(request):
        seen.append(request)
        body = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "id": "broker-contract",
                "model": "typesafe/jev-1.13",
                "usage": {},
                "answers": {name: {"type": "noul", "noul": 0.99} for name in body["questions"]},
            },
        )

    with judgment_client(
        OPENROUTER,
        "lease-token-for-test",
        httpx2.MockTransport(handle),
        decisions_url="http://127.0.0.1:9000/proxy/jev-decisions",
    ) as client:
        assert TypeSafeJudge(client).judge("action", {"action": "read"}).passes()
    assert str(seen[0].url) == "http://127.0.0.1:9000/proxy/jev-decisions"
    assert seen[0].headers["authorization"] == "Bearer lease-token-for-test"


def test_typesafe_provider_rejects_broker_url():
    with pytest.raises(ValueError, match="openrouter"):
        judgment_client(
            {**OPENROUTER, "provider": "typesafe", "base_url": "https://api.typesafe.ai"},
            "k",
            decisions_url="https://broker.example/p",
        )


def test_wrap_text_keeps_width_indent_and_json():
    line = "  • " + " ".join(["requirement"] * 12)
    wrapped = cli.wrap_text(line, 60).split("\n")
    assert len(wrapped) > 1
    assert all(len(part) <= 60 for part in wrapped)
    assert wrapped[0].startswith("  • ") and wrapped[1].startswith("    ")
    payload = json.dumps({"key": "x" * 120})
    assert cli.wrap_text(payload, 60) == payload
    assert cli.wrap_text("short\n\nlines", 60) == "short\n\nlines"


def test_say_does_not_wrap_pipes(capsys):
    text = "word " * 40
    cli.say(text)
    assert capsys.readouterr().out == text + "\n"


class _Store:
    def __init__(self, proposal):
        self.proposal = proposal

    def get(self, identifier, kind):
        return self.proposal


class _Runtime:
    def __init__(self, proposal):
        self.store = _Store(proposal)


def test_review_explains_unavailable_judgment_without_class_name(capsys):
    proposal = {
        "policy": {
            "workspace": "/tmp/project",
            "actions": ["read", "write", "execute"],
            "required_evidence": ["hello.txt contains hello world"],
        },
        "semantic_pass": False,
        "semantic_result": {"model": "unavailable", "error": "ValueError"},
    }
    cli.review(_Runtime(proposal), "p1")
    out = capsys.readouterr().out
    assert "ValueError" not in out
    assert "cannot be approved yet" in out
    assert "OPENROUTER_API_KEY" in out and "jev setup" in out and "jev --demo" in out


def test_onboarding_prompt_keeps_named_protected_paths():
    from jev_runtime.onboarding import SYSTEM

    assert "must appear in protected_paths exactly as named" in SYSTEM
    # The isolation sentence is no longer split across the launch paragraph.
    assert "isolated private copy;\nthey cannot access the host home" in SYSTEM


def test_judgment_context_states_protected_path_enforcement():
    from jev_runtime.judgment import RUNTIME_INVARIANTS

    assert "rejected at publication" in RUNTIME_INVARIANTS["paths"]
