import json
from types import SimpleNamespace

from jev_runtime.cli import main, start_worker
from jev_runtime.contracts import digest
from jev_runtime.judgment import FakeJudge
from jev_runtime.runtime import Runtime
from jev_runtime.store import Store


def test_offline_cli_onboarding_does_not_activate(tmp_path, monkeypatch, capsys):
    answers = iter(["Read this project and produce a report.", "/done"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    state = str(tmp_path / "ledger.sqlite")
    assert main(["--state", state, "--demo", "onboard", "--workspace", str(tmp_path)]) == 0
    assert not (tmp_path / "ledger.sqlite").exists()
    assert (tmp_path / "ledger.sqlite.demo").exists()
    capsys.readouterr()
    assert main(["--state", state, "--demo", "inspect", "--kind", "human_approval"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_provider_failure_retains_human_text(tmp_path, monkeypatch):
    from jev_runtime.store import Store

    answers = iter(["Exact human requirements", "/done"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    def failed(*args):
        raise TimeoutError("provider unavailable")

    monkeypatch.setattr("jev_runtime.onboarding.FakeOnboarding.turn", failed)
    state = str(tmp_path / "ledger.sqlite")
    assert main(["--state", state, "--demo", "onboard"]) == 0
    store = Store(state + ".demo")
    assert store.records("human_intent")[0][1]["text"] == "Exact human requirements"
    assert not store.records("human_approval")
    store.close()


def test_start_worker_receives_approved_source_context_and_typed_checks(tmp_path, monkeypatch):
    store = Store(tmp_path / "ledger.sqlite")
    runtime = Runtime(store, FakeJudge())
    human = "Build the requested app.\nPreserve the original requirements and private scope."
    human_id = store.record_intent(human)
    approved_note = (
        "Operator observation: use the existing app module and independent Python check."
    )
    record = {
        "session_id": "brief-session",
        "text": approved_note,
        "sha256": digest(approved_note),
        "authoritative": False,
    }
    context_id = store.append("operator_context", record)
    source_id = store.append(
        "intent_bundle",
        {
            "utterance_ids": [human_id],
            "context": [{"role": "user", "content": human}],
            "operator_context": [{"id": context_id, **record}],
        },
    )
    proposal = runtime.propose(
        {
            "intent_id": source_id,
            "workspace": str(tmp_path),
            "actions": ["read", "write", "execute"],
            "protected_paths": ["private"],
            "shell_commands": [["worker-agent", "run"]],
            "required_evidence": ["Independent Python check passes"],
            "checks": [
                {
                    "requirement": "Independent Python check passes",
                    "kind": "command",
                    "argv": ["python3", "-c", "print('verified')"],
                    "expected_exit": 0,
                    "contains": "verified",
                }
            ],
            "max_seconds": 123,
            "max_failures": 2,
            "behavior_interval": 17,
        }
    )
    runtime.approve(proposal)
    later_note = "A later note outside the approved source snapshot must not enter this launch."
    store.append("operator_context", {**record, "text": later_note, "sha256": digest(later_note)})
    captured = {}

    class Runner:
        def run(self, proposal_id, argv, **kwargs):
            captured.update(proposal=proposal_id, argv=argv, **kwargs)
            return "captured-run"

        def preview(self, run_id):
            assert run_id == "captured-run"
            return []

    monkeypatch.setattr("jev_runtime.cli.supervisor", lambda *_: Runner())
    monkeypatch.setattr("jev_runtime.cli.confirm_operation", lambda _: False)
    args = SimpleNamespace(
        demo=True,
        no_provider=True,
        interactive=False,
        state=str(store.path),
        config=str(tmp_path / "providers.toml"),
    )
    config = {"agent": {"command": ["worker-agent", "run", "{task}"]}}
    assert start_worker(runtime, args, config, proposal) == "captured-run"
    assert captured["argv"][:2] == ["worker-agent", "run"]
    assert captured["provider"] is None
    brief = captured["argv"][2]
    human_section = brief.split("## Human requirements\n\n", 1)[1].split("##", 1)[0]
    assert human_section == human + "\n\n"
    assert approved_note in brief and context_id in brief
    assert later_note not in brief
    assert "not human authority" in brief and "cannot add capabilities" in brief
    assert "current working directory is the isolated private project copy" in brief
    assert "host publication target" in brief
    approved_policy, _ = runtime._active(proposal)
    brief_policy = json.loads(brief.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert brief_policy == approved_policy.as_dict()
    assert brief_policy["checks"][0]["contains"] == "verified"
    assert brief_policy["max_seconds"] == 123
    assert store.get(human_id, "human_intent")["text"] == human
    store.close()


def test_capabilities_needs_no_configuration_credentials_or_ledger(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Capability inspection must not load config, credentials, a ledger or a model"
        )

    monkeypatch.setattr("jev_runtime.cli.load", forbidden)
    monkeypatch.setattr("jev_runtime.cli.credential", forbidden)
    monkeypatch.setattr("jev_runtime.cli.Store", forbidden)
    monkeypatch.setattr("jev_runtime.sandbox.Sandbox.check", forbidden)
    assert main(["--state", str(tmp_path / "ledger.sqlite"), "capabilities"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["grants_authority"] is False
    assert "deploy" not in report["supported_host_actions"]
    assert "deployment" in report["unsupported_controls"]
    assert "per_descendant_command_allowlist" in report["unsupported_controls"]
    assert report["host_probe"] == {"checked": False, "available": None}
    assert not (tmp_path / "ledger.sqlite").exists()


def test_capabilities_probe_is_not_overall_readiness(monkeypatch, capsys):
    monkeypatch.setattr(
        "jev_runtime.sandbox.Sandbox.check",
        lambda self: {"available": False, "backend": "bubblewrap", "reason": "disabled for test"},
    )
    assert main(["capabilities", "--probe"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["host_probe"]["checked"] is True
    assert report["host_probe"]["available"] is False
    assert "filesystem" in report["implemented_controls"]
    assert "no harness or model was tested" in report["host_probe"]["scope"]
