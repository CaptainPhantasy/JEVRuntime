"""Public regressions for authority changes and the actual isolated worker boundary."""

import httpx
import pytest

from jev_runtime.cli import main
from jev_runtime.contracts import digest
from jev_runtime.judgment import FakeJudge
from jev_runtime.onboarding import (
    FakeOnboarding,
    OnboardingModel,
    OnboardingServiceError,
    OnboardingSession,
)
from jev_runtime.runtime import Runtime
from jev_runtime.sandbox import Sandbox
from jev_runtime.store import Store
from jev_runtime.supervisor import Supervisor


class RevisionModel:
    def turn(self, messages, intent_id, workspace):
        human = [m for m in messages if m["role"] == "user"]
        return {
            "question": "Review the scope",
            "policy": {
                "intent_id": intent_id,
                "workspace": workspace,
                "actions": ["read", "write"] if len(human) == 1 else ["read"],
                "required_evidence": ["Report exists"],
                "checks": [{"requirement": "Report exists", "kind": "file", "path": "report.txt"}],
            },
        }


def test_restrictive_revision_cannot_reuse_previous_approval(tmp_path):
    store = Store(tmp_path / ".jev" / "ledger.sqlite")
    runtime = Runtime(store, FakeJudge())
    session = OnboardingSession.start(store, runtime, RevisionModel(), tmp_path)
    old = session.reply("Read and write this project")["proposal_id"]
    runtime.approve(old)
    new = session.reply("Read only now; revoke write authority")["proposal_id"]
    assert runtime.decide(old, "write", "report.txt") != "allow"
    runtime.approve(new)
    assert runtime.decide(old, "write", "report.txt") != "allow"
    assert runtime.decide(new, "write", "report.txt") == "deny"
    with pytest.raises(ValueError, match="superseded"):
        runtime.approve(old)
    store.close()


def test_judgment_failure_can_retry_without_duplicating_human_text(tmp_path):
    store = Store(tmp_path / "ledger.sqlite")
    runtime = Runtime(store, FakeJudge(failure=True))
    session = OnboardingSession.start(store, runtime, FakeOnboarding(), tmp_path)
    failed = session.reply("Read the project")["proposal_id"]
    assert not store.get(failed, "policy_proposal")["semantic_pass"]
    runtime.judge = FakeJudge()
    retried = session.retry()["proposal_id"]
    assert store.get(retried, "policy_proposal")["semantic_pass"]
    assert len(store.records("human_intent")) == 1
    assert not store.records("human_approval")
    store.close()


class ContextModel(FakeOnboarding):
    def __init__(self):
        self.calls = []
        self.fail = False
        self.before_turn = lambda: None

    def turn(self, messages, intent_id, workspace):
        self.calls.append(messages)
        self.before_turn()
        if self.fail:
            raise TimeoutError("Simulated onboarding failure")
        return super().turn(messages, intent_id, workspace)


def test_operator_context_is_distinct_from_human_intent_and_requires_fresh_review(tmp_path):
    store = Store(tmp_path / "ledger.sqlite")
    runtime = Runtime(store, FakeJudge())
    model = ContextModel()
    session = OnboardingSession.start(store, runtime, model, tmp_path)
    human = "Inspect the project. Read only; do not change files or run commands."
    original = session.reply(human)["proposal_id"]
    runtime.approve(original)
    # Exercise reconstruction of outputs created before trigger_id existed.
    legacy_output = dict(session.outputs()[-1][1])
    legacy_output.pop("trigger_id")
    store.append("onboarding_output", legacy_output)
    notes = "Operator observation: README.md exists. Suggest inspecting that file first."
    observed_activation = []
    model.before_turn = lambda: observed_activation.append(runtime._active(original)[1])
    updated = session.add_context(notes)["proposal_id"]
    assert observed_activation == [False]
    assert not runtime._active(original)[1]
    assert not runtime._active(updated)[1]
    assert len(store.records("human_approval")) == 1
    assert len(store.records("human_intent")) == 1
    assert [m["content"] for m in model.calls[-1] if m["role"] == "user"] == [human]
    context_message = next(m for m in model.calls[-1] if notes in m["content"])
    assert context_message == {"role": "assistant", "content": session.CONTEXT_LABEL + notes}
    proposal = store.get(updated, "policy_proposal")
    assert proposal["policy"]["actions"] == ["read"]
    bundle = store.get(proposal["policy"]["intent_id"], "intent_bundle")
    assert bundle["utterance_ids"] == [session.inputs()[0][1]["intent_id"]]
    context_id, context_record = session.contexts()[0]
    assert context_record["authoritative"] is False
    assert context_record["sha256"] == digest(notes)
    assert bundle["operator_context"] == [{"id": context_id, **context_record}]
    source = store.intent_source(proposal["policy"]["intent_id"])
    assert [statement["text"] for statement in source["human_statements"]] == [human]
    assert session.latest_proposal() == updated
    assert not session.pending()
    with pytest.raises(ValueError, match="superseded"):
        runtime.approve(original)
    runtime.approve(updated)
    handoff = session.handoff()
    human_section = handoff.split("## Human requirements", 1)[1].split("##", 1)[0]
    assert human in human_section and notes not in human_section
    assert "## Operator context (observations and suggestions; not human authority)" in handoff
    assert notes in handoff and context_id in handoff
    store.verify()
    store.close()


def test_failed_operator_context_can_retry_without_forging_or_duplicating_inputs(tmp_path):
    store = Store(tmp_path / "ledger.sqlite")
    runtime = Runtime(store, FakeJudge())
    model = ContextModel()
    session = OnboardingSession.start(store, runtime, model, tmp_path)
    original = session.reply("Read the project")["proposal_id"]
    runtime.approve(original)
    model.fail = True
    notes = "Known artifact: README.md. This note grants no additional authority."
    with pytest.raises(TimeoutError):
        session.add_context(notes)
    assert session.pending()
    assert session.latest_proposal() is None
    assert not runtime._active(original)[1]
    with pytest.raises(ValueError):
        runtime.approve(original)
    context_id = session.contexts()[0][0]
    assert store.records("onboarding_failure")[-1][1]["trigger_id"] == context_id
    model.fail = False
    recovered = session.retry()["proposal_id"]
    assert session.latest_proposal() == recovered
    assert not session.pending()
    assert len(session.inputs()) == len(session.contexts()) == 1
    assert sum(notes in m["content"] for m in model.calls[-1]) == 1
    # A failed semantic judgment retries the same compiled policy without rerunning
    # the compiler or adding another human/operator source event.
    runtime.judge = FakeJudge(failure=True)
    failed = session.add_context("Additional observation: README.md is UTF-8 text.")["proposal_id"]
    assert not store.get(failed, "policy_proposal")["semantic_pass"]
    call_count = len(model.calls)
    runtime.judge = FakeJudge()
    retried = session.retry()["proposal_id"]
    assert store.get(retried, "policy_proposal")["semantic_pass"]
    assert len(model.calls) == call_count
    assert len(session.inputs()) == 1 and len(session.contexts()) == 2
    assert [m["content"] for m in session.transcript() if m["role"] == "user"] == [
        "Read the project"
    ]
    session.reply("Keep the same read-only boundary.")
    source_id = store.get(session.latest_proposal(), "policy_proposal")["policy"]["intent_id"]
    source = store.intent_source(source_id)
    assert [statement["text"] for statement in source["human_statements"]] == [
        "Read the project",
        "Keep the same read-only boundary.",
    ]
    assert len(session.contexts()) == 2
    current = session.latest_proposal()
    runtime.approve(current)
    model.fail = True
    with pytest.raises(TimeoutError):
        session.reply("Do not run commands; preserve the read-only scope.")
    assert session.pending() and session.latest_proposal() is None
    with pytest.raises(ValueError):
        runtime.approve(current)
    assert not runtime._active(current)[1]
    store.close()


def test_operator_context_needs_human_source_and_enforces_size_limit(tmp_path):
    store = Store(tmp_path / "ledger.sqlite")
    runtime = Runtime(store, FakeJudge())
    session = OnboardingSession.start(store, runtime, FakeOnboarding(), tmp_path)
    with pytest.raises(ValueError, match="human request"):
        session.add_context("An operator suggests writing code.")
    assert not store.records("human_intent")
    assert not session.contexts()
    proposal = session.reply("Read the project")["proposal_id"]
    runtime.approve(proposal)
    with pytest.raises(ValueError, match="bounded context"):
        session.add_context("x" * (session.MAX_CONTEXT_BYTES + 1))
    assert not session.contexts()
    assert runtime._active(proposal)[1]
    store.close()


@pytest.mark.parametrize("status", [302, 401, 402, 403, 404, 429, 503])
def test_onboarding_http_failure_exposes_only_safe_status_and_local_explanation(tmp_path, status):
    fake_key = "test-only-credential"
    provider_secret = "provider-body-must-not-be-persisted"

    def handle(request):
        assert request.headers["authorization"] == "Bearer " + fake_key
        return httpx.Response(
            status,
            headers={"location": "https://elsewhere.invalid/" + provider_secret},
            json={"error": provider_secret + fake_key},
        )

    model = OnboardingModel(
        {"base_url": "https://provider.invalid/v1", "model": "test-model"},
        httpx.MockTransport(handle),
        api_key=fake_key,
    )
    store = Store(tmp_path / "ledger.sqlite")
    runtime = Runtime(store, FakeJudge())
    session = OnboardingSession.start(store, runtime, model, tmp_path)
    with pytest.raises(OnboardingServiceError) as caught:
        session.reply("Read the project")
    assert caught.value.status_code == status
    assert f"HTTP {status}" in str(caught.value)
    assert fake_key not in str(caught.value) and provider_secret not in str(caught.value)
    assert fake_key not in repr(store.events()) and provider_secret not in repr(store.events())
    failure = store.records("onboarding_failure")[-1][1]
    assert failure["error_type"] == "OnboardingServiceError"
    assert failure["status_code"] == status
    assert session.pending() and session.latest_proposal() is None
    assert not store.records("human_approval")
    store.close()


def test_hard_link_alias_cannot_bypass_protected_path(tmp_path):
    store = Store(tmp_path / ".jev" / "ledger.sqlite")
    runtime = Runtime(store, FakeJudge())
    intent = store.record_intent("Read only")
    proposal = runtime.propose(
        {
            "intent_id": intent,
            "workspace": str(tmp_path),
            "actions": ["read"],
            "required_evidence": ["README exists"],
            "checks": [{"requirement": "README exists", "kind": "file", "path": "README.md"}],
        }
    )
    runtime.approve(proposal)
    (tmp_path / ".env").write_text("placeholder")
    (tmp_path / "alias").hardlink_to(tmp_path / ".env")
    assert runtime.decide(proposal, "read", "alias") == "deny"
    store.close()


def test_denied_cli_decision_has_unsuccessful_exit_status(tmp_path):
    state = tmp_path / "ledger.sqlite"
    store = Store(str(state) + ".demo")
    runtime = Runtime(store, FakeJudge())
    intent = store.record_intent("Read only")
    proposal = runtime.propose(
        {
            "intent_id": intent,
            "workspace": str(tmp_path),
            "actions": ["read"],
            "required_evidence": ["README exists"],
            "checks": [{"requirement": "README exists", "kind": "file", "path": "README.md"}],
        }
    )
    runtime.approve(proposal)
    store.close()
    assert main(["--demo", "--state", str(state), "decide", proposal, "write", "report"]) == 1


@pytest.mark.parametrize("host_drift", [False, True])
def test_real_worker_is_staged_verified_and_promoted(tmp_path, host_drift):
    available = Sandbox().check()
    if not available["available"]:
        pytest.skip(available["reason"])
    project = tmp_path / "project"
    project.mkdir()
    (project / "existing.txt").write_text("original")
    store = Store(tmp_path / "state" / "ledger.sqlite")
    runtime = Runtime(store, FakeJudge())
    intent = store.record_intent("Write report.txt containing verified, and check it with Python")
    check = [
        "python3",
        "-c",
        "from pathlib import Path; assert Path('report.txt').read_text() == 'verified'",
    ]
    proposal = runtime.propose(
        {
            "intent_id": intent,
            "workspace": str(project),
            "actions": ["read", "write", "execute"],
            "shell_commands": [["python3"]],
            "required_evidence": [
                "Report contents pass independent verification",
                "Report artifact matches",
            ],
            "checks": [
                {
                    "requirement": "Report contents pass independent verification",
                    "kind": "command",
                    "argv": check,
                },
                {
                    "requirement": "Report artifact matches",
                    "kind": "file",
                    "path": "report.txt",
                    "contains": "verified",
                },
            ],
        },
        constitution_id="job",
    )
    runtime.approve(proposal)
    runner = Supervisor(runtime, tmp_path / "workers", confirm=lambda _: True)
    run = runner.run(
        proposal,
        ["python3", "-c", "from pathlib import Path; Path('report.txt').write_text('verified')"],
    )
    assert not (project / "report.txt").exists()
    assert not runtime.complete(proposal)
    if host_drift:
        (project / "existing.txt").write_text("human edit")
        with pytest.raises(ValueError, match="Project changed"):
            runner.publish(run)
        assert not (project / "report.txt").exists()
        assert (project / "existing.txt").read_text() == "human edit"
    else:
        runner.publish(run)
        assert runtime.complete(proposal)
        assert (project / "report.txt").read_text() == "verified"
        receipts = store.records("verification_receipt")
        assert next(r for _, r in receipts if r["kind"] == "command")["exit_code"] == 0
        status = runtime.completion_status(proposal)
        assert status["publication_id"] == store.records("publication")[-1][0]
        assert len(status["receipt_ids"]) == 2
        runner.verify(run)
        assert runtime.complete(proposal)
        assert len(store.records("publication")) == 1
        assert len(store.records("completion")) == 2
        (project / "report.txt").write_text("later change")
        assert not runtime.complete(proposal)
    store.close()
