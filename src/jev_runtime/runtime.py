import hashlib
import stat
from dataclasses import asdict
from pathlib import Path

from .capabilities import SUPPORTED_ACTIONS, control_manifest
from .contracts import Decision, Judgment, Policy, canonical, digest


class Runtime:
    """Trusted host API. Models and future workers must never get this object or its ledger."""

    def __init__(self, store, judge):
        self.store, self.judge = store, judge
        self._publishing_attempt = None
        self._executing_git_attempt = None

    def _judge_record(self, kind, state):
        try:
            if len(canonical(state).encode()) > 32000:
                raise ValueError("Judgment context too large")
            result = self.judge.judge(kind, state)
            if not isinstance(result, Judgment):
                raise ValueError("Invalid judgment")
            # Also forces validation of values from custom adapters.
            digest(asdict(result))
        except Exception as exc:
            result = Judgment(None, "unavailable", type(exc).__name__)
        identifier = self.store.append(
            "judgment", {"kind": kind, "state_hash": digest(state), "result": asdict(result)}
        )
        return identifier, result

    def _judge(self, kind, state):
        return self._judge_record(kind, state)[1]

    def propose(self, raw, constitution_id=None):
        policy = Policy.parse(raw)
        intent = self.store.intent_source(policy.intent_id)
        if not Path(policy.workspace).is_dir():
            raise ValueError("Constitution workspace must be an existing directory")
        # Exercise the same deterministic path gate used by operations before activation.
        simulations = {}
        for target in (
            "../outside",
            ".git/config",
            ".jev/ledger.sqlite",
            ".env",
            ".codex/auth.json",
        ):
            try:
                self.checked_path(policy, target)
                simulations[target] = False
            except (ValueError, OSError):
                simulations[target] = True
        # Acceptance files must be reachable through the same gate as staging and
        # evidence collection. Future artifacts are valid; impossible/protected
        # predicates must fail before spending a semantic judgment or activating.
        from .workspace import WorkspaceError, validate_path

        acceptance_paths = []
        for check in policy.checks:
            if check.kind != "file":
                continue
            usable = True
            try:
                validate_path(check.path, protected_paths=policy.protected_paths)
                target = self.checked_path(policy, check.path)
                try:
                    info = target.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISREG(info.st_mode):
                        usable = False
            except (ValueError, OSError, WorkspaceError):
                usable = False
            acceptance_paths.append(
                {"requirement": check.requirement, "path": check.path, "passed": usable}
            )
        unsupported = sorted(set(policy.actions) - SUPPORTED_ACTIONS)
        passed = (
            not unsupported
            and policy.has_acceptance
            and all(simulations.values())
            and all(p["passed"] for p in acceptance_paths)
        )
        validation = self.store.append(
            "policy_validation",
            {
                "policy_hash": digest(policy.as_dict()),
                "schema_pass": True,
                "unsupported_actions": unsupported,
                "controls": control_manifest(),
                "protected_path_simulations": simulations,
                "acceptance_paths": acceptance_paths,
                "acceptance_complete": policy.has_acceptance,
                "passed": passed,
            },
        )
        if unsupported:
            raise ValueError("Unsupported runtime capabilities: " + ", ".join(unsupported))
        if not policy.has_acceptance:
            raise ValueError(
                "Every Constitution needs explicit acceptance requirements and typed checks"
            )
        if not passed:
            raise ValueError("Deterministic Constitution validation failed")
        judgment = self._judge("policy", {"human_intent": intent, "policy": policy.as_dict()})
        return self.store.append(
            "policy_proposal",
            {
                "policy": policy.as_dict(),
                "semantic_pass": judgment.passes(policy.judgment_threshold),
                "semantic_result": asdict(judgment),
                "constitution_id": constitution_id,
                "validation_id": validation,
            },
        )

    def constitution_id(self, proposal_id):
        proposal = self.store.get(proposal_id, "policy_proposal")
        if proposal.get("constitution_id"):
            return proposal["constitution_id"]
        for _, output in self.store.records("onboarding_output"):
            if output.get("proposal_id") == proposal_id:
                return output["session_id"]
        return proposal_id

    def current(self, constitution_id):
        events = [
            e
            for e in self.store.events(
                ("policy_activation", "policy_suspension", "policy_revocation")
            )
            if e["payload"]["constitution_id"] == constitution_id
        ]
        if not events or events[-1]["kind"] != "policy_activation":
            return None
        candidate = events[-1]["payload"]["proposal_id"]
        return candidate if self._active(candidate)[1] else None

    def suspend(self, constitution_id, reason="Human intent changed"):
        self.store.append(
            "policy_suspension", {"constitution_id": constitution_id, "reason": reason}
        )

    def approve(self, proposal_id):
        """Only the trusted human CLI calls this after displaying the exact proposal."""
        proposal = self.store.get(proposal_id, "policy_proposal")
        if proposal["semantic_pass"] is not True:
            raise ValueError("Semantic verification failed; cannot activate")
        policy = Policy.parse(proposal["policy"])
        if not set(policy.actions) <= SUPPORTED_ACTIONS:
            raise ValueError("Unsupported capabilities require a revised Constitution")
        if not policy.has_acceptance:
            raise ValueError(
                "This Constitution needs explicit acceptance checks; revise it before approval"
            )
        if not self._onboarding_current(proposal_id):
            raise ValueError(
                "This proposal was superseded by newer onboarding context; a fresh proposal is required"
            )
        if proposal.get("validation_id"):
            validation = self.store.get(proposal["validation_id"], "policy_validation")
            if not validation["passed"] or validation["policy_hash"] != digest(proposal["policy"]):
                raise ValueError("Deterministic validation does not match this Constitution")
        constitution = self.constitution_id(proposal_id)
        linked = [
            (identifier, p)
            for identifier, p in self.store.records("policy_proposal")
            if p.get("constitution_id") == constitution
        ]
        if linked and linked[-1][0] != proposal_id:
            raise ValueError(
                "This proposal was superseded. Review the current version or explicitly roll back."
            )
        return self.store.append_many(
            [
                (
                    "human_approval",
                    {"proposal_id": proposal_id, "policy_hash": digest(proposal["policy"])},
                ),
                (
                    "policy_activation",
                    {"constitution_id": constitution, "proposal_id": proposal_id},
                ),
            ]
        )[0]

    def _onboarding_current(self, proposal_id):
        # Check at consumption too: a pending input or late activation cannot
        # restore authority from an older interview snapshot.
        outputs = [
            output
            for _, output in self.store.records("onboarding_output")
            if output.get("proposal_id") == proposal_id
        ]
        if outputs:
            output = outputs[-1]
            entries = [
                event
                for event in self.store.events(("onboarding_input", "operator_context"))
                if event["payload"]["session_id"] == output["session_id"]
            ]
            if entries and entries[-1]["id"] != output.get("trigger_id", output["input_id"]):
                return False
        return True

    def _active(self, proposal_id):
        proposal = self.store.get(proposal_id, "policy_proposal")
        approved = any(
            a["proposal_id"] == proposal_id and a["policy_hash"] == digest(proposal["policy"])
            for _, a in self.store.records("human_approval")
        )
        cid = self.constitution_id(proposal_id)
        transitions = [
            e
            for e in self.store.events(
                ("policy_activation", "policy_suspension", "policy_revocation")
            )
            if e["payload"]["constitution_id"] == cid
        ]
        if transitions:
            approved = (
                approved
                and transitions[-1]["kind"] == "policy_activation"
                and transitions[-1]["payload"].get("proposal_id") == proposal_id
            )
        policy = Policy.parse(proposal["policy"])
        return policy, (
            approved
            and set(proposal["policy"]["actions"]) <= SUPPORTED_ACTIONS
            and policy.has_acceptance
            and proposal["semantic_pass"] is True
            and self._onboarding_current(proposal_id)
        )

    def checked_path(self, policy, target):
        root = Path(policy.workspace).resolve(strict=True)
        path = Path(target) if Path(target).is_absolute() else root / target
        if ".." in path.parts or not path.is_relative_to(root):
            raise ValueError("Path is outside the project")
        relative = path.relative_to(root)
        from .workspace import WorkspaceError, validate_path

        if relative.parts:
            try:
                validate_path(relative.as_posix(), protected_paths=policy.protected_paths)
            except WorkspaceError as exc:
                raise ValueError(str(exc)) from exc
        current = root
        for part in relative.parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if (
                stat.S_ISLNK(info.st_mode)
                or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)
                or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
            ):
                raise ValueError("Symlinks, hard links and special files are not authorized")
        return path

    def decide(self, proposal_id, action, target):
        policy, active = self._active(proposal_id)
        reason, decision = "not_human_approved", Decision.APPROVAL_REQUIRED
        root = Path(policy.workspace).resolve()
        path = root / target
        try:
            path = self.checked_path(policy, target)
            safe = True
        except (ValueError, OSError):
            safe = False
        if not set(policy.actions) <= SUPPORTED_ACTIONS:
            reason, decision = "unsupported_policy_requires_revision", Decision.DENY
        elif not policy.has_acceptance:
            reason, decision = "acceptance_checks_require_revision", Decision.DENY
        elif action not in SUPPORTED_ACTIONS or action not in policy.actions or not safe:
            reason, decision = "outside_authority", Decision.DENY
        elif active and self.autonomy(proposal_id)["state"] in {"RESTRICTED", "QUARANTINED"}:
            reason, decision = "autonomy_restricted", Decision.DENY
        elif action in {"delete", "push"}:
            reason, decision = "per_operation_approval_needed", Decision.APPROVAL_REQUIRED
        elif active and self.autonomy(proposal_id)["state"] == "CAUTIOUS":
            reason, decision = "cautious_human_review", Decision.APPROVAL_REQUIRED
        elif active:
            state = {
                "policy": policy.as_dict(),
                "action": action,
                "target": str(path),
                "human_intent": self.store.intent_source(policy.intent_id),
            }
            result = self._judge("action", state)
            decision = Decision.ALLOW if result.passes(policy.judgment_threshold) else Decision.DENY
            reason = "semantic_gate"
        self.store.append(
            "decision",
            {
                "proposal_id": proposal_id,
                "action": action,
                "target": str(path),
                "decision": decision.value,
                "reason": reason,
            },
        )
        return decision

    def request_operation(self, proposal_id, action, payload, *, human_required=False):
        policy, active = self._active(proposal_id)
        decision, reason = Decision.DENY, "inactive_or_outside_authority"
        if not set(policy.actions) <= SUPPORTED_ACTIONS:
            reason = "unsupported_policy_requires_revision"
        elif not policy.has_acceptance:
            reason = "acceptance_checks_require_revision"
        human_required = human_required or self.autonomy(proposal_id)["state"] == "CAUTIOUS"
        if (
            active
            and action in SUPPORTED_ACTIONS
            and action in policy.actions
            and self.autonomy(proposal_id)["state"] not in {"RESTRICTED", "QUARANTINED"}
        ):
            safe = True
            if action in {"read", "write", "delete"}:
                try:
                    self.checked_path(policy, payload["path"])
                except (ValueError, OSError, KeyError):
                    safe = False
            if action in {"git", "push"}:
                from .git_gateway import validate_policy_plan

                safe = validate_policy_plan(policy, action, payload)
                if not safe:
                    reason = "git_review_missing_or_unsafe"
            if action == "execute":
                argv = payload.get("argv", [])
                safe = bool(argv) and all(isinstance(a, str) and "\0" not in a for a in argv)
                covered = any(
                    tuple(argv[: len(prefix)]) == prefix for prefix in policy.shell_commands
                )
                covered = covered or any(
                    c.kind == "command" and list(c.argv) == argv for c in policy.checks
                )
                human_required = human_required or not covered
            if action in {"delete", "git", "push"}:
                human_required = True
            if safe:
                result = self._judge(
                    "action",
                    {
                        "policy": policy.as_dict(),
                        "human_intent": self.store.intent_source(policy.intent_id),
                        "action": action,
                        "operation": payload,
                    },
                )
                if result.passes(policy.judgment_threshold):
                    decision = Decision.APPROVAL_REQUIRED if human_required else Decision.ALLOW
                    reason = "human_confirmation" if human_required else "approved_scope"
                else:
                    reason = "semantic_review_failed"
        return self.store.append(
            "operation_request",
            {
                "proposal_id": proposal_id,
                "action": action,
                "payload": payload,
                "decision": decision.value,
                "reason": reason,
            },
        )

    def consume_operation(self, request_id, *, human_approved=False):
        request = self.store.get(request_id, "operation_request")
        expected_head = self.store.events()[-1]["id"]
        if any(r["request_id"] == request_id for _, r in self.store.records("operation_consumed")):
            raise ValueError("Operation authorization was already used")
        if request["action"] not in SUPPORTED_ACTIONS:
            raise ValueError("Unsupported runtime capability")
        policy, active = self._active(request["proposal_id"])
        if request["action"] in {"git", "push"}:
            from .git_gateway import validate_policy_plan

            if not validate_policy_plan(policy, request["action"], request["payload"]):
                raise ValueError("Git operation lacks a current deterministic policy review")
        if (
            not active
            or request["decision"] == Decision.DENY
            or self.autonomy(request["proposal_id"])["state"] in {"RESTRICTED", "QUARANTINED"}
        ):
            raise ValueError("Operation is denied or policy is no longer active")
        if request["decision"] == Decision.APPROVAL_REQUIRED and not human_approved:
            raise ValueError("This exact operation requires human approval")
        records = []
        if human_approved:
            records.append(
                (
                    "human_operation_approval",
                    {"request_id": request_id, "request_hash": digest(request)},
                )
            )
        records.append(
            ("operation_consumed", {"request_id": request_id, "request_hash": digest(request)})
        )
        if request["action"] in {"git", "push"}:
            from .git_operations import ATTEMPT_FORMAT

            records.append(
                (
                    "git_attempt_started",
                    {
                        "format": ATTEMPT_FORMAT,
                        "request_id": request_id,
                        "request_hash": digest(request),
                        "proposal_id": request["proposal_id"],
                        "plan_id": request["payload"]["plan_id"],
                    },
                )
            )
        self.store.append_many(records, expected_head=expected_head)
        return request

    def autonomy(self, proposal_id):
        cid = self.constitution_id(proposal_id)
        records = [p for _, p in self.store.records("autonomy") if p["constitution_id"] == cid]
        state = (
            records[-1] if records else {"constitution_id": cid, "state": "NORMAL", "failures": 0}
        )
        from .reconciliation import unresolved_publications

        pending = [
            event
            for event in unresolved_publications(self, proposal_id=proposal_id)
            if event["id"] != self._publishing_attempt
        ]
        if pending:
            return {
                **state,
                "state": "QUARANTINED",
                "reason": "publication_requires_reconciliation",
                "publication_attempt_ids": [event["id"] for event in pending],
            }
        from .git_operations import unresolved_git

        git_pending = [
            a
            for a in unresolved_git(self, proposal_id)
            if a["attempt_id"] != self._executing_git_attempt
        ]
        if git_pending:
            return {
                **state,
                "state": "QUARANTINED",
                "reason": "git_requires_reconciliation",
                "git_attempt_ids": [a["attempt_id"] for a in git_pending],
            }
        return state

    def failure(self, proposal_id, reason, *, quarantine=False):
        policy, _ = self._active(proposal_id)
        previous = self.autonomy(proposal_id)
        failures = previous["failures"] + 1
        state = (
            "QUARANTINED"
            if quarantine or previous["state"] == "QUARANTINED" or failures >= policy.max_failures
            else "RESTRICTED"
            if previous["state"] == "RESTRICTED" or failures >= 2
            else "CAUTIOUS"
        )
        self.store.append(
            "autonomy",
            {
                "constitution_id": self.constitution_id(proposal_id),
                "state": state,
                "failures": failures,
                "reason": reason,
            },
        )
        return state

    def recovery_context(self, proposal_id):
        """Event identities distinguish a new failure or reactivation from old state."""
        cid = self.constitution_id(proposal_id)
        events = [e for e in self.store.events() if e["payload"].get("constitution_id") == cid]
        authority = [
            e
            for e in events
            if e["kind"] in {"policy_activation", "policy_suspension", "policy_revocation"}
        ]
        autonomy = [e for e in events if e["kind"] == "autonomy"]
        return {
            "authority_id": authority[-1]["id"] if authority else None,
            "autonomy_id": autonomy[-1]["id"] if autonomy else None,
        }

    def recover(self, proposal_id, *, verification_id=None, human_approved=False):
        from .git_operations import unresolved_git
        from .reconciliation import unresolved_publications
        from .recovery import validate_recovery

        if unresolved_git(self, proposal_id):
            raise ValueError("Reconcile unresolved Git attempts before recovery")
        if unresolved_publications(self, proposal_id=proposal_id):
            raise ValueError("Reconcile unresolved publication attempts before recovery")

        if not human_approved or not verification_id:
            raise ValueError("Recovery requires human approval and a specific fresh verification")
        expected_head = validate_recovery(self, proposal_id, verification_id)
        return self.store.append_many(
            [
                (
                    "autonomy",
                    {
                        "constitution_id": self.constitution_id(proposal_id),
                        "state": "NORMAL",
                        "failures": 0,
                        "reason": "Human reviewed recovery",
                        "verification_id": verification_id,
                    },
                )
            ],
            expected_head=expected_head,
        )[0]

    def collect_file(self, proposal_id, requirement, target):
        policy, active = self._active(proposal_id)
        if not active or requirement not in policy.required_evidence:
            raise ValueError("Unknown requirement or inactive policy")
        if self.decide(proposal_id, "read", target) != Decision.ALLOW:
            raise ValueError("Evidence read denied")
        path = (Path(policy.workspace).resolve() / target).resolve()
        with path.open("rb") as stream:
            content = stream.read(32001)
        if len(content) > 32000:
            raise ValueError("File evidence exceeds the bounded content limit")
        return self.store.append(
            "file_evidence",
            {
                "proposal_id": proposal_id,
                "requirement": requirement,
                "path": str(path),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            },
        )

    def completion_status(self, proposal_id):
        from .completion import completion_status

        return completion_status(self, proposal_id)

    def complete(self, proposal_id):
        return self.completion_status(proposal_id)["passed"]
