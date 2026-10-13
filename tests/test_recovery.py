"""Recovery restores capabilities only from fresh, unchanged, human-reviewed evidence."""

import pytest

from jev_runtime.judgment import FakeJudge
from jev_runtime.runtime import Runtime
from jev_runtime.sandbox import Sandbox
from jev_runtime.store import Store
from jev_runtime.supervisor import Supervisor


@pytest.fixture
def recovery(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "report.txt").write_text("verified")
    store = Store(tmp_path / "ledger.sqlite")
    runtime = Runtime(store, FakeJudge())
    source = store.record_intent("Verify report.txt and the independent check before recovering.")
    proposal = runtime.propose(
        {
            "intent_id": source,
            "workspace": str(project),
            "actions": ["read", "execute"],
            "shell_commands": [["python3"]],
            "required_evidence": ["report", "command"],
            "checks": [
                {
                    "requirement": "report",
                    "kind": "file",
                    "path": "report.txt",
                    "contains": "verified",
                },
                {
                    "requirement": "command",
                    "kind": "command",
                    "argv": ["python3", "-c", "print('checked')"],
                    "contains": "checked",
                },
            ],
        },
        constitution_id="recovery-job",
    )
    runtime.approve(proposal)
    runtime.failure(proposal, "Recovery required", quarantine=True)
    runner = Supervisor(runtime, tmp_path / "workers", confirm=lambda _: True)
    yield runtime, runner, proposal, project
    store.close()


def real_sandbox():
    available = Sandbox().check()
    if not available["available"]:
        pytest.skip(available["reason"])


def test_recovery_blocks_host_change_during_judgment(recovery):
    real_sandbox()
    runtime, runner, proposal, project = recovery

    class ChangedHost:
        def judge(self, kind, state):
            if kind == "completion":
                (project / "report.txt").write_text("human revision after checks")
            return FakeJudge().judge(kind, state)

    runtime.judge = ChangedHost()
    with pytest.raises(ValueError):
        runner.recover(proposal)
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"
    assert (project / "report.txt").read_text() == "human revision after checks"


def test_recovery_runs_checks_while_quarantined_and_consumes_once(recovery):
    real_sandbox()
    runtime, runner, proposal, _ = recovery
    observed = []
    original = runner.sandbox.run

    def checked(*args, **kwargs):
        observed.append(runtime.autonomy(proposal)["state"])
        return original(*args, **kwargs)

    runner.sandbox.run = checked
    verification = runner.recover(proposal)
    assert observed == ["QUARANTINED"]
    assert runtime.autonomy(proposal)["state"] == "NORMAL"
    assert runtime.autonomy(proposal)["verification_id"] == verification
    assert not runtime.store.records("publication") and not runtime.store.records("completion")
    with pytest.raises(ValueError):
        runtime.recover(proposal, verification_id=verification, human_approved=True)
    runtime.failure(proposal, "Later failure", quarantine=True)
    with pytest.raises(ValueError):
        runtime.recover(proposal, verification_id=verification, human_approved=True)
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"
    assert not list(runner.directory.glob("recovery-*"))


@pytest.mark.parametrize(
    "flag,value",
    [
        ("output_truncated", True),
        ("output_truncated", None),
        ("timed_out", None),
        ("stopped", None),
        ("exit_code", False),
    ],
)
def test_incomplete_command_receipt_cannot_restore(recovery, flag, value):
    runtime, runner, proposal, _ = recovery
    result = {
        "exit_code": 0,
        "output": "checked",
        "output_truncated": False,
        "timed_out": False,
        "stopped": False,
    }
    if value is None:
        result.pop(flag)
    else:
        result[flag] = value
    runner.sandbox.run = lambda *args, **kwargs: result
    with pytest.raises(ValueError):
        runner.recover(proposal)
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"
    assert not runtime.store.records("recovery_verification")
    assert runtime.store.records("recovery_failed")


@pytest.mark.parametrize("change", ["revoke", "new_failure", "jev_failure"])
def test_changed_authority_or_failed_judgment_cannot_restore(recovery, change):
    real_sandbox()
    runtime, runner, proposal, _ = recovery

    class ChangeDuringJudgment:
        def judge(self, kind, state):
            if kind == "completion":
                if change == "revoke":
                    runtime.suspend("recovery-job")
                elif change == "new_failure":
                    runtime.failure(proposal, "New failure", quarantine=True)
                else:
                    raise TimeoutError("simulated unavailable judgment")
            return FakeJudge().judge(kind, state)

    runtime.judge = ChangeDuringJudgment()
    with pytest.raises(ValueError):
        runner.recover(proposal)
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"
    assert runtime.store.records("recovery_failed")


def test_human_decline_never_runs_checks_or_restores(recovery):
    runtime, runner, proposal, _ = recovery
    runner.confirm = lambda _: False
    runner.sandbox.run = lambda *a, **k: pytest.fail("No command may run after decline")
    with pytest.raises(ValueError, match="authorized"):
        runner.recover(proposal)
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"
    assert not runtime.store.records("recovery_receipt")


def test_concurrent_failure_at_restoration_cannot_be_overwritten(recovery, monkeypatch):
    real_sandbox()
    runtime, runner, proposal, _ = recovery
    original = runtime.store.append_many

    def concurrent_append(records, **kwargs):
        if kwargs.get("expected_head"):
            other = Store(runtime.store.path)
            try:
                Runtime(other, FakeJudge()).failure(proposal, "Concurrent failure", quarantine=True)
            finally:
                other.close()
        return original(records, **kwargs)

    monkeypatch.setattr(runtime.store, "append_many", concurrent_append)
    with pytest.raises(ValueError, match="Ledger changed"):
        runner.recover(proposal)
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"
    assert runtime.autonomy(proposal)["reason"] == "Concurrent failure"


def test_legacy_or_unlinked_recovery_receipt_cannot_restore(recovery):
    runtime, _, proposal, _ = recovery
    policy, _ = runtime._active(proposal)
    from jev_runtime.contracts import digest

    legacy = runtime.store.append(
        "recovery_verification",
        {"proposal_id": proposal, "policy_hash": digest(policy.as_dict()), "passed": True},
    )
    with pytest.raises(ValueError):
        runtime.recover(proposal, verification_id=legacy, human_approved=True)
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"


def test_revocation_between_checks_prevents_next_command(recovery, monkeypatch):
    import jev_runtime.supervisor as supervisor

    runtime, runner, proposal, _ = recovery
    read_change = supervisor.read_change

    def revoke_after_read(*args, **kwargs):
        content = read_change(*args, **kwargs)
        runtime.suspend("recovery-job")
        return content

    monkeypatch.setattr(supervisor, "read_change", revoke_after_read)
    runner.sandbox.run = lambda *a, **k: pytest.fail("No next command may run after revocation")
    with pytest.raises(ValueError, match="Authority changed"):
        runner.recover(proposal)
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"
