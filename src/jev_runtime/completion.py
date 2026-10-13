"""Read-only completion verification from independent, explicitly linked receipts."""

import hashlib
from pathlib import Path

from .contracts import Judgment, digest
from .workspace import snapshot

COMPLETION_FORMAT = "jevruntime.completion.v1"
PUBLICATION_FORMAT = "jevruntime.publication.v1"


def judgment_evidence(receipt):
    summary = dict(receipt)
    if "output" in summary:
        output = summary.pop("output").encode()
        summary["output_sha256"] = hashlib.sha256(output).hexdigest()
        summary["output_bytes"] = len(output)
    return summary


def receipt_passes(check, receipt, manifest):
    if receipt.get("passed") is not True or receipt.get("check_hash") != digest(check.as_dict()):
        return False
    if receipt.get("kind") != check.kind or receipt.get("requirement") != check.requirement:
        return False
    if check.kind == "file":
        content = receipt.get("content")
        return (
            receipt.get("path") == check.path
            and isinstance(content, str)
            and check.contains in content
            and hashlib.sha256(content.encode()).hexdigest() == receipt.get("sha256")
            and manifest.get(check.path, {}).get("sha256") == receipt.get("sha256")
        )
    return (
        receipt.get("argv") == list(check.argv)
        and receipt.get("expected_exit") == check.expected_exit
        and receipt.get("required_output") == check.contains
        and type(receipt.get("exit_code")) is int
        and receipt["exit_code"] == check.expected_exit
        and receipt.get("timed_out") is False
        and receipt.get("stopped") is False
        and receipt.get("output_truncated") is False
        and isinstance(receipt.get("output"), str)
        and check.contains in receipt["output"]
    )


def completion_status(runtime, proposal_id):
    """Never invoke JEV or synthesize evidence while answering a status query."""

    def fail(reason):
        return {"passed": False, "reason": reason, "proposal_id": proposal_id}

    try:
        policy, active = runtime._active(proposal_id)
        if not policy.has_acceptance:
            return fail("acceptance_checks_require_revision")
        if not active:
            return fail("constitution_inactive")
        state = runtime.autonomy(proposal_id)
        if state.get("reason") == "publication_requires_reconciliation":
            return fail("publication_unfinished")
        if state.get("reason") == "git_requires_reconciliation":
            return fail("git_unfinished")
        if state["state"] in {"RESTRICTED", "QUARANTINED"}:
            return fail("runtime_recovery_required")
        events = runtime.store.events()
        by_id = {event["id"]: event for event in events}

        def record(identifier, kind):
            event = by_id[identifier]
            if event["kind"] != kind:
                raise ValueError("Wrong completion provenance record kind")
            return event, event["payload"]

        completions = [
            e
            for e in events
            if e["kind"] == "completion" and e["payload"].get("proposal_id") == proposal_id
        ]
        if not completions:
            return fail("no_published_completion")
        event = completions[-1]
        completion = event["payload"]
        if completion.get("passed") is not True:
            return fail("latest_completion_failed")
        if completion.get("format") != COMPLETION_FORMAT:
            return fail("fresh_verification_required")
        run_id, tree_hash = completion["run_id"], completion["tree_hash"]
        from .reconciliation import is_abandoned

        if is_abandoned(runtime.store, run_id):
            return fail("publication_abandoned")
        run_event, run = record(run_id, "runtime_session")
        verification_event, verification = record(completion["verification_id"], "run_verification")
        publication_event, publication = record(completion["publication_id"], "publication")
        result_event, result = record(verification["runtime_result_id"], "runtime_result")
        attempt_event, attempt = record(verification["attempt_id"], "verification_started")
        judgment_event, judgment = record(verification["judgment_id"], "judgment")
        if (
            run.get("proposal_id") != proposal_id
            or any(
                item.get("run_id") != run_id
                or item.get("proposal_id") != proposal_id
                or item.get("tree_hash") != tree_hash
                for item in (result, verification, publication)
            )
            or attempt.get("run_id") != run_id
        ):
            return fail("mismatched_completion_provenance")
        if (
            completion.get("policy_hash") != digest(policy.as_dict())
            or verification.get("policy_hash") != completion["policy_hash"]
        ):
            return fail("policy_changed")
        if (
            verification.get("passed") is not True
            or result.get("success") is not True
            or type(result.get("exit_code")) is not int
            or result["exit_code"] != 0
            or result.get("timed_out") is not False
            or result.get("stopped") is not False
            or result.get("output_truncated") is not False
        ):
            return fail("worker_or_verification_failed")
        if not (
            run_event["seq"]
            < result_event["seq"]
            < attempt_event["seq"]
            < judgment_event["seq"]
            < verification_event["seq"]
            < event["seq"]
            and result_event["seq"] < publication_event["seq"] < event["seq"]
        ):
            return fail("invalid_provenance_order")
        mode = completion.get("mode")
        if publication.get("format") == PUBLICATION_FORMAT:
            started_event, started = record(publication["attempt_id"], "publication_started")
            original_event, original = record(publication["verification_id"], "run_verification")
            if (
                started.get("run_id") != run_id
                or started.get("proposal_id") != proposal_id
                or started.get("verification_id") != original_event["id"]
                or original.get("run_id") != run_id
                or original.get("proposal_id") != proposal_id
                or original.get("passed") is not True
                or original.get("tree_hash") != tree_hash
                or original.get("policy_hash") != completion["policy_hash"]
                or not original_event["seq"] < started_event["seq"] < publication_event["seq"]
            ):
                return fail("invalid_publication_provenance")
            if mode == "publication":
                if original_event["id"] != verification_event["id"]:
                    return fail("invalid_publication_verification")
            elif mode == "reverification":
                if not publication_event["seq"] < attempt_event["seq"]:
                    return fail("invalid_reverification_order")
            else:
                return fail("invalid_completion_mode")
        elif not (
            "format" not in publication
            and mode == "historical_reverification"
            and publication_event["seq"] < attempt_event["seq"]
        ):
            return fail("fresh_verification_required")
        if any(
            e["kind"] == "publication_started"
            and e["payload"].get("run_id") == run_id
            and e["seq"] > publication_event["seq"]
            for e in events
        ):
            return fail("publication_unfinished")
        for kind, expected in (
            ("runtime_result", result_event),
            ("verification_started", attempt_event),
            ("run_verification", verification_event),
            ("publication", publication_event),
        ):
            matching = [
                e for e in events if e["kind"] == kind and e["payload"].get("run_id") == run_id
            ]
            if not matching or matching[-1]["id"] != expected["id"]:
                return fail("newer_run_evidence_requires_verification")
        failures = [
            e
            for e in events
            if e["payload"].get("run_id") == run_id
            and e["kind"] in {"verification_failed", "publication_failed", "runtime_stop"}
        ]
        if any(e["seq"] > attempt_event["seq"] for e in failures):
            return fail("run_failed_or_stopped")
        manifest = snapshot(Path(policy.workspace), protected_paths=run["protected_paths"])
        if digest(manifest) != tree_hash:
            return fail("published_artifacts_changed")
        identifiers = verification["receipt_ids"]
        if (
            not isinstance(identifiers, list)
            or len(identifiers) != len(policy.checks)
            or len(set(identifiers)) != len(identifiers)
        ):
            return fail("incomplete_receipt_coverage")
        evidence = []
        for check, identifier in zip(policy.checks, identifiers, strict=True):
            receipt_event, receipt = record(identifier, "verification_receipt")
            if (
                not attempt_event["seq"] < receipt_event["seq"] < judgment_event["seq"]
                or receipt.get("run_id") != run_id
                or receipt.get("proposal_id") != proposal_id
                or receipt.get("tree_hash") != tree_hash
                or receipt.get("attempt_id") != attempt_event["id"]
            ):
                return fail("mismatched_verification_receipt")
            if not receipt_passes(check, receipt, manifest):
                return fail("acceptance_predicate_failed")
            evidence.append(judgment_evidence({**receipt, "receipt_id": identifier}))
        state = {
            "human_intent": runtime.store.intent_source(policy.intent_id),
            "requirements": [c.as_dict() for c in policy.checks],
            "evidence": evidence,
        }
        if (
            judgment.get("kind") != "completion"
            or judgment.get("state_hash") != digest(state)
            or not Judgment(**judgment["result"]).passes(policy.judgment_threshold)
        ):
            return fail("completion_judgment_failed_or_unbound")
        # Hashing artifacts may take time. Do not certify a result after authority
        # or the ledger changed while this read-only proof was being assembled.
        current_policy, active = runtime._active(proposal_id)
        if not active or current_policy != policy:
            return fail("constitution_inactive")
        if runtime.autonomy(proposal_id)["state"] in {"RESTRICTED", "QUARANTINED"}:
            return fail("runtime_recovery_required")
        latest = runtime.store.events()
        if not latest or latest[-1]["id"] != events[-1]["id"]:
            return fail("evidence_changed_during_verification")
        return {
            "passed": True,
            "reason": "verified_published_artifacts",
            "proposal_id": proposal_id,
            "run_id": run_id,
            "completion_id": event["id"],
            "verification_id": verification_event["id"],
            "publication_id": publication_event["id"],
            "receipt_ids": identifiers,
            "tree_hash": tree_hash,
        }
    except (KeyError, TypeError, ValueError, AttributeError, OSError, RuntimeError):
        return fail("invalid_or_missing_completion_provenance")
