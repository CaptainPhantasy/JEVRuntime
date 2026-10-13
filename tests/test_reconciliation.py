"""Actual process loss must not become authority or a fabricated completion."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_completion import staged as staged

from jev_runtime.cli import main
from jev_runtime.contracts import Decision
from jev_runtime.judgment import FakeJudge
from jev_runtime.reconciliation import publication_report, unresolved_publications
from jev_runtime.runtime import Runtime
from jev_runtime.store import Store
from jev_runtime.supervisor import Supervisor


def crash_publisher(staged, moment):
    runtime, runner, run, _, _ = staged
    script = """
import os, sys
from jev_runtime.store import Store
from jev_runtime.runtime import Runtime
from jev_runtime.judgment import FakeJudge
from jev_runtime.supervisor import Supervisor
import jev_runtime.supervisor as module
original = module.apply_changes
moment = sys.argv[4]
def crash(*args, **kwargs):
    if moment == 'after':
        original(*args, **kwargs)
    os._exit(73)
module.apply_changes = crash
store = Store(sys.argv[1])
Supervisor(Runtime(store, FakeJudge()), sys.argv[2], confirm=lambda _: True).publish(sys.argv[3])
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(runtime.store.path), str(runner.directory), run, moment],
        capture_output=True,
        text=True,
        timeout=15,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    assert result.returncode == 73, (result.stdout, result.stderr)
    assert runtime.store.records("publication_failed") == []
    assert runtime.store.records("publication") == []
    assert runtime.store.records("autonomy") == []
    return runtime, runner, run, staged[3], staged[4]


@pytest.mark.parametrize("moment,state", [("before", "baseline"), ("after", "target")])
def test_process_loss_is_inspected_without_writes_or_inference(staged, capsys, moment, state):
    runtime, runner, run, proposal, _ = crash_publisher(staged, moment)
    ledger = runtime.store.path
    before = ledger.read_bytes(), ledger.stat().st_mtime_ns
    assert main(["--state", str(ledger), "reconcile", run]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["host_state"] == state
    assert report["unresolved"] is True
    assert report["completion_established"] is False
    assert (ledger.read_bytes(), ledger.stat().st_mtime_ns) == before
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"
    assert not runtime.complete(proposal)
    before_judgments = len(runtime.store.records("judgment"))
    request = runtime.request_operation(proposal, "write", {"path": "report.txt"})
    assert runtime.store.get(request, "operation_request")["decision"] == Decision.DENY
    with pytest.raises(ValueError, match="denied"):
        runtime.consume_operation(request, human_approved=True)
    for action in ("read", "write", "delete", "push"):
        assert runtime.decide(proposal, action, "report.txt") == Decision.DENY
    with pytest.raises(ValueError, match="Reconcile"):
        runner.recover(proposal)
    assert len(runtime.store.records("judgment")) == before_judgments
    assert runtime.store.records("recovery_started") == []


def test_abandonment_preserves_target_and_still_requires_fresh_recovery(staged):
    runtime, runner, run, proposal, project = crash_publisher(staged, "after")
    before = (project / "report.txt").read_bytes()
    result = runner.abandon_publication(run)
    assert (
        result["files_modified"]
        is result["completion_established"]
        is result["authority_restored"]
        is False
    )
    assert (project / "report.txt").read_bytes() == before
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"
    assert unresolved_publications(runtime, run_id=run) == []
    assert publication_report(runtime, run)["abandoned"] is True
    with pytest.raises(ValueError, match="abandoned"):
        runner.verify(run)
    with pytest.raises(ValueError, match="abandoned"):
        runner.publish(run)
    with pytest.raises(ValueError, match="No unresolved"):
        runner.abandon_publication(run)
    runner.recover(proposal)
    assert runtime.autonomy(proposal)["state"] == "NORMAL"
    assert runtime.complete(proposal) is False
    assert runtime.store.records("publication") == runtime.store.records("completion") == []


def test_declined_abandonment_leaves_evidence_and_files_untouched(staged):
    runtime, runner, run, _, project = crash_publisher(staged, "after")
    before = runtime.store.path.read_bytes(), (project / "report.txt").read_bytes()
    runner.confirm = lambda _: False
    with pytest.raises(ValueError, match="not authorized"):
        runner.abandon_publication(run)
    assert (runtime.store.path.read_bytes(), (project / "report.txt").read_bytes()) == before
    assert unresolved_publications(runtime, run_id=run)


@pytest.mark.parametrize("change", ["files", "ledger"])
def test_abandonment_rejects_changes_during_human_review(staged, change):
    runtime, runner, run, _, project = crash_publisher(staged, "after")

    def confirm(_):
        if change == "files":
            (project / "report.txt").write_text("Human changed this during review")
        else:
            runtime.store.record_intent("New human instruction during review")
        return True

    runner.confirm = confirm
    with pytest.raises(ValueError, match="changed"):
        runner.abandon_publication(run)
    assert runtime.store.records("publication_abandoned") == []
    assert unresolved_publications(runtime, run_id=run)
    if change == "files":
        assert (project / "report.txt").read_text() == "Human changed this during review"


def test_abandonment_rejects_concurrent_event_at_atomic_commit(staged, monkeypatch):
    runtime, runner, run, proposal, _ = crash_publisher(staged, "after")
    original = runtime.store.append_many

    def append(records, **kwargs):
        if kwargs.get("expected_head"):
            other = Store(runtime.store.path)
            other.record_intent("New instruction immediately before closure")
            other.close()
        return original(records, **kwargs)

    monkeypatch.setattr(runtime.store, "append_many", append)
    with pytest.raises(ValueError, match="Ledger changed"):
        runner.abandon_publication(run)
    assert runtime.store.records("publication_abandoned") == []
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"


def test_abandonment_cannot_run_while_publisher_holds_lock(staged):
    runtime, runner, run, _, _ = crash_publisher(staged, "before")
    other = Supervisor(runtime, runner.directory, confirm=lambda _: True)
    with runner._lock(), pytest.raises(ValueError, match="Another worker"):
        other.abandon_publication(run)
    assert runtime.store.records("publication_abandoned") == []


def test_divergent_host_is_never_restored_or_certified(staged):
    runtime, runner, run, proposal, project = crash_publisher(staged, "after")
    (project / "report.txt").write_text("Preserve this human correction")
    assert publication_report(runtime, run)["host_state"] == "divergent"
    runner.abandon_publication(run)
    assert (project / "report.txt").read_text() == "Preserve this human correction"
    with pytest.raises(ValueError, match="Recovery verification failed"):
        runner.recover(proposal)
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"
    assert runtime.complete(proposal) is False


def test_missing_host_is_not_empty_or_abandonable(staged):
    runtime, runner, run, _, project = crash_publisher(staged, "before")
    project.rename(project.with_name("preserved-project"))
    report = publication_report(runtime, run)
    assert report["host_state"] == "unreadable" and report["host_hash"] is None
    with pytest.raises(ValueError, match="Restore access"):
        runner.abandon_publication(run)
    assert runtime.store.records("publication_abandoned") == []


def test_uncertainty_is_scoped_to_constitution_and_project(staged, tmp_path):
    runtime, runner, run, proposal, project = crash_publisher(staged, "before")
    original = runtime.store.get(proposal, "policy_proposal")["policy"]
    other = tmp_path / "unrelated-project"
    other.mkdir()
    same_project = runtime.propose(
        {**original, "workspace": str(project)}, constitution_id="other-policy"
    )
    other_project = runtime.propose(
        {**original, "workspace": str(other)}, constitution_id="unrelated-policy"
    )
    alias_project = runtime.propose(
        {**original, "workspace": str(project) + "/."}, constitution_id="alias-policy"
    )
    runtime.approve(alias_project)
    runtime.approve(same_project)
    runtime.approve(other_project)
    assert runtime.autonomy(same_project)["state"] == "QUARANTINED"
    assert runtime.autonomy(alias_project)["state"] == "QUARANTINED"
    assert runtime.autonomy(other_project)["state"] == "NORMAL"
    runner.abandon_publication(run)
    assert runtime.autonomy(same_project)["state"] == "QUARANTINED"
    assert runtime.autonomy(alias_project)["state"] == "QUARANTINED"
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"
    assert runtime.autonomy(other_project)["state"] == "NORMAL"


def test_active_publisher_exemption_does_not_hide_new_quarantine(staged, monkeypatch):
    runtime, runner, run, proposal, project = staged
    original = runner._authorize

    def authorize(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[1] == "write":
            runtime.failure(proposal, "A new independent fault", quarantine=True)
        return result

    monkeypatch.setattr(runner, "_authorize", authorize)
    with pytest.raises(ValueError, match="autonomy"):
        runner.publish(run)
    assert not (project / "report.txt").exists()
    assert runtime._publishing_attempt is None
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"
    assert runtime.complete(proposal) is False


def test_new_failure_never_reduces_existing_quarantine(staged):
    runtime, _, _, proposal, _ = staged
    runtime.failure(proposal, "Serious fault", quarantine=True)
    runtime.failure(proposal, "Later ordinary failure")
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"


@pytest.mark.parametrize("fault", ["missing_hash", "invalid_hash", "wrong_proposal"])
def test_incomplete_legacy_publication_cannot_clear_uncertainty(staged, tmp_path, fault):
    from copy import deepcopy

    runtime, runner, run, proposal, _ = staged
    runner.publish(run)
    events = deepcopy(runtime.store.events())
    publication = next(e["payload"] for e in events if e["kind"] == "publication")
    started = next(e["payload"] for e in events if e["kind"] == "publication_started")
    result = next(e["payload"] for e in events if e["kind"] == "runtime_result")
    started.pop("verification_id")
    started["changes"] = publication["changes"]
    for key in ("format", "attempt_id", "verification_id"):
        publication.pop(key)
    if fault == "missing_hash":
        publication.pop("tree_hash")
        result.pop("tree_hash")
    elif fault == "invalid_hash":
        publication["tree_hash"] = result["tree_hash"] = "not-a-tree-hash"
    else:
        result["proposal_id"] = "different-proposal"
    store = Store(tmp_path / "damaged-legacy.sqlite")
    try:
        store.append_many(
            [(e["kind"], e["payload"]) for e in events], identifiers=[e["id"] for e in events]
        )
        historical = Runtime(store, FakeJudge())
        assert unresolved_publications(historical, run_id=run)
        assert historical.autonomy(proposal)["state"] == "QUARANTINED"
        assert historical.complete(proposal) is False
    finally:
        store.close()
