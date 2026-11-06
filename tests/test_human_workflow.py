"""Regression tests for the human-facing workflow fixed in 0.2.0."""

import json

import httpx
import pytest

from jev_runtime import __version__
from jev_runtime.cli import main
from jev_runtime.contracts import Policy
from jev_runtime.onboarding import (
    REPAIR_PROMPT,
    DemoOnboarding,
    OnboardingContractError,
    OnboardingModel,
    extract_json_object,
    normalize_response,
)

SUBCOMMANDS = (
    "setup onboard inspect approve decide collect-file complete doctor capabilities status "
    "stop revoke run verify apply reconcile recover constitution git"
).split()


def test_version_flags(capsys):
    for flag in ("--version", "-V"):
        with pytest.raises(SystemExit) as done:
            main([flag])
        assert done.value.code == 0
        assert capsys.readouterr().out.strip() == "jev " + __version__ == "jev 0.2.0"


def test_every_subcommand_has_help(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    top = capsys.readouterr().out
    assert len(SUBCOMMANDS) == 19
    for name in SUBCOMMANDS:
        line = next(x for x in top.splitlines() if x.strip().startswith(name + " "))
        assert len(line.split()) > 1, name


def test_onboard_workspace_option_has_help(capsys):
    with pytest.raises(SystemExit):
        main(["onboard", "--help"])
    assert "Existing project directory" in capsys.readouterr().out


def test_doctor_is_human_readable_with_json_option(monkeypatch, capsys):
    result = {"available": True, "backend": "bubblewrap", "reason": ""}
    monkeypatch.setattr("jev_runtime.sandbox.Sandbox.check", lambda self: result)
    assert main(["doctor"]) == 0
    assert capsys.readouterr().out.strip() == "Worker isolation: available (bubblewrap)"
    assert main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == result


def test_status_empty_is_human_readable(tmp_path, monkeypatch, capsys):
    state = str(tmp_path / "ledger.sqlite")
    monkeypatch.setattr("builtins.input", lambda _: "/done")
    assert main(["--state", state, "--demo", "onboard", "--workspace", str(tmp_path)]) == 0
    capsys.readouterr()
    assert main(["--state", state, "--demo", "status"]) == 0
    assert "No worker sessions recorded yet" in capsys.readouterr().out
    assert main(["--state", state, "--demo", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


@pytest.mark.parametrize(
    "content",
    [
        '```json\n{"question": "Q?", "policy": null}\n```',
        'Sure! Here it is: {"question": "Q?", "policy": null} Hope that helps.',
        '{"Question": "Q?"}',
        '{"message": "Q?", "policy": {}}',
    ],
)
def test_common_model_variants_normalize(content, tmp_path):
    raw = extract_json_object(content)
    assert normalize_response(raw, "i", str(tmp_path)) == {"question": "Q?", "policy": None}


def test_policy_variants_normalize_without_adding_capabilities(tmp_path):
    raw = {
        "question": "Ready for review",
        "policy": {
            "actions": ["Read", "write", "write"],
            "required_evidence": "notes.txt exists",
            "checks": {"file": "notes.txt"},
            "judgment_threshold": 0.5,
            "max_seconds": "600",
            "extra": "dropped",
        },
    }
    out = normalize_response(raw, "intent", str(tmp_path))
    policy = Policy.parse(out["policy"])
    assert policy.actions == ("read", "write")
    assert policy.required_evidence == ("notes.txt exists",)
    assert policy.checks[0].path == "notes.txt"
    assert policy.judgment_threshold == 0.95
    assert policy.max_seconds == 600
    assert "execute" not in policy.actions


def test_conflicting_provenance_is_not_overwritten(tmp_path):
    raw = {"question": "q", "policy": {"workspace": "/elsewhere", "actions": []}}
    assert normalize_response(raw, "i", str(tmp_path))["policy"]["workspace"] == "/elsewhere"


def _model(replies, seen):
    replies = iter(replies)

    def handle(request):
        seen.append(json.loads(request.content))
        content = next(replies)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return OnboardingModel(
        {"base_url": "http://localhost:1/v1", "model": "m", "auth_required": False},
        httpx.MockTransport(handle),
        api_key="",
    )


def test_schema_repair_prompt_recovers_in_same_turn(tmp_path):
    seen = []
    model = _model(["I think you should...", '{"question": "What file?", "policy": null}'], seen)
    result = model.turn("Make a file", "i", str(tmp_path))
    assert result == {"question": "What file?", "policy": None}
    assert len(seen) == 2
    assert seen[1]["messages"][-1]["content"].startswith(REPAIR_PROMPT)


def test_repair_gives_up_with_contract_error(tmp_path):
    seen = []
    model = _model(["nope"] * 3, seen)
    with pytest.raises(OnboardingContractError):
        model.turn("Make a file", "i", str(tmp_path))
    assert len(seen) == 3


def test_demo_onboarding_proposes_a_runnable_scope(tmp_path):
    result = DemoOnboarding().turn([], "intent", str(tmp_path))
    policy = Policy.parse(result["policy"])
    assert {"read", "execute"} <= set(policy.actions)
    assert policy.shell_commands == (("sh",),)
    assert "/run --no-provider" in result["question"]


def test_brand_icons_are_real_files_with_multi_size_favicon():
    import struct
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    brand = root / "docs" / "brand" / "icons"
    assert brand.is_dir() and not brand.is_symlink()
    ico = (root / "assets" / "icons" / "favicon.ico").read_bytes()
    count = struct.unpack("<H", ico[4:6])[0]
    widths = sorted(ico[6 + 16 * i] for i in range(count))
    assert widths == [16, 32, 48]
    png = (root / "assets" / "icons" / "icon-128.png").read_bytes()
    assert struct.unpack(">II", png[16:24]) == (128, 128)
    for name in ("favicon.ico", "icon-128.png", "apple-touch-icon.png"):
        assert (brand / name).read_bytes() == (root / "assets" / "icons" / name).read_bytes()
