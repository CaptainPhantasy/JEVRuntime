"""Connection choices cannot silently turn JEV access into generative inference."""

import json
from types import SimpleNamespace

import httpx2
import pytest

from jev_runtime import cli
from jev_runtime.config import generative_settings, load, setup
from jev_runtime.judgment import FakeJudge, judgment_client
from jev_runtime.onboarding import FakeOnboarding, OnboardingSession
from jev_runtime.runtime import Runtime
from jev_runtime.store import Store


def write_config(path, config):
    path.write_text(
        "\n".join(
            f"[{section}]\n"
            + "".join(f"{key} = {json.dumps(value)}\n" for key, value in settings.items())
            for section, settings in config.items()
        )
    )
    return path


@pytest.fixture
def connections():
    return {
        "judgment": {
            "provider": "openrouter",
            "base_url": "https://openrouter.ai",
            "model": "typesafe/jev-1.13",
            "api_key_env": "TEST_JEV",
        },
        "onboarding": {
            "provider": "openai-compatible",
            "base_url": "https://interviewer.invalid/v1",
            "model": "human-selected-interviewer",
            "api_key_env": "TEST_INTERVIEW",
        },
        "agent_provider": {
            "provider": "openai-compatible",
            "base_url": "https://worker.invalid/v1",
            "model": "human-selected-worker",
            "api_key_env": "TEST_WORKER",
        },
        "agent": {"command": ["worker"], "provider": "unconfigured"},
    }


@pytest.fixture
def active_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("Project")
    store = Store(tmp_path / "ledger.sqlite")
    runtime = Runtime(store, FakeJudge())
    intent = store.record_intent("Read the project and run the selected worker.")
    proposal = runtime.propose(
        {
            "intent_id": intent,
            "workspace": str(project),
            "actions": ["read", "execute"],
            "required_evidence": ["README exists"],
            "checks": [{"requirement": "README exists", "kind": "file", "path": "README.md"}],
        }
    )
    runtime.approve(proposal)
    yield runtime, store, proposal
    store.close()


def forbidden(*args, **kwargs):
    raise AssertionError("No credential, provider or connection initialization is allowed here")


def test_default_setup_defers_both_generative_connections(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    path = tmp_path / "providers.toml"
    setup(path)
    config = load(path)
    assert config["onboarding"] == {"provider": "unconfigured"}
    assert config["agent"]["provider"] == "unconfigured"
    assert config["judgment"]["provider"] == "openrouter"
    assert "api.openai.com" not in path.read_text()
    assert "gpt-" not in path.read_text()


def test_explicit_local_interviewer_does_not_opt_worker_in(tmp_path, monkeypatch):
    answers = iter(["yes", "http://localhost:8123/v1", "my-local-model", "", "", "", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    path = tmp_path / "providers.toml"
    setup(path)
    config = load(path)
    assert config["onboarding"]["model"] == "my-local-model"
    assert config["onboarding"]["auth_required"] is False
    assert config["agent"]["provider"] == "unconfigured"


@pytest.mark.parametrize("answers", [["yes", ""], ["yes", "https://models.invalid/v1", ""]])
def test_setup_requires_explicit_endpoint_and_model_without_overwriting(
    tmp_path, monkeypatch, answers
):
    path = tmp_path / "providers.toml"
    original = '[onboarding]\nprovider = "unconfigured"\n'
    path.write_text(original)
    responses = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    with pytest.raises(ValueError, match="explicitly"):
        setup(path)
    assert path.read_text() == original


@pytest.mark.parametrize("section", ["onboarding", "agent_provider"])
def test_shared_jev_credential_reference_is_rejected(tmp_path, connections, section):
    connections[section]["api_key_env"] = "TEST_JEV"
    path = write_config(tmp_path / "providers.toml", connections)
    config = load(path)
    if section == "agent_provider":
        config["agent"]["provider"] = "agent_provider"
    with pytest.raises(ValueError, match="JEV credential reference"):
        generative_settings(config, "onboarding" if section == "onboarding" else "worker")


def test_credential_cache_is_scoped_and_known_jev_value_cannot_generate(monkeypatch, connections):
    monkeypatch.setattr(cli, "_CREDENTIALS", {})
    monkeypatch.delenv("TEST_JEV", raising=False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    answers = iter(["synthetic-judgment-only", "synthetic-distinct-onboarding"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: next(answers))
    settings = connections["judgment"]
    assert cli.credential(settings, "JEV", purpose="judgment") == "synthetic-judgment-only"
    assert (
        cli.credential(settings, "Onboarding", purpose="onboarding")
        == "synthetic-distinct-onboarding"
    )
    monkeypatch.setenv("TEST_INTERVIEW", "synthetic-judgment-only")
    with pytest.raises(ValueError, match="JEV credential"):
        cli.model_credential(connections, connections["onboarding"], "Onboarding", "onboarding")
    cli._CREDENTIALS.clear()
    monkeypatch.setenv("TEST_JEV", "synthetic-judgment-only")
    with pytest.raises(ValueError, match="JEV credential"):
        cli.model_credential(connections, connections["onboarding"], "Onboarding", "onboarding")


def test_jev_only_configuration_uses_only_native_judgment(
    tmp_path, monkeypatch, connections, active_project
):
    runtime, store, proposal = active_project
    connections["onboarding"] = {"provider": "unconfigured"}
    # An invalid unused generative connection cannot block native JEV or be selected.
    connections["agent_provider"] = {
        "provider": "invalid",
        "base_url": "not-an-endpoint",
        "api_key_env": "TEST_JEV",
    }
    path = write_config(tmp_path / "providers.toml", connections)
    monkeypatch.setenv("TEST_JEV", "synthetic-jev-credential")
    monkeypatch.setattr(cli, "model_credential", forbidden)
    monkeypatch.setattr(cli, "OnboardingModel", forbidden)
    requests = []

    def handle(request):
        requests.append(request)
        assert str(request.url) == "https://openrouter.ai/api/alpha/decisions"
        assert request.headers["authorization"] == "Bearer synthetic-jev-credential"
        body = json.loads(request.content)
        assert body["model"] == "typesafe/jev-1.13"
        assert "messages" not in body
        return httpx2.Response(
            200,
            json={
                "model": "native-contract",
                "usage": {},
                "answers": {name: {"type": "noul", "noul": 0.99} for name in body["questions"]},
            },
        )

    monkeypatch.setattr(
        cli,
        "judgment_client",
        lambda settings, key: judgment_client(settings, key, httpx2.MockTransport(handle)),
    )
    assert (
        cli.main(
            [
                "--state",
                str(store.path),
                "--config",
                str(path),
                "decide",
                proposal,
                "read",
                "README.md",
            ]
        )
        == 0
    )
    assert len(requests) == 1


@pytest.mark.parametrize("operation", ["approve", "export", "complete"])
def test_local_policy_commands_need_no_connection(tmp_path, monkeypatch, active_project, operation):
    runtime, store, proposal = active_project
    monkeypatch.setattr(cli, "load", forbidden)
    monkeypatch.setattr(cli, "credential", forbidden)
    monkeypatch.setattr(cli, "judgment_client", forbidden)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    path = tmp_path / "unreadable-providers.toml"
    path.write_text("not valid TOML and must not be read")
    args = ["--state", str(store.path), "--config", str(path)]
    if operation == "export":
        args += ["constitution", "export", proposal, str(tmp_path / "export.json")]
    else:
        args += [operation, proposal]
    assert cli.main(args) == (1 if operation == "complete" else 0)


def test_deferred_interview_preserves_text_and_suspends_old_authority(
    tmp_path, monkeypatch, capsys
):
    store = Store(tmp_path / "ledger.sqlite")
    project = tmp_path / "project"
    project.mkdir()
    runtime = Runtime(store, FakeJudge())
    session = OnboardingSession.start(store, runtime, FakeOnboarding(), project)
    proposal = session.reply("Read this project.")["proposal_id"]
    runtime.approve(proposal)
    path = write_config(tmp_path / "providers.toml", {"onboarding": {"provider": "unconfigured"}})
    for name in ("credential", "judgment_client", "OnboardingModel"):
        monkeypatch.setattr(cli, name, forbidden)
    args = ["--state", str(store.path), "--config", str(path), "onboard", "--session", session.id]
    answers = iter(["/review", "/done"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert cli.main(args) == 0
    assert "Requested capabilities: read" in capsys.readouterr().out
    assert runtime.current(session.id) == proposal
    text = "  Preserve this exact revised request.\nDo not choose any paid model.  "
    answers = iter([text, "/retry", "/done"])
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    assert "Onboarding model is not connected" in output
    assert session.pending()
    assert runtime.current(session.id) is None
    assert [r["text"] for _, r in store.records("human_intent")] == ["Read this project.", text]
    assert len(store.records("onboarding_output")) == 1
    assert len(store.records("judgment")) == 1
    assert len(store.records("human_approval")) == 1
    store.close()


@pytest.mark.parametrize(
    "selector", [None, "unconfigured", "invalid", "none", "onboarding", "agent_provider"]
)
def test_worker_uses_exact_selected_connection(
    tmp_path, monkeypatch, connections, active_project, selector
):
    runtime, store, proposal = active_project
    if selector is None:
        del connections["agent"]["provider"]
    else:
        connections["agent"]["provider"] = selector
    captured = {}

    class Runner:
        def run(self, proposal_id, command, **kwargs):
            captured.update(kwargs)
            return "selected-run"

        def preview(self, run_id):
            return []

    calls = []
    monkeypatch.setattr(cli, "supervisor", lambda *_: Runner())
    monkeypatch.setattr(cli, "confirm_operation", lambda _: False)
    monkeypatch.setattr(
        cli,
        "model_credential",
        lambda config, settings, label, purpose: (
            calls.append(settings["api_key_env"]) or "synthetic-worker-key"
        ),
    )
    args = SimpleNamespace(
        demo=False,
        no_provider=False,
        interactive=False,
        state=str(store.path),
        config=str(tmp_path / "providers.toml"),
    )
    if selector in {None, "unconfigured", "invalid"}:
        with pytest.raises(ValueError):
            cli.start_worker(runtime, args, connections, proposal)
        assert not calls and not captured
    else:
        assert cli.start_worker(runtime, args, connections, proposal) == "selected-run"
        if selector == "none":
            assert captured["provider"] is None and not calls
        else:
            assert captured["provider"]["model"] == connections[selector]["model"]
            assert calls == [connections[selector]["api_key_env"]]
    args.no_provider = True
    calls.clear()
    assert cli.start_worker(runtime, args, connections, proposal) == "selected-run"
    assert captured["provider"] is None and not calls


def test_rejected_prompted_jev_key_does_not_poison_retry_cache(monkeypatch, connections):
    monkeypatch.setattr(cli, "_CREDENTIALS", {})
    monkeypatch.delenv("TEST_JEV", raising=False)
    monkeypatch.delenv("TEST_INTERVIEW", raising=False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    prompts = []
    answers = iter(
        ["synthetic-judgment-only", "synthetic-judgment-only", "synthetic-interviewer-only"]
    )

    def get_key(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(cli.getpass, "getpass", get_key)
    cli.credential(connections["judgment"], "JEV", purpose="judgment")
    with pytest.raises(ValueError, match="JEV credential"):
        cli.model_credential(connections, connections["onboarding"], "Onboarding", "onboarding")
    assert len(cli._CREDENTIALS) == 1
    assert (
        cli.model_credential(connections, connections["onboarding"], "Onboarding", "onboarding")
        == "synthetic-interviewer-only"
    )
    assert len(prompts) == 3


def test_unused_plaintext_key_is_still_rejected(tmp_path, connections):
    connections["agent_provider"]["api_key"] = "synthetic-key-must-not-be-saved"
    path = write_config(tmp_path / "providers.toml", connections)
    with pytest.raises(ValueError, match="never saved API keys"):
        load(path)


def test_saved_interview_with_missing_config_does_not_launch_setup(tmp_path, monkeypatch):
    store = Store(tmp_path / "ledger.sqlite")
    runtime = Runtime(store, FakeJudge())
    session = OnboardingSession.start(store, runtime, FakeOnboarding(), tmp_path)
    monkeypatch.setattr(cli, "setup", forbidden)
    monkeypatch.setattr(cli, "credential", forbidden)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "/done")
    assert (
        cli.main(
            [
                "--state",
                str(store.path),
                "--config",
                str(tmp_path / "missing.toml"),
                "onboard",
                "--session",
                session.id,
            ]
        )
        == 0
    )
    store.close()


def test_unused_malformed_judgment_reference_does_not_block_local_connection(tmp_path, connections):
    connections["judgment"]["api_key_env"] = 123
    connections["onboarding"]["base_url"] = "http://localhost:8123/v1"
    connections["onboarding"]["auth_required"] = False
    config = load(write_config(tmp_path / "providers.toml", connections))
    settings = generative_settings(config, "onboarding")
    assert cli.model_credential(config, settings, "Onboarding", "onboarding") == ""


def test_oversized_compiler_reply_remains_pending_and_can_be_corrected(tmp_path):
    class Compiler(FakeOnboarding):
        oversized = True

        def turn(self, conversation, intent_id, workspace):
            result = super().turn(conversation, intent_id, workspace)
            if self.oversized:
                result["policy"]["checks"][0]["contains"] = "x" * 100001
            return result

    store = Store(tmp_path / "history.sqlite")
    try:
        runtime = Runtime(store, FakeJudge())
        model = Compiler()
        session = OnboardingSession.start(store, runtime, model, tmp_path)
        words = "Preserve this exact request while the compiler corrects its response."
        with pytest.raises(ValueError, match="16000-byte contract limit"):
            session.reply(words)
        assert session.pending()
        assert not session.outputs()
        assert not store.records("policy_proposal")
        assert not store.records("human_approval")
        assert store.records("human_intent")[0][1]["text"] == words
        model.oversized = False
        assert session.retry()["proposal_id"] is not None
        assert not session.pending()
        assert len(store.records("human_intent")) == 1
        assert not store.records("human_approval")
    finally:
        store.close()
