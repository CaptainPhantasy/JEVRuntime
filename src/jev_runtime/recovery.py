"""Validate fresh recovery evidence without letting old receipts restore authority."""

from pathlib import Path

from .completion import judgment_evidence, receipt_passes
from .contracts import Judgment, digest
from .workspace import snapshot

RECOVERY_FORMAT = "jevruntime.recovery.v1"


def validate_recovery(runtime, proposal_id, verification_id):
    """Return the ledger head that must still exist when NORMAL is recorded."""
    try:
        policy, active = runtime._active(proposal_id)
        if not active or not policy.has_acceptance or "read" not in policy.actions:
            raise ValueError("Recovery needs an active Constitution with acceptance checks")
        events = runtime.store.events()
        by_id = {event["id"]: event for event in events}

        def record(identifier, kind):
            event = by_id[identifier]
            if event["kind"] != kind:
                raise ValueError("Wrong recovery evidence kind")
            return event, event["payload"]

        verification_event, verification = record(verification_id, "recovery_verification")
        if (
            verification.get("format") != RECOVERY_FORMAT
            or verification.get("passed") is not True
            or verification.get("proposal_id") != proposal_id
        ):
            raise ValueError("Fresh linked recovery verification is required")
        attempt_event, attempt = record(verification["attempt_id"], "recovery_started")
        approval_event, approval = record(verification["approval_id"], "recovery_approval")
        judgment_event, judgment = record(verification["judgment_id"], "judgment")
        context = {key: attempt[key] for key in ("authority_id", "autonomy_id")}
        if (
            attempt.get("proposal_id") != proposal_id
            or attempt.get("policy_hash") != digest(policy.as_dict())
            or approval.get("proposal_id") != proposal_id
            or approval.get("attempt_id") != attempt_event["id"]
            or context != runtime.recovery_context(proposal_id)
            or not attempt_event["seq"]
            < approval_event["seq"]
            < judgment_event["seq"]
            < verification_event["seq"]
        ):
            raise ValueError("Recovery authority or approval changed")
        for kind, expected in (
            ("recovery_started", attempt_event),
            ("recovery_verification", verification_event),
        ):
            matching = [
                e
                for e in events
                if e["kind"] == kind and e["payload"].get("proposal_id") == proposal_id
            ]
            if not matching or matching[-1]["id"] != expected["id"]:
                raise ValueError("A newer recovery attempt requires fresh verification")
        if any(
            (
                e["kind"] == "recovery_failed"
                and e["payload"].get("attempt_id") == attempt_event["id"]
            )
            or (e["kind"] == "autonomy" and e["payload"].get("verification_id") == verification_id)
            for e in events
        ):
            raise ValueError("Recovery verification failed or was already consumed")
        manifest = snapshot(Path(policy.workspace), protected_paths=attempt["protected_paths"])
        if digest(manifest) != attempt["tree_hash"]:
            raise ValueError("Host artifacts changed during recovery")
        ids = verification["receipt_ids"]
        if not isinstance(ids, list) or len(ids) != len(policy.checks) or len(set(ids)) != len(ids):
            raise ValueError("Incomplete recovery receipts")
        evidence = []
        for check, identifier in zip(policy.checks, ids, strict=True):
            event, receipt = record(identifier, "recovery_receipt")
            if (
                not approval_event["seq"] < event["seq"] < judgment_event["seq"]
                or receipt.get("proposal_id") != proposal_id
                or receipt.get("attempt_id") != attempt_event["id"]
                or receipt.get("tree_hash") != attempt["tree_hash"]
                or not receipt_passes(check, receipt, manifest)
            ):
                raise ValueError("Invalid recovery check receipt")
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
            raise ValueError("Recovery judgment failed or lacks provenance")
        if not runtime._active(proposal_id)[1] or runtime.recovery_context(proposal_id) != context:
            raise ValueError("Authority changed during recovery")
        if runtime.store.events()[-1]["id"] != events[-1]["id"]:
            raise ValueError("Ledger changed during recovery verification")
        return events[-1]["id"]
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Incomplete recovery evidence") from exc
