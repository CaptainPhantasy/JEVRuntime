"""Inspect interrupted publications and close attempts without claiming success."""

from pathlib import Path

from .contracts import digest
from .workspace import WorkspaceError, snapshot


def _legacy_success(start, publication, events):
    """Recognize only an unambiguous pre-attempt-ID publication, never certify it."""
    began, ended = start["payload"], publication["payload"]
    if (
        "verification_id" in began
        or not isinstance(began.get("changes"), list)
        or "format" in ended
        or "attempt_id" in ended
        or ended.get("run_id") != began.get("run_id")
        or ended.get("proposal_id") != began.get("proposal_id")
        or ended.get("changes") != began["changes"]
    ):
        return False
    starts = [
        event
        for event in events
        if event["kind"] == "publication_started"
        and event["payload"].get("run_id") == began["run_id"]
        and event["seq"] < publication["seq"]
    ]
    results = [
        event["payload"]
        for event in events
        if event["kind"] == "runtime_result"
        and event["payload"].get("run_id") == began["run_id"]
        and event["seq"] < start["seq"]
    ]
    tree_hash = ended.get("tree_hash")
    return (
        isinstance(tree_hash, str)
        and len(tree_hash) == 64
        and all(character in "0123456789abcdef" for character in tree_hash)
        and len(starts) == 1
        and starts[0]["id"] == start["id"]
        and bool(results)
        and results[-1].get("success") is True
        and results[-1].get("proposal_id") == began.get("proposal_id")
        and ended.get("tree_hash") == results[-1].get("tree_hash")
        and not any(
            event["kind"] == "publication_failed"
            and event["payload"].get("run_id") == began["run_id"]
            and start["seq"] < event["seq"] < publication["seq"]
            for event in events
        )
    )


def affected_constitutions(runtime, run_id):
    run = runtime.store.get(run_id, "runtime_session")
    origin = run["proposal_id"]
    cid = runtime.constitution_id(origin)
    workspace = Path(runtime.store.get(origin, "policy_proposal")["policy"]["workspace"]).resolve()
    affected = {cid: origin}
    for proposal, value in runtime.store.records("policy_proposal"):
        candidate = runtime.constitution_id(proposal)
        if candidate == cid or Path(value["policy"]["workspace"]).resolve() == workspace:
            if runtime._active(proposal)[1]:
                affected[candidate] = proposal
    return [
        {"constitution_id": key, "proposal_id": value} for key, value in sorted(affected.items())
    ]


def unresolved_publications(runtime, *, proposal_id=None, run_id=None):
    events = runtime.store.events()
    by_id = {event["id"]: event for event in events}
    cid = runtime.constitution_id(proposal_id) if proposal_id else None
    workspace = (
        Path(runtime.store.get(proposal_id, "policy_proposal")["policy"]["workspace"]).resolve()
        if proposal_id
        else None
    )
    unresolved = []
    for event in events:
        if event["kind"] != "publication_started":
            continue
        started = event["payload"]
        if run_id is not None and started.get("run_id") != run_id:
            continue
        if proposal_id:
            saved_run = by_id.get(started.get("run_id"), {}).get("payload", {})
            owner = started.get("proposal_id") or saved_run.get("proposal_id")
            if not owner:
                continue
            saved_policy = runtime.store.get(owner, "policy_proposal")["policy"]
            if (
                runtime.constitution_id(owner) != cid
                and Path(saved_policy["workspace"]).resolve() != workspace
            ):
                continue
        resolved = any(
            later["seq"] > event["seq"]
            and (
                (
                    later["kind"] == "publication"
                    and later["payload"].get("run_id") == started.get("run_id")
                    and (
                        later["payload"].get("attempt_id") == event["id"]
                        or _legacy_success(event, later, events)
                    )
                )
                or (
                    later["kind"] == "publication_failed"
                    and later["payload"].get("attempt_id") == event["id"]
                    and later["payload"].get("phase") == "preflight"
                )
                or (
                    later["kind"] == "publication_abandoned"
                    and later["payload"].get("run_id") == started.get("run_id")
                    and event["id"] in later["payload"].get("attempt_ids", [])
                    and later["payload"].get("human_approved") is True
                )
            )
            for later in events
        )
        if not resolved:
            unresolved.append(event)
    return unresolved


def is_abandoned(store, run_id):
    return any(p.get("run_id") == run_id for _, p in store.records("publication_abandoned"))


def publication_report(runtime, run_id):
    """Read files and recorded receipts only; no inference, lock creation or writes."""
    events = runtime.store.events()
    run = runtime.store.get(run_id, "runtime_session")
    policy = runtime.store.get(run["proposal_id"], "policy_proposal")["policy"]
    pending = unresolved_publications(runtime, run_id=run_id)
    result = {
        "run_id": run_id,
        "proposal_id": run["proposal_id"],
        "workspace": policy["workspace"],
        "attempt_ids": [event["id"] for event in pending],
        "abandoned": is_abandoned(runtime.store, run_id),
        "completion_established": False,
        "baseline_hash": digest(run["baseline"]),
        "host_hash": None,
        "target_hash": None,
        "host_state": "unreadable",
        "staged_state": "unavailable",
        "unresolved": bool(pending),
        "ledger_head": events[-1]["id"],
        "affected_constitutions": affected_constitutions(runtime, run_id),
    }
    receipts = [p for _, p in runtime.store.records("runtime_result") if p.get("run_id") == run_id]
    successful = receipts and receipts[-1].get("success") is True
    if successful:
        result["target_hash"] = receipts[-1].get("tree_hash")
    # The target hash is recorded worker evidence, not proof of how host files changed.
    try:
        host = snapshot(Path(policy["workspace"]), protected_paths=run["protected_paths"])
        result["host_hash"] = digest(host)
        result["host_state"] = (
            "baseline"
            if host == run["baseline"]
            else "target"
            if result["target_hash"] == digest(host)
            else "divergent"
        )
        result["changed_paths"] = [
            path
            for path in sorted(set(host) | set(run["baseline"]))
            if host.get(path) != run["baseline"].get(path)
        ]
    except (OSError, ValueError, WorkspaceError) as exc:
        result["host_error"] = type(exc).__name__
    try:
        staged = snapshot(
            Path(run["directory"]) / "workspace", protected_paths=run["protected_paths"]
        )
        result["staged_state"] = (
            "matches_recorded_result"
            if successful and digest(staged) == result["target_hash"]
            else "changed_or_unverified"
        )
    except (OSError, ValueError, WorkspaceError) as exc:
        result["staged_error"] = type(exc).__name__
    result["next_step"] = (
        "Attempt closed as abandoned. Repair the project if needed; recovery requires fresh acceptance checks and human approval. Start a new worker for a new completion record."
        if result["abandoned"]
        else "An attempt is active or interrupted. If no publisher is running, review these files and explicitly abandon the attempt. This never restores or overwrites files, certifies completion, or restores authority."
        if pending
        else "No unresolved publication attempt for this run. Use complete to inspect its independent completion evidence."
    )
    if runtime.store.events()[-1]["id"] != result["ledger_head"]:
        raise ValueError("Evidence changed during publication inspection; retry")
    return result
