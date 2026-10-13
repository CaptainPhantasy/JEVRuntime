import sqlite3

import pytest

from jev_runtime.contracts import Decision
from jev_runtime.judgment import FakeJudge
from jev_runtime.runtime import Runtime
from jev_runtime.store import Store


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "ledger.sqlite")
    root = tmp_path / "project"
    root.mkdir()
    runtime = Runtime(store, FakeJudge())
    intent = store.record_intent("  Read and write this project. Produce a report.\n")
    raw = dict(
        intent_id=intent,
        workspace=str(root),
        actions=["read", "write"],
        required_evidence=["A report satisfying the request"],
        checks=[
            {"requirement": "A report satisfying the request", "kind": "file", "path": "report"}
        ],
    )
    proposal = runtime.propose(raw)
    yield runtime, store, root, raw, proposal
    store.close()


def test_jev_cannot_authorize(setup):
    runtime, store, root, raw, proposal = setup
    assert runtime.decide(proposal, "write", "report") == Decision.APPROVAL_REQUIRED
    assert not store.records("human_approval")
    runtime.approve(proposal)
    assert runtime.decide(proposal, "write", "report") == Decision.ALLOW
    assert runtime.decide(proposal, "deploy", "report") == Decision.DENY


@pytest.mark.parametrize(
    "bad",
    [
        FakeJudge(failure=True),
        FakeJudge(0.5),
        FakeJudge(-1),
        FakeJudge(2),
        FakeJudge(float("nan")),
        FakeJudge(True),
    ],
)
def test_failure_cannot_expand_authority(setup, bad):
    runtime, store, root, raw, proposal = setup
    runtime.approve(proposal)
    runtime.judge = bad
    assert runtime.decide(proposal, "write", "report") == Decision.DENY
    failed = runtime.propose(raw)
    with pytest.raises(ValueError):
        runtime.approve(failed)


def test_collected_file_evidence_cannot_substitute_for_verified_publication(setup):
    runtime, store, root, raw, proposal = setup
    runtime.approve(proposal)
    store.append("agent_claim", {"proposal_id": proposal, "text": "done"})
    assert not runtime.complete(proposal)
    (root / "report").write_text("Observed result")
    runtime.collect_file(proposal, raw["required_evidence"][0], "report")
    assert not runtime.complete(proposal)
    (root / "report").write_text("Changed after observation")
    assert not runtime.complete(proposal)
    # Identical bytes cannot rehabilitate a now-protected path alias.
    (root / ".env").write_text("Observed result")
    (root / "report").unlink()
    (root / "report").symlink_to(root / ".env")
    assert not runtime.complete(proposal)


def test_empty_requirements_and_wrong_policy_evidence_fail(setup):
    runtime, store, root, raw, proposal = setup
    runtime.approve(proposal)
    (root / "report").write_text("result")
    runtime.collect_file(proposal, raw["required_evidence"][0], "report")
    other = runtime.propose(raw)
    runtime.approve(other)
    assert not runtime.complete(other)
    with pytest.raises(ValueError, match="acceptance"):
        runtime.propose({**raw, "required_evidence": [], "checks": []})


@pytest.mark.parametrize("target", ["../outside", "/etc/passwd", ".git/config", ".env", ".jev/key"])
def test_deterministic_checks_override_judgment(setup, target):
    runtime, store, root, raw, proposal = setup
    runtime.approve(proposal)
    assert runtime.decide(proposal, "read", target) == Decision.DENY


def test_symlink_escape(setup, tmp_path):
    runtime, store, root, raw, proposal = setup
    runtime.approve(proposal)
    (root / "escape").symlink_to(tmp_path)
    assert runtime.decide(proposal, "read", "escape/secret") == Decision.DENY


def test_intent_distinct_from_compilation_and_immutable(setup):
    runtime, store, root, raw, proposal = setup
    original = store.get(raw["intent_id"], "human_intent")
    runtime.propose({**raw, "actions": ["read"]})
    assert store.get(raw["intent_id"], "human_intent") == original
    assert original["text"] == "  Read and write this project. Produce a report.\n"
    for statement in ["UPDATE events SET body='{}'", "DELETE FROM events"]:
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute(statement)
        store.db.rollback()


def test_restart_preserves_intent(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path)
    identifier = store.record_intent("Human text")
    store.close()
    store = Store(path)
    assert store.get(identifier, "human_intent")["text"] == "Human text"
    store.close()


def test_failed_audit_write_never_returns_allow(setup, monkeypatch):
    runtime, store, root, raw, proposal = setup
    runtime.approve(proposal)

    def broken(*args):
        raise OSError("disk full")

    monkeypatch.setattr(store, "append", broken)
    with pytest.raises(OSError):
        runtime.decide(proposal, "read", "report")


def test_wrong_provenance_and_extra_grants_rejected(setup):
    runtime, store, root, raw, proposal = setup
    with pytest.raises(ValueError):
        runtime.propose({**raw, "authorized": True})
    with pytest.raises(ValueError):
        runtime.propose({**raw, "intent_id": proposal})


@pytest.mark.parametrize(
    "target",
    [".env", "private/report.txt", "node_modules/report.txt", "directory", "alias", "hardlink"],
)
def test_impossible_acceptance_file_rejected_before_jev(setup, target):
    runtime, store, root, raw, _ = setup
    (root / "directory").mkdir()
    (root / "real.txt").write_text("reference")
    (root / "alias").symlink_to(root / "real.txt")
    (root / "hardlink").hardlink_to(root / "real.txt")
    calls_before = len(store.records("judgment"))
    with pytest.raises(ValueError, match="Deterministic Constitution validation failed"):
        runtime.propose(
            {
                **raw,
                "protected_paths": ["private"],
                "checks": [
                    {"requirement": raw["required_evidence"][0], "kind": "file", "path": target}
                ],
            }
        )
    assert len(store.records("judgment")) == calls_before
    assert store.records("policy_validation")[-1][1]["passed"] is False


def test_safe_future_acceptance_artifact_can_be_proposed(setup):
    runtime, store, root, raw, _ = setup
    proposed = runtime.propose(
        {
            **raw,
            "checks": [
                {
                    "requirement": raw["required_evidence"][0],
                    "kind": "file",
                    "path": "future/report.txt",
                }
            ],
        }
    )
    assert store.get(proposed, "policy_proposal")["semantic_pass"]
    assert not (root / "future").exists()
    assert store.records("policy_validation")[-1][1]["acceptance_paths"][0]["passed"]


@pytest.mark.parametrize("revision", ["operator", "human"])
def test_pending_revision_blocks_stale_activation_and_consumption(setup, revision):
    from jev_runtime.onboarding import FakeOnboarding, OnboardingSession

    runtime, store, root, _, _ = setup
    session = OnboardingSession.start(store, runtime, FakeOnboarding(), root)
    proposal = session.reply("Read this project and check README.md exists.")["proposal_id"]
    runtime.approve(proposal)
    request = runtime.request_operation(proposal, "read", {"path": "README.md"})
    # Simulate an interruption before suspension, followed by a late activation
    # from an approval that had already inspected the older input snapshot.
    if revision == "operator":
        store.append(
            "operator_context",
            {"session_id": session.id, "text": "Reconsider the job", "authoritative": False},
        )
    else:
        intent = store.record_intent("Stop; I am revising the scope.")
        store.append("onboarding_input", {"session_id": session.id, "intent_id": intent})
    store.append("policy_activation", {"constitution_id": session.id, "proposal_id": proposal})
    assert not runtime._active(proposal)[1]
    assert runtime.current(session.id) is None
    with pytest.raises(ValueError):
        runtime.consume_operation(request, human_approved=True)
    with pytest.raises(ValueError, match="superseded"):
        runtime.approve(proposal)


def test_unsupported_capability_fails_before_judgment_and_legacy_activation(setup):
    from jev_runtime.contracts import Policy, digest

    runtime, store, root, raw, _ = setup
    unsupported = {**raw, "actions": ["read", "deploy"]}
    before = len(store.records("judgment"))
    with pytest.raises(ValueError, match="Unsupported runtime capabilities"):
        runtime.propose(unsupported)
    validation = store.records("policy_validation")[-1][1]
    assert validation["unsupported_actions"] == ["deploy"]
    assert validation["passed"] is False
    assert len(store.records("judgment")) == before

    # Historical records remain readable; their recorded semantic pass cannot
    # activate a capability for which the runtime has no executor.
    policy = Policy.parse(unsupported).as_dict()
    legacy = store.append("policy_proposal", {"policy": policy, "semantic_pass": True})
    with pytest.raises(ValueError, match="Unsupported capabilities"):
        runtime.approve(legacy)
    store.append("human_approval", {"proposal_id": legacy, "policy_hash": digest(policy)})
    store.append("policy_activation", {"constitution_id": legacy, "proposal_id": legacy})
    assert not runtime._active(legacy)[1]
    assert runtime.current(legacy) is None
    assert runtime.decide(legacy, "read", ".") == Decision.DENY
    assert store.records("decision")[-1][1]["reason"] == "unsupported_policy_requires_revision"
    read_request = runtime.request_operation(legacy, "read", {"path": "."})
    assert store.get(read_request, "operation_request")["decision"] == Decision.DENY
    assert (
        store.get(read_request, "operation_request")["reason"]
        == "unsupported_policy_requires_revision"
    )
    assert runtime.decide(legacy, "deploy", ".") == Decision.DENY
    request = runtime.request_operation(legacy, "deploy", {"target": "production"})
    assert store.get(request, "operation_request")["decision"] == Decision.DENY
    old_request = store.append(
        "operation_request",
        {
            "proposal_id": legacy,
            "action": "deploy",
            "decision": "approval_required",
            "payload": {"target": "production"},
        },
    )
    with pytest.raises(ValueError, match="Unsupported runtime capability"):
        runtime.consume_operation(old_request, human_approved=True)
    assert not store.records("operation_consumed")
    assert len(store.records("judgment")) == before


def test_legacy_evidence_cannot_complete_or_reactivate_without_typed_checks(setup, tmp_path):
    from jev_runtime.constitutions import export_policy
    from jev_runtime.contracts import Policy, digest

    runtime, store, root, raw, _ = setup
    legacy_policy = Policy.parse({**raw, "checks": []}).as_dict()
    legacy = store.append("policy_proposal", {"policy": legacy_policy, "semantic_pass": True})
    store.append("human_approval", {"proposal_id": legacy, "policy_hash": digest(legacy_policy)})
    store.append("policy_activation", {"constitution_id": legacy, "proposal_id": legacy})
    (root / "report").write_text("All tests passed, says the worker")
    store.append(
        "file_evidence",
        {
            "proposal_id": legacy,
            "requirement": raw["required_evidence"][0],
            "path": str(root / "report"),
        },
    )
    store.append("completion", {"proposal_id": legacy, "passed": True})
    before = len(store.records("judgment"))
    assert not runtime.complete(legacy)
    assert runtime.completion_status(legacy)["reason"] == "acceptance_checks_require_revision"
    assert runtime.current(legacy) is None
    assert runtime.decide(legacy, "read", "report") == Decision.DENY
    with pytest.raises(ValueError, match="acceptance checks"):
        runtime.approve(legacy)
    with pytest.raises(ValueError, match="typed checks"):
        runtime.propose(legacy_policy)
    assert store.records("policy_validation")[-1][1]["acceptance_complete"] is False
    assert len(store.records("judgment")) == before
    assert store.get(legacy, "policy_proposal")["policy"] == legacy_policy
    destination = tmp_path / "legacy-export.json"
    export_policy(runtime, legacy, destination)
    assert destination.exists()
    assert (
        store.get(raw["intent_id"], "human_intent")["text"]
        == "  Read and write this project. Produce a report.\n"
    )
