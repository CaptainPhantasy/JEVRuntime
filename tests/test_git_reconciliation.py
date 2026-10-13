"""Durable Git evidence and real child-process loss, without live model/remote calls."""

import json
import subprocess
import sys
import textwrap
import time

import pytest
from test_git_policy import git, repository, runtime_for, stage  # noqa: F401

from jev_runtime.git_operations import (
    abandon_git,
    execute_git,
    git_attempts,
    git_report,
    mutation_lock,
    unresolved_git,
)
from jev_runtime.supervisor import Supervisor


@pytest.fixture
def setup(repository, tmp_path):  # noqa: F811
    stage(repository, "report.txt", "Reviewed report\n")
    runtime, store, proposal, policy, gateway = runtime_for(repository, tmp_path)
    runner = Supervisor(runtime, tmp_path / "workers", confirm=lambda _: True)
    yield runtime, store, proposal, gateway, runner, repository
    store.close()


def consumed(setup):
    runtime, store, proposal, gateway, runner, root = setup
    plan = gateway.preview("commit", message="Reviewed report")
    request_id = runtime.request_operation(proposal, "git", plan)
    runtime.consume_operation(request_id, human_approved=True)
    return unresolved_git(runtime)[0], plan


def test_exact_success_receipt_closes_atomic_attempt(setup):
    runtime, store, proposal, gateway, runner, root = setup
    plan = gateway.preview("commit", message="Reviewed report")
    result = execute_git(runner, proposal, gateway, plan)
    assert git(root, "rev-parse", "HEAD") == result["commit"]
    attempt = git_attempts(runtime)[0]
    assert attempt["receipt_id"] and not unresolved_git(runtime)
    events = store.events()
    consumed_event = next(e for e in events if e["kind"] == "operation_consumed")
    started_event = next(e for e in events if e["kind"] == "git_attempt_started")
    assert started_event["seq"] == consumed_event["seq"] + 1
    assert started_event["payload"]["request_hash"] == consumed_event["payload"]["request_hash"]
    assert runtime.autonomy(proposal)["state"] == "NORMAL"
    assert runtime._executing_git_attempt is None


@pytest.mark.parametrize("phase", ["before_mutation", "receipt"])
def test_uncertain_exception_blocks_retry_and_recovery(setup, monkeypatch, phase):
    runtime, store, proposal, gateway, runner, root = setup
    plan = gateway.preview("commit", message="Reviewed report")
    if phase == "before_mutation":

        def fail(*args, **kwargs):
            raise RuntimeError("Injected interruption")

        monkeypatch.setattr(gateway, "execute", fail)
    else:
        original = store.append

        def append(kind, payload):
            if kind == "git_receipt":
                raise RuntimeError("Injected missing receipt")
            return original(kind, payload)

        monkeypatch.setattr(store, "append", append)
    with pytest.raises(RuntimeError):
        execute_git(runner, proposal, gateway, plan)
    assert bool(git(root, "rev-list", "--all")) == (phase == "receipt")
    assert len(unresolved_git(runtime)) == 1
    assert runtime.autonomy(proposal)["reason"] == "git_requires_reconciliation"
    assert runtime.completion_status(proposal)["reason"] == "git_unfinished"
    judgments = len(store.records("judgment"))
    with pytest.raises(ValueError, match="denied"):
        execute_git(runner, proposal, gateway, plan)
    with pytest.raises(ValueError, match="Git"):
        runner.recover(proposal)
    with pytest.raises(ValueError, match="Git"):
        runtime.recover(proposal, human_approved=True, verification_id="invented")
    assert len(store.records("judgment")) == judgments
    assert runtime._executing_git_attempt is None


@pytest.mark.parametrize("after_mutation", [False, True])
def test_abrupt_process_loss_leaves_durable_quarantine(setup, after_mutation):
    runtime, store, proposal, gateway, runner, root = setup
    script = r"""
import os,sys
from pathlib import Path
from jev_runtime.contracts import digest
from jev_runtime.git_gateway import GitGateway
from jev_runtime.git_operations import execute_git
from jev_runtime.judgment import FakeJudge
from jev_runtime.runtime import Runtime
from jev_runtime.store import Store
from jev_runtime.supervisor import Supervisor
store=Store(sys.argv[1]);runtime=Runtime(store,FakeJudge());proposal=sys.argv[2]
policy,_=runtime._active(proposal)
gateway=GitGateway(policy.workspace,protected_paths=policy.protected_paths,policy_hash=digest(policy.as_dict()))
runner=Supervisor(runtime,sys.argv[3],confirm=lambda _:True)
plan=gateway.preview('commit',message='Reviewed report')
if sys.argv[4]=='True':
    original=store.append
    def append(kind,payload):
        if kind=='git_receipt':os._exit(73)
        return original(kind,payload)
    store.append=append
else:
    gateway.execute=lambda *a,**k:os._exit(73)
execute_git(runner,proposal,gateway,plan)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(script),
            str(store.path),
            proposal,
            str(runner.directory),
            str(after_mutation),
        ],
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 73, result.stderr.decode()
    attempt = unresolved_git(runtime)[0]
    assert not store.records("git_failure") and not store.records("git_receipt")
    report = git_report(runtime, attempt["attempt_id"])
    assert report["local_state"] == (
        "changed_since_approval" if after_mutation else "matches_approved_snapshot"
    )
    assert not report["completion_established"]
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"


def test_git_helper_inherits_lock_after_parent_death(setup):
    runtime, store, proposal, gateway, runner, root = setup
    ready, done = root.parent / "ready", root.parent / "done"
    script = r"""
import os,sys,threading,time,shlex
from pathlib import Path
from jev_runtime.contracts import digest
from jev_runtime.git_gateway import GitGateway
from jev_runtime.git_operations import execute_git
from jev_runtime.judgment import FakeJudge
from jev_runtime.runtime import Runtime
from jev_runtime.store import Store
from jev_runtime.supervisor import Supervisor
store=Store(sys.argv[1]);runtime=Runtime(store,FakeJudge());proposal=sys.argv[2]
policy,_=runtime._active(proposal)
gateway=GitGateway(policy.workspace,protected_paths=policy.protected_paths,policy_hash=digest(policy.as_dict()))
runner=Supervisor(runtime,sys.argv[3],confirm=lambda _:True)
ready,done=map(Path,sys.argv[4:6])
def die():
    while not ready.exists():time.sleep(.01)
    os._exit(73)
threading.Thread(target=die,daemon=True).start()
helper='from pathlib import Path;import time;Path('+repr(str(ready))+').touch();time.sleep(1.5);Path('+repr(str(done))+').touch()'
def delayed(*args,**kwargs):
    return gateway._run(['-c','alias.delayed=!'+shlex.quote(sys.executable)+' -c '+shlex.quote(helper),'delayed'])
gateway.execute=delayed
execute_git(runner,proposal,gateway,gateway.preview('commit',message='Delayed fixture'))
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(script),
            str(store.path),
            proposal,
            str(runner.directory),
            str(ready),
            str(done),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert process.wait(timeout=5) == 73
    attempt = unresolved_git(runtime)[0]
    try:
        assert ready.exists() and not done.exists()
        with pytest.raises(ValueError, match="busy"):
            abandon_git(
                runtime,
                attempt["attempt_id"],
                lambda _: pytest.fail("Must not ask while Git helper is alive"),
            )
    finally:
        deadline = time.monotonic() + 4
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    assert done.exists()
    # The actual shell/Git helper finishes and then releases its inherited lock.
    deadline = time.monotonic() + 2
    while True:
        try:
            with mutation_lock(gateway):
                break
        except ValueError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)
    result = abandon_git(runtime, attempt["attempt_id"], lambda _: True)
    assert result["abandoned"] and not result["authority_restored"]


def test_abandon_keeps_all_affected_policies_quarantined_without_mutation(setup):
    runtime, store, proposal, gateway, runner, root = setup
    other = runtime.propose(store.get(proposal, "policy_proposal")["policy"])
    runtime.approve(other)
    attempt, plan = consumed(setup)
    index = (root / ".git/index").read_bytes()
    result = abandon_git(runtime, attempt["attempt_id"], lambda _: True)
    assert result["abandoned"] and not result["files_or_refs_modified"]
    assert not unresolved_git(runtime)
    assert (root / ".git/index").read_bytes() == index
    assert git(root, "rev-list", "--all") == ""
    for candidate in (proposal, other):
        assert runtime.autonomy(candidate)["state"] == "QUARANTINED"
    with pytest.raises(ValueError, match="already used"):
        runtime.consume_operation(attempt["request_id"], human_approved=True)


@pytest.mark.parametrize("change", ["decline", "ref", "ledger"])
def test_changed_or_declined_abandonment_preserves_uncertainty(setup, change):
    runtime, store, proposal, gateway, runner, root = setup
    attempt, plan = consumed(setup)

    def confirm(_):
        if change == "ref":
            git(root, "commit", "-m", "Host changed during review")
        if change == "ledger":
            store.append("operator_observation", {"text": "Concurrent fixture"})
        return change != "decline"

    with pytest.raises(ValueError, match="authorized|changed"):
        abandon_git(runtime, attempt["attempt_id"], confirm)
    assert unresolved_git(runtime) and not store.records("git_abandoned")


def test_readonly_installed_cli_inspects_revoked_policy_without_provider(setup):
    runtime, store, proposal, gateway, runner, root = setup
    attempt, plan = consumed(setup)
    runtime.suspend(runtime.constitution_id(proposal), "Fixture revocation")
    before, mtime = store.path.read_bytes(), store.path.stat().st_mtime_ns
    result = subprocess.run(
        [
            "/usr/local/bin/jev",
            "--state",
            str(store.path),
            "git",
            "reconcile",
            "--attempt",
            attempt["attempt_id"],
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["unresolved"] and report["remote_state"] == "unobserved"
    assert report["local_state"] == "matches_approved_snapshot"
    assert store.path.read_bytes() == before and store.path.stat().st_mtime_ns == mtime


def test_report_observes_exact_ref_after_head_switch(setup):
    runtime, store, proposal, gateway, runner, root = setup
    git(root, "commit", "-m", "Baseline")
    stage(root, "report.txt", "next\n")
    attempt, plan = consumed(setup)
    old = plan["state"]["head"]
    git(root, "commit", "-m", "Changed main")
    new = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "--detach", old)
    report = git_report(runtime, attempt["attempt_id"])
    assert report["observed_ref_head"] == new
    assert report["observed_repository"]["head"] == old
    assert report["local_state"] == "changed_since_approval"


@pytest.mark.parametrize(
    "damage", [None, "no_receipt", "wrong_plan", "wrong_proposal", "wrong_operation"]
)
def test_legacy_consumed_request_only_closes_with_exact_success(setup, damage):
    from jev_runtime.contracts import digest

    runtime, store, proposal, gateway, runner, root = setup
    plan = gateway.preview("commit", message="Historical commit")
    # Represents the pre-v2 plan schema, not a new executable grant.
    for key in ("git_review", "policy_hash", "protected_paths"):
        plan.pop(key)
    plan["version"] = 1
    from test_git_policy import resign

    resign(plan)
    request = {
        "proposal_id": proposal,
        "action": "git",
        "payload": plan,
        "decision": "approval_required",
        "reason": "human_confirmation",
    }
    request_id = store.append("operation_request", request)
    store.append(
        "human_operation_approval", {"request_id": request_id, "request_hash": digest(request)}
    )
    consumed_id = store.append(
        "operation_consumed", {"request_id": request_id, "request_hash": digest(request)}
    )
    git(root, "commit", "-m", "Historical commit")
    receipt = {
        "proposal_id": proposal,
        "request_id": request_id,
        "result": {
            "operation": "commit",
            "plan_id": plan["plan_id"],
            "commit": git(root, "rev-parse", "HEAD"),
        },
    }
    if damage == "wrong_plan":
        receipt["result"]["plan_id"] = "0" * 64
    elif damage == "wrong_proposal":
        receipt["proposal_id"] = "other"
    elif damage == "wrong_operation":
        receipt["result"]["operation"] = "push"
    if damage != "no_receipt":
        store.append("git_receipt", receipt)
    attempt = git_attempts(runtime)[0]
    assert attempt["attempt_id"] == consumed_id and attempt["legacy"]
    assert attempt["unresolved"] == (damage is not None)


def test_atomic_consumption_fails_if_another_writer_changes_ledger(setup, monkeypatch):
    from jev_runtime.store import Store

    runtime, store, proposal, gateway, runner, root = setup
    plan = gateway.preview("commit", message="Reviewed")
    request_id = runtime.request_operation(proposal, "git", plan)
    original = store.append_many

    def interleave(records, **kwargs):
        other = Store(store.path)
        try:
            other.append("operator_observation", {"text": "Interleaved fixture"})
        finally:
            other.close()
        return original(records, **kwargs)

    monkeypatch.setattr(store, "append_many", interleave)
    with pytest.raises(ValueError, match="Ledger changed"):
        runtime.consume_operation(request_id, human_approved=True)
    assert not store.records("operation_consumed")
    assert not store.records("git_attempt_started")
    assert not store.records("human_operation_approval")


def test_abandonment_final_transaction_rejects_new_event(setup, monkeypatch):
    from jev_runtime.store import Store

    runtime, store, proposal, gateway, runner, root = setup
    attempt, plan = consumed(setup)
    original = store.append_many

    def interleave(records, **kwargs):
        other = Store(store.path)
        try:
            other.append("operator_observation", {"text": "Interleaved fixture"})
        finally:
            other.close()
        return original(records, **kwargs)

    monkeypatch.setattr(store, "append_many", interleave)
    with pytest.raises(ValueError, match="Ledger changed"):
        abandon_git(runtime, attempt["attempt_id"], lambda _: True)
    assert unresolved_git(runtime) and not store.records("git_abandoned")


def test_revocation_before_ref_mutation_prevents_commit_publication(setup, monkeypatch):
    runtime, store, proposal, gateway, runner, root = setup
    original = gateway._run

    def revoke(args, **kwargs):
        result = original(args, **kwargs)
        if args[0] == "commit-tree":
            runtime.suspend(runtime.constitution_id(proposal), "Revoked during commit preparation")
        return result

    monkeypatch.setattr(gateway, "_run", revoke)
    with pytest.raises(ValueError, match="authority changed"):
        execute_git(
            runner, proposal, gateway, gateway.preview("commit", message="Denied after revocation")
        )
    assert git(root, "rev-list", "--all") == ""
    assert unresolved_git(runtime)


def test_successful_outcome_receipt_does_not_restore_new_quarantine(setup, monkeypatch):
    runtime, store, proposal, gateway, runner, root = setup
    original = gateway.execute

    def restrict(*args, **kwargs):
        result = original(*args, **kwargs)
        runtime.failure(proposal, "New observed failure after mutation", quarantine=True)
        return result

    monkeypatch.setattr(gateway, "execute", restrict)
    result = execute_git(
        runner, proposal, gateway, gateway.preview("commit", message="Recorded outcome")
    )
    assert git(root, "rev-parse", "HEAD") == result["commit"]
    assert not unresolved_git(runtime)
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"


def test_repository_lock_shared_between_different_worker_directories(setup, tmp_path):
    from jev_runtime.git_gateway import GitGateway

    runtime, store, proposal, gateway, runner, root = setup
    another = Supervisor(
        runtime,
        tmp_path / "other-workers",
        confirm=lambda _: pytest.fail("Must stop before approval"),
    )
    second_gateway = GitGateway(
        root, protected_paths=gateway.protected_paths, policy_hash=gateway.policy_hash
    )
    plan = second_gateway.preview("commit", message="Other process")
    with mutation_lock(gateway):
        with pytest.raises(ValueError, match="busy"):
            execute_git(another, proposal, second_gateway, plan)
    assert not store.records("operation_consumed")


def test_readonly_cli_lists_legacy_attempt_id_without_active_policy(setup):
    from jev_runtime.contracts import digest

    runtime, store, proposal, gateway, runner, root = setup
    plan = gateway.preview("commit", message="Historical uncertain attempt")
    request = {
        "proposal_id": proposal,
        "action": "git",
        "payload": plan,
        "decision": "approval_required",
    }
    request_id = store.append("operation_request", request)
    store.append(
        "human_operation_approval", {"request_id": request_id, "request_hash": digest(request)}
    )
    consumed_id = store.append(
        "operation_consumed", {"request_id": request_id, "request_hash": digest(request)}
    )
    runtime.suspend(runtime.constitution_id(proposal), "Fixture revocation")
    before = store.path.read_bytes()
    result = subprocess.run(
        ["/usr/local/bin/jev", "--state", str(store.path), "git", "reconcile"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    attempts = json.loads(result.stdout)
    assert len(attempts) == 1 and attempts[0]["attempt_id"] == consumed_id
    assert attempts[0]["legacy"] and attempts[0]["unresolved"]
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "damage",
    [
        "missing_plan_id",
        "changed_plan",
        "missing_approval",
        "wrong_approval_hash",
        "wrong_plan_operation",
    ],
)
def test_malformed_legacy_success_cannot_clear_uncertainty(setup, damage):
    from test_git_policy import resign

    from jev_runtime.contracts import digest

    runtime, store, proposal, gateway, runner, root = setup
    plan = gateway.preview("commit", message="Historical uncertain result")
    if damage == "missing_plan_id":
        del plan["plan_id"]
    elif damage == "changed_plan":
        plan["arguments"]["message"] = "Different message after checksum"
    elif damage == "wrong_plan_operation":
        plan["operation"] = "push"
        resign(plan)
    request = {
        "proposal_id": proposal,
        "action": "git",
        "payload": plan,
        "decision": "approval_required",
    }
    request_id = store.append("operation_request", request)
    if damage != "missing_approval":
        store.append(
            "human_operation_approval",
            {
                "request_id": request_id,
                "request_hash": "0" * 64 if damage == "wrong_approval_hash" else digest(request),
            },
        )
    store.append("operation_consumed", {"request_id": request_id, "request_hash": digest(request)})
    git(root, "commit", "-m", "Historical fixture")
    result = {"operation": "commit", "commit": git(root, "rev-parse", "HEAD")}
    if "plan_id" in plan:
        result["plan_id"] = plan["plan_id"]
    store.append(
        "git_receipt", {"proposal_id": proposal, "request_id": request_id, "result": result}
    )
    assert len(unresolved_git(runtime)) == 1
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"


@pytest.mark.parametrize("damage", [None, "remote_url", "target_ref", "commit"])
def test_push_receipt_requires_exact_pinned_destination(setup, damage):

    runtime, store, proposal, gateway, runner, root = setup
    git(root, "commit", "-m", "Local fixture")
    plan = gateway.preview("push", remote="origin", branch="main")
    request_id = runtime.request_operation(proposal, "push", plan)
    runtime.consume_operation(request_id, human_approved=True)
    attempt = unresolved_git(runtime)[0]
    result = {
        "operation": "push",
        "plan_id": plan["plan_id"],
        "commit": plan["state"]["head"],
        "remote_url": plan["remote_url"],
        "target_ref": plan["target_ref"],
    }
    if damage:
        result[damage] = "0" * 40 if damage == "commit" else "different"
    store.append(
        "git_receipt",
        {
            "format": "jevruntime.git-receipt.v1",
            "attempt_id": attempt["attempt_id"],
            "proposal_id": proposal,
            "request_id": request_id,
            "result": result,
        },
    )
    assert bool(unresolved_git(runtime)) == (damage is not None)
    assert git_report(runtime, attempt["attempt_id"])["remote_state"] == "unobserved"
