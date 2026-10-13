"""Durable host Git attempts; local inspection never infers a remote outcome."""

import fcntl
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

from .contracts import canonical, digest
from .destinations import member_directory
from .git_gateway import GitGateway

ATTEMPT_FORMAT = "jevruntime.git-attempt.v1"
RECEIPT_FORMAT = "jevruntime.git-receipt.v1"


def _success(request, receipt):
    plan = request["payload"]
    result = receipt.get("result", {})
    operation = "commit" if request["action"] == "git" else "push"
    return (
        plan.get("operation") == operation
        and isinstance(plan.get("plan_id"), str)
        and re.fullmatch(r"[0-9a-f]{64}", plan["plan_id"]) is not None
        and plan["plan_id"]
        == hashlib.sha256(
            json.dumps(
                {k: v for k, v in plan.items() if k != "plan_id"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        and receipt.get("proposal_id") == request["proposal_id"]
        and result.get("operation") == operation
        and result.get("plan_id") == plan.get("plan_id")
        and isinstance(result.get("commit"), str)
        and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", result["commit"]) is not None
        and (
            operation == "commit"
            or (
                result["commit"] == plan["state"]["head"]
                and result.get("remote_url") == plan["remote_url"]
                and result.get("target_ref") == plan["target_ref"]
            )
        )
    )


def git_attempts(runtime):
    """Consumed legacy Git grants without an exact success remain uncertain too."""
    events = runtime.store.events()
    by_id = {e["id"]: e for e in events}
    attempts = []
    for consumed in events:
        if consumed["kind"] != "operation_consumed":
            continue
        request_id = consumed["payload"].get("request_id")
        request_event = by_id.get(request_id, {})
        request = request_event.get("payload", {})
        if request.get("action") not in {"git", "push"}:
            continue
        if (
            request_event.get("kind") != "operation_request"
            or request_event["seq"] >= consumed["seq"]
            or consumed["payload"].get("request_hash") != digest(request)
        ):
            raise ValueError("Invalid consumed Git provenance")
        starts = [
            e
            for e in events
            if e["kind"] == "git_attempt_started" and e["payload"].get("request_id") == request_id
        ]
        if len(starts) > 1:
            raise ValueError("Ambiguous Git attempt provenance")
        start = starts[0] if starts else consumed
        if starts and (
            start["seq"] <= consumed["seq"]
            or start["payload"].get("format") != ATTEMPT_FORMAT
            or start["payload"].get("request_hash") != digest(request)
            or start["payload"].get("proposal_id") != request["proposal_id"]
            or start["payload"].get("plan_id") != request["payload"]["plan_id"]
        ):
            raise ValueError("Invalid Git attempt provenance")
        approvals = [
            e
            for e in events
            if e["kind"] == "human_operation_approval"
            and e["payload"].get("request_id") == request_id
            and request_event["seq"] < e["seq"] < consumed["seq"]
            and e["payload"].get("request_hash") == digest(request)
        ]
        approved = request.get("decision") == "approval_required" and len(approvals) == 1
        repeated = (
            sum(
                e["kind"] == "operation_consumed" and e["payload"].get("request_id") == request_id
                for e in events
            )
            != 1
        )
        successes = [
            e
            for e in events
            if e["kind"] == "git_receipt"
            and e["payload"].get("request_id") == request_id
            and e["seq"] > start["seq"]
            and _success(request, e["payload"])
            and (
                not starts
                or (
                    e["payload"].get("format") == RECEIPT_FORMAT
                    and e["payload"].get("attempt_id") == start["id"]
                )
            )
        ]
        abandoned = any(
            e["kind"] == "git_abandoned"
            and e["seq"] > start["seq"]
            and e["payload"].get("attempt_id") == start["id"]
            and e["payload"].get("human_approved") is True
            for e in events
        )
        attempts.append(
            {
                "attempt_id": start["id"],
                "request_id": request_id,
                "request": request,
                "legacy": not bool(starts),
                "unresolved": not abandoned and (not approved or repeated or len(successes) != 1),
                "abandoned": abandoned,
                "receipt_id": successes[0]["id"]
                if len(successes) == 1 and not repeated and approved
                else None,
            }
        )
    return attempts


def unresolved_git(runtime, proposal_id=None):
    pending = [a for a in git_attempts(runtime) if a["unresolved"]]
    if proposal_id is None:
        return pending
    policy = runtime.store.get(proposal_id, "policy_proposal")["policy"]
    cid = runtime.constitution_id(proposal_id)
    workspace = Path(policy["workspace"]).resolve()
    return [
        a
        for a in pending
        if runtime.constitution_id(a["request"]["proposal_id"]) == cid
        or Path(a["request"]["payload"]["root"]).resolve() == workspace
    ]


def _attempt(runtime, attempt_id):
    return (
        next((a for a in git_attempts(runtime) if a["attempt_id"] == attempt_id), None)
        or _missing()
    )


def _missing():
    raise ValueError("No recorded Git attempt with that ID")


def _gateway(runtime, attempt):
    request = attempt["request"]
    policy = runtime.store.get(request["proposal_id"], "policy_proposal")["policy"]
    plan = request["payload"]
    return GitGateway(
        plan["root"],
        protected_paths=policy.get("protected_paths", ()),
        policy_hash=plan.get("policy_hash", ""),
    )


@contextmanager
def mutation_lock(gateway):
    """One repository lock, inherited by Git so parent loss cannot release it early."""
    overrides, _ = gateway._configuration()
    _, raw, _ = gateway._run(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"], overrides=overrides
    )
    directory = member_directory(Path(os.fsdecode(raw).rstrip("\n")))
    fd = os.open(directory / "jev-mutation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ValueError("Unsafe Git mutation lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Git is busy with a run for this member; wait for it to stop") from exc
        gateway._mutation_lock_fd = fd
        yield
    finally:
        gateway._mutation_lock_fd = None
        # Do not LOCK_UN: an inherited open-file description must remain locked
        # until an in-flight Git subprocess and its helpers have also closed it.
        os.close(fd)


def execute_git(supervisor, proposal_id, gateway, plan, *, username=None, token=None):
    runtime, store = supervisor.runtime, supervisor.store
    with supervisor._lock(), mutation_lock(gateway):
        request_id = supervisor._authorize(
            proposal_id,
            "git" if plan["operation"] == "commit" else "push",
            plan,
            human_required=True,
        )
        attempt = next(a for a in git_attempts(runtime) if a["request_id"] == request_id)
        attempt_id = attempt["attempt_id"]
        runtime._executing_git_attempt = attempt_id
        try:

            def current_authority():
                if not runtime._active(proposal_id)[1] or runtime.autonomy(proposal_id)[
                    "state"
                ] in {"RESTRICTED", "QUARANTINED"}:
                    raise ValueError("Constitution authority changed before the Git mutation")
                member_directory(plan["root"])

            current_authority()
            result = gateway.execute(
                plan, username=username, token=token, before_mutation=current_authority
            )
            if not _success(
                store.get(request_id, "operation_request"),
                {"proposal_id": proposal_id, "result": result},
            ):
                raise ValueError("Git returned an invalid operation receipt")
            store.append(
                "git_receipt",
                {
                    "format": RECEIPT_FORMAT,
                    "attempt_id": attempt_id,
                    "proposal_id": proposal_id,
                    "request_id": request_id,
                    "result": result,
                },
            )
            return result
        except BaseException as exc:
            store.append(
                "git_failure",
                {
                    "attempt_id": attempt_id,
                    "proposal_id": proposal_id,
                    "request_id": request_id,
                    "plan_id": plan["plan_id"],
                    "error_type": type(exc).__name__,
                    "outcome": "uncertain",
                },
            )
            raise
        finally:
            runtime._executing_git_attempt = None


def git_report(runtime, attempt_id):
    attempt = _attempt(runtime, attempt_id)
    request, plan = attempt["request"], attempt["request"]["payload"]
    proposal = request["proposal_id"]
    cid = runtime.constitution_id(proposal)
    workspace = Path(plan["root"]).resolve()
    affected = {cid: proposal}
    for candidate, value in runtime.store.records("policy_proposal"):
        other = runtime.constitution_id(candidate)
        if (
            other == cid or Path(value["policy"]["workspace"]).resolve() == workspace
        ) and runtime._active(candidate)[1]:
            affected[other] = candidate
    report = {
        "attempt_id": attempt_id,
        "request_id": attempt["request_id"],
        "proposal_id": proposal,
        "workspace": str(workspace),
        "operation": plan["operation"],
        "plan_id": plan["plan_id"],
        "approved_head": plan["state"]["head"],
        "approved_ref": plan["state"]["head_ref"],
        "remote_url": plan.get("remote_url", ""),
        "target_ref": plan.get("target_ref", ""),
        "remote_state": "unobserved",
        "local_state": "unreadable",
        "unresolved": attempt["unresolved"],
        "abandoned": attempt["abandoned"],
        "legacy": attempt["legacy"],
        "receipt_id": attempt["receipt_id"],
        "completion_established": False,
        "ledger_head": runtime.store.events()[-1]["id"],
        "affected_constitutions": [
            {"constitution_id": c, "proposal_id": p} for c, p in sorted(affected.items())
        ],
    }
    try:
        gateway = _gateway(runtime, attempt)
        observed = gateway.preview("status")["state"]
        overrides, _ = gateway._configuration()
        code, value, _ = gateway._run(
            ["rev-parse", "--verify", plan["state"]["head_ref"]], overrides=overrides, check=False
        )
        report["observed_ref_head"] = value.decode().strip() if code == 0 else None
        report["observed_repository"] = observed
        report["local_state"] = (
            "matches_approved_snapshot"
            if (report["observed_ref_head"] or "") == plan["state"]["head"]
            and all(
                observed.get(k) == plan["state"].get(k)
                for k in ("head", "head_ref", "index_digest", "worktree_digest", "config_digest")
            )
            else "changed_since_approval"
        )
    except (ValueError, OSError) as exc:
        report["local_error"] = type(exc).__name__
    report["next_step"] = (
        "Local observations do not prove execution or non-execution. A remote push may have taken effect; abandonment cannot cancel it. If unresolved, review and explicitly abandon only after the local operation stops. This closes accounting without restoring authority or certifying completion."
    )
    if runtime.store.events()[-1]["id"] != report["ledger_head"]:
        raise ValueError("Git evidence changed during inspection; retry")
    return report


def abandon_git(runtime, attempt_id, confirm):
    attempt = _attempt(runtime, attempt_id)
    gateway = _gateway(runtime, attempt)
    with mutation_lock(gateway):
        report = git_report(runtime, attempt_id)
        if not report["unresolved"]:
            raise ValueError("No unresolved Git attempt to abandon")
        if report["local_state"] == "unreadable":
            raise ValueError("Restore repository access before reviewing abandonment")
        if not confirm(
            "Abandon this uncertain Git attempt? No files or refs will be changed. A remote push may already have taken effect. Quarantine remains; no retry, completion or authority restoration is granted.\n"
            + canonical(report)
        ):
            raise ValueError("Git abandonment was not authorized")
        if git_report(runtime, attempt_id) != report:
            raise ValueError("Repository or evidence changed during review; inspect again")
        records = [
            (
                "git_abandoned",
                {
                    "attempt_id": attempt_id,
                    "human_approved": True,
                    "report": report,
                    "report_hash": digest(report),
                },
            )
        ]
        for affected in report["affected_constitutions"]:
            state = runtime.autonomy(affected["proposal_id"])
            records.append(
                (
                    "autonomy",
                    {
                        "constitution_id": affected["constitution_id"],
                        "state": "QUARANTINED",
                        "failures": state["failures"],
                        "reason": "Human closed an uncertain Git attempt; fresh recovery required",
                    },
                )
            )
        ids = runtime.store.append_many(records, expected_head=report["ledger_head"])
        return {
            "attempt_id": attempt_id,
            "record_id": ids[0],
            "abandoned": True,
            "files_or_refs_modified": False,
            "remote_state": "unobserved",
            "authority_restored": False,
            "completion_established": False,
        }
