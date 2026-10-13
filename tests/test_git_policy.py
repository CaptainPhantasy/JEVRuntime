"""Real disposable Git repositories; no providers or remote network operations."""

import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from jev_runtime.contracts import Decision, digest
from jev_runtime.git_gateway import GitGateway, validate_policy_plan
from jev_runtime.judgment import FakeJudge
from jev_runtime.runtime import Runtime
from jev_runtime.store import Store


def git(root, *args):
    return (
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=root,
            env={
                "PATH": os.defpath,
                "HOME": str(root),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "remote", "add", "origin", "https://example.invalid/private.git")
    return root


def stage(root, name, text="Synthetic fixture text\n"):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git(root, "add", "-f", "--", name)


def runtime_for(root, tmp_path, protected=("protected.txt",)):
    store = Store(tmp_path / "ledger.sqlite")
    runtime = Runtime(store, FakeJudge())
    intent = store.record_intent(
        "Commit and push permitted project changes; do not include protected paths."
    )
    proposal = runtime.propose(
        {
            "intent_id": intent,
            "workspace": str(root),
            "actions": ["read", "git", "push"],
            "protected_paths": list(protected),
            "required_evidence": ["report"],
            "checks": [{"kind": "file", "requirement": "report", "path": "report.txt"}],
        }
    )
    runtime.approve(proposal)
    policy, _ = runtime._active(proposal)
    gateway = GitGateway(root, protected_paths=protected, policy_hash=digest(policy.as_dict()))
    return runtime, store, proposal, policy, gateway


def resign(plan):
    plan["plan_id"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in plan.items() if k != "plan_id"}, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return plan


@pytest.mark.parametrize(
    "name", ["protected.txt", "nested/PROTECTED.TXT", ".env", "nested/.ENV.token"]
)
def test_protected_index_is_rejected_before_patch_even_if_worktree_missing(
    repository, name, monkeypatch
):
    stage(repository, name)
    (repository / name).unlink()
    gateway = GitGateway(repository, protected_paths=("protected.txt",))
    original = gateway._run
    calls = []

    def observe(args, **kwargs):
        calls.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(gateway, "_run", observe)
    with pytest.raises(ValueError, match="protected"):
        gateway.preview("commit", message="Must not commit")
    assert not any(a[0] == "diff" for a in calls)
    assert git(repository, "rev-list", "--all") == ""


def test_status_does_not_open_protected_worktree_bytes(repository, monkeypatch):
    stage(repository, "protected.txt")
    original = os.open

    def observe(path, *args, **kwargs):
        if isinstance(path, (str, Path)) and Path(path).name == "protected.txt":
            raise AssertionError("Protected bytes were opened")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", observe)
    plan = GitGateway(repository, protected_paths=("protected.txt",)).preview("status")
    assert plan["state"]["excluded_worktree_paths"] == ["protected.txt"]


@pytest.mark.parametrize("rename", [False, True])
def test_protected_deletion_or_rename_cannot_disclose_old_content(repository, rename, monkeypatch):
    stage(repository, "protected.txt")
    git(repository, "commit", "-m", "Existing fixture")
    git(
        repository,
        *(("mv", "protected.txt", "ordinary.txt") if rename else ("rm", "protected.txt")),
    )
    gateway = GitGateway(repository, protected_paths=("protected.txt",))
    original = gateway._run
    calls = []

    def observe(args, **kwargs):
        calls.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(gateway, "_run", observe)
    with pytest.raises(ValueError, match="protected"):
        gateway.preview("commit", message="Denied")
    assert not any(a[0] == "diff" and "--name-only" not in a for a in calls)


def test_approved_commit_uses_exact_staged_content_and_consumes_once(repository, tmp_path):
    stage(repository, "result.txt", "Reviewed staged content\n")
    (repository / "result.txt").write_text("Unstaged content stays outside commit\n")
    runtime, store, proposal, policy, gateway = runtime_for(repository, tmp_path)
    try:
        plan = gateway.preview("commit", message="Reviewed result")
        assert validate_policy_plan(policy, "git", plan)
        assert "Reviewed staged content" in plan["git_review"]["patch"]
        assert "Unstaged content" not in plan["git_review"]["patch"]
        request = runtime.request_operation(proposal, "git", plan)
        assert store.get(request, "operation_request")["decision"] == Decision.APPROVAL_REQUIRED
        runtime.consume_operation(request, human_approved=True)
        result = gateway.execute(plan)
        assert git(repository, "rev-parse", "HEAD") == result["commit"]
        assert git(repository, "show", "HEAD:result.txt") == "Reviewed staged content"
        assert "Unstaged content" in (repository / "result.txt").read_text()
        with pytest.raises(ValueError, match="already used"):
            runtime.consume_operation(request, human_approved=True)
    finally:
        store.close()


@pytest.mark.parametrize("change", ["legacy", "protected", "other_policy", "missing_patch"])
def test_unsafe_plan_is_denied_before_judgment_and_human_cannot_override(
    repository, tmp_path, change
):
    stage(repository, "result.txt")
    runtime, store, proposal, policy, gateway = runtime_for(repository, tmp_path)
    try:
        plan = gateway.preview("commit", message="Result")
        if change == "legacy":
            plan["version"] = 1
        elif change == "protected":
            plan["git_review"]["paths"].append("protected.txt")
        elif change == "other_policy":
            plan["policy_hash"] = "0" * 64
        else:
            del plan["git_review"]["patch"]
        resign(plan)
        judgments = len(store.records("judgment"))
        request = runtime.request_operation(proposal, "git", plan)
        assert store.get(request, "operation_request")["decision"] == Decision.DENY
        assert len(store.records("judgment")) == judgments
        with pytest.raises(ValueError, match="deterministic policy review"):
            runtime.consume_operation(request, human_approved=True)
        assert not store.records("human_operation_approval")
    finally:
        store.close()


def test_changed_index_requires_new_review(repository):
    stage(repository, "result.txt")
    gateway = GitGateway(repository)
    plan = gateway.preview("commit", message="Result")
    stage(repository, "second.txt")
    with pytest.raises(ValueError, match="changed"):
        gateway.execute(plan)
    assert git(repository, "rev-list", "--all") == ""


@pytest.mark.parametrize("content", ["\x00binary", "A" * 17000, "invalid\x1b[2J"])
def test_commit_rejects_unreviewable_content(repository, content):
    stage(repository, "result.txt", content)
    with pytest.raises(ValueError):
        GitGateway(repository).preview("commit", message="Denied")


def test_commit_rejects_symlink(repository):
    (repository / "link").symlink_to("ordinary.txt")
    git(repository, "add", "link")
    with pytest.raises(ValueError, match="symlinks"):
        GitGateway(repository).preview("commit", message="Denied")


@pytest.mark.parametrize("same_blob", [False, True])
def test_push_checks_deleted_protected_ancestors_and_aliases_before_blob_read(
    repository, same_blob, monkeypatch
):
    stage(repository, "protected.txt", "same content\n")
    stage(repository, "ordinary.txt", "same content\n" if same_blob else "different content\n")
    git(repository, "commit", "-m", "Earlier protected fixture")
    git(repository, "rm", "protected.txt")
    git(repository, "commit", "-m", "Removed fixture")
    gateway = GitGateway(repository, protected_paths=("protected.txt",))
    original = gateway._run
    calls = []

    def observe(args, **kwargs):
        calls.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(gateway, "_run", observe)
    with pytest.raises(ValueError, match="protected"):
        gateway.preview("push", remote="origin", branch="main")
    assert not any(a[0] in {"push", "cat-file"} for a in calls)


def test_complete_push_preview_includes_old_and_current_text(repository, tmp_path):
    stage(repository, "ordinary.txt", "original\n")
    git(repository, "commit", "-m", "First")
    stage(repository, "ordinary.txt", "current\n")
    git(repository, "commit", "-m", "Second")
    runtime, store, proposal, policy, gateway = runtime_for(repository, tmp_path)
    try:
        plan = gateway.preview("push", remote="origin", branch="main")
        assert validate_policy_plan(policy, "push", plan)
        assert plan["git_review"]["content_reviewed"] is True
        assert {b["text"] for b in plan["git_review"]["blobs"]} == {"original\n", "current\n"}
        assert len(plan["git_review"]["commits"]) == 2
        request = runtime.request_operation(proposal, "push", plan)
        assert store.get(request, "operation_request")["decision"] == Decision.APPROVAL_REQUIRED
        damaged = copy.deepcopy(plan)
        damaged["git_review"]["content_reviewed"] = False
        assert not validate_policy_plan(policy, "push", resign(damaged))
    finally:
        store.close()


@pytest.mark.parametrize("content", ["\0binary", "A" * 17000])
def test_push_rejects_binary_or_excessive_historical_content(repository, content):
    stage(repository, "ordinary.txt", content)
    git(repository, "commit", "-m", "Existing fixture")
    with pytest.raises(ValueError, match="content|binary"):
        GitGateway(repository).preview("push", remote="origin", branch="main")


def test_push_rejects_excessive_history(repository, monkeypatch):
    import jev_runtime.git_gateway as module

    monkeypatch.setattr(module, "MAX_HISTORY_COMMITS", 1)
    stage(repository, "ordinary.txt")
    git(repository, "commit", "-m", "First")
    stage(repository, "ordinary.txt", "second\n")
    git(repository, "commit", "-m", "Second")
    with pytest.raises(ValueError, match="complete review limit"):
        GitGateway(repository).preview("push", remote="origin", branch="main")


def test_push_rejects_shallow_history(repository):
    stage(repository, "ordinary.txt")
    git(repository, "commit", "-m", "First")
    (repository / ".git/shallow").write_text(git(repository, "rev-parse", "HEAD") + "\n")
    with pytest.raises(ValueError, match="shallow"):
        GitGateway(repository).preview("push", remote="origin", branch="main")


def test_push_rejects_excessive_commit_metadata(repository):
    stage(repository, "ordinary.txt")
    git(repository, "commit", "-m", "X" * 17000)
    with pytest.raises(ValueError, match="content-review"):
        GitGateway(repository).preview("push", remote="origin", branch="main")


def test_graft_cannot_hide_protected_ancestor(repository):
    stage(repository, "protected.txt")
    git(repository, "commit", "-m", "Protected ancestor")
    git(repository, "rm", "protected.txt")
    stage(repository, "ordinary.txt")
    git(repository, "commit", "-m", "Removed fixture")
    (repository / ".git/info/grafts").write_text(git(repository, "rev-parse", "HEAD") + "\n")
    with pytest.raises(ValueError, match="protected"):
        GitGateway(repository, protected_paths=("protected.txt",)).preview(
            "push", remote="origin", branch="main"
        )


def test_hard_link_to_protected_bytes_is_rejected_before_review(repository, monkeypatch):
    (repository / "protected.txt").write_text("Synthetic protected bytes\n")
    os.link(repository / "protected.txt", repository / "ordinary.txt")
    git(repository, "add", "ordinary.txt")
    gateway = GitGateway(repository, protected_paths=("protected.txt",))
    original = gateway._run
    calls = []

    def observe(args, **kwargs):
        calls.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(gateway, "_run", observe)
    with pytest.raises(ValueError, match="Hard-linked"):
        gateway.preview("commit", message="Denied")
    assert not any(a[0] == "diff" or "status" in a for a in calls)


def test_git_status_subprocess_is_scoped_to_permitted_literal_paths(repository, monkeypatch):
    stage(repository, "protected.txt")
    stage(repository, "ordinary.txt")
    (repository / "protected.txt").write_text("changed private fixture\n")
    gateway = GitGateway(repository, protected_paths=("protected.txt",))
    original = gateway._run
    calls = []

    def observe(args, **kwargs):
        calls.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(gateway, "_run", observe)
    plan = gateway.preview("status")
    status_calls = [a for a in calls if "status" in a]
    assert len(status_calls) == 1
    assert status_calls[0][0] == "--literal-pathspecs"
    assert status_calls[0][status_calls[0].index("--") + 1 :] == ["ordinary.txt"]
    assert "protected.txt" not in plan["state"]["status"]


@pytest.mark.parametrize(
    "damage", ["protected_alias", "missing_alias", "bad_object", "metadata_control"]
)
def test_malformed_push_review_is_denied_before_judgment(repository, tmp_path, damage):
    stage(repository, "ordinary.txt")
    git(repository, "commit", "-m", "Reviewed fixture")
    runtime, store, proposal, policy, gateway = runtime_for(repository, tmp_path)
    try:
        plan = gateway.preview("push", remote="origin", branch="main")
        if damage == "protected_alias":
            plan["git_review"]["blobs"][0]["paths"].append("protected.txt")
        elif damage == "missing_alias":
            plan["git_review"]["blobs"][0]["paths"] = []
        elif damage == "bad_object":
            plan["git_review"]["blobs"][0]["object"] = "x" * 40
        else:
            plan["git_review"]["commits"][0]["metadata"] += "\0"
        resign(plan)
        judgments = len(store.records("judgment"))
        request = runtime.request_operation(proposal, "push", plan)
        assert store.get(request, "operation_request")["decision"] == Decision.DENY
        assert len(store.records("judgment")) == judgments
    finally:
        store.close()
