"""Completion needs a bound execution/acceptance/publication chain, not a success label."""

from copy import deepcopy
from pathlib import Path

import pytest

from jev_runtime.cli import main
from jev_runtime.judgment import FakeJudge
from jev_runtime.runtime import Runtime
from jev_runtime.sandbox import Sandbox
from jev_runtime.store import Store
from jev_runtime.supervisor import Supervisor
from jev_runtime.workspace import WorkspaceChanged, WorkspaceError


@pytest.fixture
def staged(tmp_path):
    availability = Sandbox().check()
    if not availability["available"]:
        pytest.skip(availability["reason"])
    project = tmp_path / "project"
    project.mkdir()
    store = Store(tmp_path / "ledger.sqlite")
    runtime = Runtime(store, FakeJudge())
    intent = store.record_intent("Write report.txt containing verified; independently check it.")
    proposal = runtime.propose(
        {
            "intent_id": intent,
            "workspace": str(project),
            "actions": ["read", "write", "execute"],
            "shell_commands": [["python3"]],
            "required_evidence": ["report content", "independent command"],
            "checks": [
                {
                    "requirement": "report content",
                    "kind": "file",
                    "path": "report.txt",
                    "contains": "verified",
                },
                {
                    "requirement": "independent command",
                    "kind": "command",
                    "argv": [
                        "python3",
                        "-c",
                        "from pathlib import Path; assert Path('report.txt').read_text() == 'verified'; print('checked')",
                    ],
                    "contains": "checked",
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
    yield runtime, runner, run, proposal, project
    store.close()


@pytest.fixture
def published(staged):
    runtime, runner, run, proposal, _ = staged
    runner.publish(run)
    assert runtime.complete(proposal)
    return staged


def replay(tmp_path, events):
    # Build a separate, hash-valid fault fixture; never alter the real append-only ledger.
    store = Store(tmp_path / "fault.sqlite")
    store.append_many(
        [(e["kind"], e["payload"]) for e in events], identifiers=[e["id"] for e in events]
    )
    return store


@pytest.mark.parametrize(
    "kind,field,value,reason",
    [
        ("runtime_result", "output_truncated", None, "worker_or_verification_failed"),
        ("runtime_result", "output_truncated", True, "worker_or_verification_failed"),
        ("verification_receipt", "output_truncated", None, "acceptance_predicate_failed"),
        ("verification_receipt", "timed_out", True, "acceptance_predicate_failed"),
        ("verification_receipt", "exit_code", 1, "acceptance_predicate_failed"),
        (
            "verification_receipt",
            "attempt_id",
            "different attempt",
            "mismatched_verification_receipt",
        ),
        ("verification_receipt", "run_id", "different run", "mismatched_verification_receipt"),
        ("verification_receipt", "check_hash", "different check", "acceptance_predicate_failed"),
        ("run_verification", "receipt_ids", [], "incomplete_receipt_coverage"),
        ("judgment", "state_hash", "unbound", "completion_judgment_failed_or_unbound"),
        ("publication", "verification_id", "missing", "invalid_or_missing_completion_provenance"),
        (
            "publication_started",
            "verification_id",
            "different review",
            "invalid_publication_provenance",
        ),
        ("completion", "mode", "reverification", "invalid_reverification_order"),
    ],
)
def test_broken_chain_cannot_pass(published, tmp_path, kind, field, value, reason):
    runtime, _, _, proposal, _ = published
    events = deepcopy(runtime.store.events())
    candidates = [e for e in events if e["kind"] == kind]
    if kind == "verification_receipt":
        candidates = [e for e in candidates if e["payload"]["kind"] == "command"]
    if kind == "judgment":
        candidates = [e for e in candidates if e["payload"]["kind"] == "completion"]
    payload = candidates[-1]["payload"]
    if value is None:
        payload.pop(field)
    else:
        payload[field] = value
    store = replay(tmp_path, events)
    try:
        assert Runtime(store, FakeJudge()).completion_status(proposal)["reason"] == reason
    finally:
        store.close()


@pytest.mark.parametrize(
    "kind,payload,reason",
    [
        ("completion", {"passed": False}, "latest_completion_failed"),
        ("completion", {"passed": True}, "fresh_verification_required"),
        ("verification_started", {}, "newer_run_evidence_requires_verification"),
        ("verification_failed", {}, "run_failed_or_stopped"),
        ("publication_failed", {}, "run_failed_or_stopped"),
        ("publication_started", {}, "publication_unfinished"),
    ],
)
def test_latest_failed_or_unfinished_work_invalidates_old_success(published, kind, payload, reason):
    runtime, _, run, proposal, _ = published
    runtime.store.append(kind, {"run_id": run, "proposal_id": proposal, **payload})
    assert runtime.completion_status(proposal)["reason"] == reason


def test_completion_requires_publication_and_full_coverage(published, tmp_path):
    runtime, _, _, proposal, _ = published
    events = [e for e in runtime.store.events() if e["kind"] != "publication"]
    store = replay(tmp_path, events)
    try:
        assert Runtime(store, FakeJudge()).completion_status(proposal)["passed"] is False
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["suspend", "quarantine", "new_attempt"])
def test_status_rechecks_authority_and_events_after_hashing(published, monkeypatch, operation):
    import jev_runtime.completion as completion

    runtime, _, run, proposal, _ = published
    snapshot = completion.snapshot

    def concurrent_change(*args, **kwargs):
        manifest = snapshot(*args, **kwargs)
        if operation == "suspend":
            runtime.suspend("job")
        elif operation == "quarantine":
            runtime.failure(proposal, "Concurrent failure", quarantine=True)
        else:
            runtime.store.append("verification_started", {"run_id": run})
        return manifest

    monkeypatch.setattr(completion, "snapshot", concurrent_change)
    assert runtime.completion_status(proposal)["passed"] is False


@pytest.mark.parametrize("operation", ["suspend", "quarantine", "host_edit"])
def test_reverification_records_failure_after_staged_checks(published, monkeypatch, operation):
    import jev_runtime.supervisor as supervisor

    runtime, runner, run, proposal, project = published
    snapshot = supervisor.snapshot

    def concurrent_change(path, **kwargs):
        if Path(path) == project:
            if operation == "suspend":
                runtime.suspend("job")
            elif operation == "quarantine":
                runtime.failure(proposal, "Concurrent failure", quarantine=True)
            else:
                (project / "report.txt").write_text("human revision")
        return snapshot(path, **kwargs)

    monkeypatch.setattr(supervisor, "snapshot", concurrent_change)
    with pytest.raises(ValueError):
        runner.verify(run)
    failure = runtime.store.records("verification_failed")[-1][1]
    assert failure["phase"] == "host_attestation"
    assert failure["attempt_id"] == runtime.store.records("verification_started")[-1][0]
    assert len(runtime.store.records("completion")) == 1
    assert not runtime.complete(proposal)


def test_historical_publication_needs_new_explicit_attestation(published, tmp_path):
    runtime, runner, run, proposal, _ = published
    events = deepcopy(runtime.store.events())
    changes = next(e["payload"]["changes"] for e in events if e["kind"] == "publication")
    for event in events:
        if event["kind"] == "publication_started":
            event["payload"].pop("verification_id")
            event["payload"]["changes"] = changes
        if event["kind"] == "publication":
            for key in ("format", "attempt_id", "verification_id"):
                event["payload"].pop(key)
        if event["kind"] == "completion":
            event["payload"].pop("format")
    store = replay(tmp_path, events)
    try:
        historical = Runtime(store, FakeJudge())
        assert historical.completion_status(proposal)["reason"] == "fresh_verification_required"
        Supervisor(historical, runner.directory, confirm=lambda _: True).verify(run)
        assert historical.complete(proposal)
        assert store.records("completion")[-1][1]["mode"] == "historical_reverification"
        assert len(store.records("publication")) == 1
    finally:
        store.close()


def test_status_shows_failed_attempts_without_loading_providers(tmp_path, capsys):
    state = tmp_path / "events.sqlite"
    kinds = [
        "verification_started",
        "verification_failed",
        "publication_started",
        "publication_failed",
        "completion",
    ]
    store = Store(state)
    store.append_many([(kind, {"run_id": "run"}) for kind in kinds])
    store.close()
    assert main(["--state", str(state), "--config", str(tmp_path / "absent.toml"), "status"]) == 0
    output = capsys.readouterr().out
    assert all(kind in output for kind in kinds)


@pytest.mark.parametrize("error_type", [OSError, WorkspaceError, WorkspaceChanged])
def test_publication_host_snapshot_failure_retains_applied_paths(staged, monkeypatch, error_type):
    import jev_runtime.supervisor as supervisor

    runtime, runner, run, proposal, project = staged
    snapshot = supervisor.snapshot
    host_reads = 0

    def snapshot_error(path, **kwargs):
        nonlocal host_reads
        if Path(path) == project:
            host_reads += 1
            if host_reads == 2:
                raise error_type("Injected failure after files were applied")
        return snapshot(path, **kwargs)

    monkeypatch.setattr(supervisor, "snapshot", snapshot_error)
    with pytest.raises(error_type):
        runner.publish(run)
    failure = runtime.store.records("publication_failed")[-1][1]
    assert failure["phase"] == "host_attestation"
    assert failure["applied"]
    assert failure["attempt_id"] == runtime.store.records("publication_started")[-1][0]
    assert (project / "report.txt").read_text() == "verified"
    assert runtime.autonomy(proposal)["state"] == "QUARANTINED"
    assert not runtime.store.records("completion")
    assert not runtime.complete(proposal)


def test_preflight_host_drift_is_visible_without_host_writes(staged):
    runtime, runner, run, proposal, project = staged
    (project / "human.txt").write_text("preserve me")
    with pytest.raises(ValueError, match="Project changed"):
        runner.publish(run)
    failure = runtime.store.records("publication_failed")[-1][1]
    assert failure["phase"] == "preflight" and failure["applied"] == []
    assert (project / "human.txt").read_text() == "preserve me"
    assert not (project / "report.txt").exists()
    assert not runtime.complete(proposal)
