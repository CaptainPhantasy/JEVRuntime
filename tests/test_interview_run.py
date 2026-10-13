"""Interview launch integration; model doubles do not establish live model readiness."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from jev_runtime.cli import interview
from jev_runtime.judgment import FakeJudge
from jev_runtime.onboarding import OnboardingSession
from jev_runtime.runtime import Runtime
from jev_runtime.sandbox import Sandbox
from jev_runtime.store import Store


class FixtureCompiler:
    def turn(self, conversation, intent_id, workspace):
        return {
            "question": "Review this synthetic verification scope.",
            "policy": {
                "intent_id": intent_id,
                "workspace": workspace,
                "actions": ["read", "write", "execute"],
                "shell_commands": [["/usr/bin/python3"]],
                "required_evidence": ["Exact report content checked independently"],
                "checks": [
                    {
                        "requirement": "Exact report content checked independently",
                        "kind": "command",
                        "argv": [
                            "/usr/bin/python3",
                            "-c",
                            "from pathlib import Path; "
                            "assert Path('report.txt').read_text() == 'verified locally\\n'; "
                            "print('independent check passed')",
                        ],
                        "expected_exit": 0,
                        "contains": "independent check passed",
                    }
                ],
            },
        }


@pytest.fixture
def saved_interview(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = Store(tmp_path / "history.sqlite")
    runtime = Runtime(store, FakeJudge())
    model = FixtureCompiler()
    session = OnboardingSession.start(store, runtime, model, project)
    proposal = session.reply("Synthetic fixture: write report.txt and check its exact content.")[
        "proposal_id"
    ]
    runtime.approve(proposal)
    args = SimpleNamespace(
        session=session.id,
        workspace=str(project),
        explicit_workspace=True,
        new=False,
        demo=False,
        state=str(store.path),
        config=str(tmp_path / "providers.toml"),
        run_dir=str(tmp_path / "workers"),
    )
    yield store, runtime, model, args, proposal
    store.close()


@pytest.mark.parametrize(
    ("text", "command", "interactive"),
    [
        ("/run --no-provider -- /usr/bin/python3 -V", ["/usr/bin/python3", "-V"], False),
        ("/run --interactive --no-provider", ["configured-worker", "argument"], True),
        (
            '/run --no-provider worker --interactive "two words" --no-provider',
            ["worker", "--interactive", "two words", "--no-provider"],
            False,
        ),
        ("/run", ["configured-worker", "argument"], False),
    ],
)
def test_interview_launch_options_and_worker_arguments(
    saved_interview, monkeypatch, text, command, interactive
):
    store, runtime, model, args, proposal = saved_interview
    original = vars(args).copy()
    captured = []

    class Runner:
        def run(self, selected, argv, **kwargs):
            captured.append((selected, argv, kwargs))
            return "fixture-run"

        def preview(self, run_id):
            return []

    def forbidden(*args, **kwargs):
        pytest.fail("Provider-free launch must not request model credentials")

    monkeypatch.setattr("jev_runtime.cli.model_credential", forbidden)
    monkeypatch.setattr("jev_runtime.cli.supervisor", lambda *_: Runner())
    monkeypatch.setattr("jev_runtime.cli.confirm_operation", lambda _: False)
    responses = iter([text, "/review", "/done"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    config = {"agent": {"provider": "none", "command": ["configured-worker", "argument"]}}
    if "--no-provider" in text:
        config["agent"]["provider"] = "unconfigured"
    assert interview(store, runtime, model, args, config) == 0
    assert captured == [
        (proposal, command, {"provider": None, "interactive": interactive, "tool_roots": ()})
    ]
    assert vars(args) == original
    assert len(store.records("onboarding_session")) == 1
    assert len(store.records("human_intent")) == 1


@pytest.mark.parametrize("bad_input", ["/run --unknown", '/run "unterminated', "/run --help"])
def test_invalid_launch_keeps_interview_usable(saved_interview, monkeypatch, capsys, bad_input):
    store, runtime, model, args, _ = saved_interview
    monkeypatch.setattr("jev_runtime.cli.start_worker", lambda *a: pytest.fail("Invalid launch"))
    responses = iter([bad_input, "/review", "/done"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    assert interview(store, runtime, model, args, {}) == 0
    output = capsys.readouterr().out
    assert "Cannot start worker" in output or "No closing quotation" in output
    assert "Conversation saved" in output
    assert len(store.records("human_intent")) == 1


def test_interview_real_worker_verification_publication_and_resume(
    saved_interview, monkeypatch, capsys
):
    availability = Sandbox().check()
    if not availability["available"]:
        pytest.skip(availability["reason"])
    store, runtime, model, args, proposal = saved_interview
    report = Path(args.workspace) / "report.txt"
    assert not runtime.complete(proposal)

    def confirm(message):
        # The host project must remain untouched before explicit publication review.
        if "apply this reviewed result" in message:
            assert not report.exists()
        return True

    def forbidden(*args, **kwargs):
        pytest.fail("This local worker must not access model credentials")

    monkeypatch.setattr("jev_runtime.cli.model_credential", forbidden)
    monkeypatch.setattr("jev_runtime.cli.confirm_operation", confirm)
    responses = iter(
        [
            '/run --no-provider -- /usr/bin/python3 -c "from pathlib import Path; '
            "Path('report.txt').write_text('verified locally\\n')\"",
            "/review",
            "/done",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    assert interview(store, runtime, model, args, {"agent": {"provider": "unconfigured"}}) == 0
    assert report.read_text() == "verified locally\n"
    assert runtime.complete(proposal)
    assert len(store.records("onboarding_session")) == 1
    assert len(store.records("runtime_session")) == 1
    assert len(store.records("run_verification")) == 1
    assert len(store.records("publication")) == 1
    assert "Applied 1 reviewed file changes" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda _: "/done")
    assert interview(store, runtime, model, args, {}) == 0
    assert len(store.records("onboarding_session")) == 1
    report.write_text("changed after publication")
    assert not runtime.complete(proposal)
