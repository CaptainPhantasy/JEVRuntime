"""Own worker lifecycles, independent receipts, and promotion into the host project."""

import fcntl
import queue
import subprocess
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .broker import ProviderBroker
from .completion import COMPLETION_FORMAT, PUBLICATION_FORMAT, judgment_evidence, receipt_passes
from .contracts import Decision, canonical, digest
from .destinations import contained, member_directory
from .reconciliation import is_abandoned, publication_report, unresolved_publications
from .sandbox import Sandbox, SandboxError
from .workspace import (
    WorkspaceChanged,
    WorkspaceError,
    apply_changes,
    diff,
    read_change,
    snapshot,
    stage,
)


class Supervisor:
    def __init__(self, runtime, directory, *, confirm=None, output=None):
        self.runtime, self.store = runtime, runtime.store
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.confirm = confirm or (lambda message: False)
        self.output = output or (lambda text: None)
        self.sandbox = Sandbox()

    @contextmanager
    def _lock(self):
        with (self.directory / "supervisor.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("Another worker or publication is using this runtime") from exc
            yield

    def _protected(self, policy):
        root = Path(policy.workspace).resolve()
        extra = [self.directory, self.store.path]
        return tuple(policy.protected_paths) + tuple(
            str(p.relative_to(root)) for p in extra if p.is_relative_to(root)
        )

    def _authorize(self, proposal, action, payload, *, human_required=False):
        request_id = self.runtime.request_operation(
            proposal, action, payload, human_required=human_required
        )
        request = self.store.get(request_id, "operation_request")
        if request["decision"] == Decision.DENY:
            raise ValueError("Operation denied: " + request["reason"])
        approved = False
        if request["decision"] == Decision.APPROVAL_REQUIRED:
            approved = self.confirm(
                "Authorize this one " + action + " operation?\n" + canonical(payload)
            )
        self.runtime.consume_operation(request_id, human_approved=approved)
        return request_id

    @staticmethod
    def _git_copy(workspace):
        # Private scratch metadata only; never copy host hooks/config/credentials.
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(workspace.parent),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "JEVRUNTIME",
            "GIT_AUTHOR_EMAIL": "runtime@localhost",
            "GIT_COMMITTER_NAME": "JEVRUNTIME",
            "GIT_COMMITTER_EMAIL": "runtime@localhost",
        }
        for arguments in (
            ("init", "--quiet"),
            ("add", "--all"),
            ("commit", "--quiet", "--allow-empty", "-m", "Isolated baseline"),
        ):
            subprocess.run(
                ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *arguments],
                cwd=workspace,
                env=env,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )

    def run(self, proposal_id, argv, *, provider=None, interactive=False, tool_roots=()):
        with self._lock():
            policy, active = self.runtime._active(proposal_id)
            if not active:
                raise ValueError("Review and activate the current Constitution first")
            if "read" not in policy.actions:
                raise ValueError(
                    "A worker needs explicit read permission before project files can be staged"
                )
            source_root = member_directory(policy.workspace, writable=False)
            for supplied in tool_roots:
                installation = Path(supplied).resolve(strict=True)
                if (
                    installation.is_relative_to(source_root)
                    or source_root.is_relative_to(installation)
                    or self.directory.is_relative_to(installation)
                    or installation.is_relative_to(self.directory)
                    or self.store.path.is_relative_to(installation)
                    or any(
                        p in {".ssh", ".aws", ".codex", ".claude", ".jev", ".config"}
                        for p in installation.parts
                    )
                ):
                    raise ValueError(
                        "Tool mounts cannot expose the source project, runtime state, or credential directories"
                    )
            availability = self.sandbox.check()
            if not availability["available"]:
                raise SandboxError(availability["reason"])
            self._authorize(
                proposal_id,
                "execute",
                {"argv": argv, "workspace": policy.workspace, "isolated": True},
            )
            self._authorize(
                proposal_id, "read", {"path": ".", "purpose": "stage_project_for_isolated_worker"}
            )
            root = self.directory / str(uuid4())
            root.mkdir(mode=0o700)
            working = root / "workspace"
            protected = self._protected(policy)
            baseline = stage(Path(policy.workspace), working, protected_paths=protected)
            self._git_copy(working)
            run_id = self.store.append(
                "runtime_session",
                {
                    "proposal_id": proposal_id,
                    "constitution_id": self.runtime.constitution_id(proposal_id),
                    "directory": str(root),
                    "argv": argv,
                    "baseline": baseline,
                    "protected_paths": list(protected),
                    "tool_roots": list(tool_roots),
                },
            )
            self.output("Worker session: " + run_id + "\n")
            provider_events = queue.SimpleQueue()
            last_behavior = time.monotonic()
            deferred_observations = 0

            def record_provider_events(*, fail_on_error=True):
                failed = False
                while not provider_events.empty():
                    event = provider_events.get()
                    self.store.append("provider_event", {"run_id": run_id, **event})
                    if event["status"] >= 400:
                        failed = True
                if failed and fail_on_error:
                    raise ValueError("Worker model provider request failed")

            def tick():
                nonlocal last_behavior, deferred_observations
                record_provider_events()
                if any(
                    record["run_id"] == run_id for _, record in self.store.records("runtime_stop")
                ):
                    return False
                _, still_active = self.runtime._active(proposal_id)
                if not still_active or self.runtime.autonomy(proposal_id)["state"] in {
                    "RESTRICTED",
                    "QUARANTINED",
                }:
                    return False
                if time.monotonic() - last_behavior >= policy.behavior_interval:
                    try:
                        changes = diff(working, baseline, protected_paths=protected)
                        deferred_observations = 0
                    except WorkspaceChanged:
                        deferred_observations += 1
                        self.store.append(
                            "observation_deferred",
                            {"run_id": run_id, "attempt": deferred_observations},
                        )
                        last_behavior = time.monotonic() - policy.behavior_interval + 1
                        if deferred_observations >= 3:
                            self.runtime.failure(
                                proposal_id, "Unable to obtain a stable behavior observation"
                            )
                            return False
                        return True
                    result = self.runtime._judge(
                        "behavior",
                        {
                            "policy": policy.as_dict(),
                            "human_intent": self.store.intent_source(policy.intent_id),
                            "observed_changes": changes,
                            "elapsed_seconds": time.monotonic() - started,
                        },
                    )
                    self.store.append(
                        "behavior_observation",
                        {
                            "run_id": run_id,
                            "changes": changes,
                            "passed": result.passes(policy.judgment_threshold),
                        },
                    )
                    last_behavior = time.monotonic()
                    if not result.passes(policy.judgment_threshold):
                        self.runtime.failure(proposal_id, "Behavior review failed", quarantine=True)
                        return False
                return True

            started = time.monotonic()
            try:
                with ExitStack() as stack:
                    command = list(argv)
                    socket_dir, environment = None, {}
                    if provider:
                        broker = stack.enter_context(
                            ProviderBroker(
                                root / "provider",
                                base_url=provider["base_url"],
                                api_key=provider["api_key"],
                                model=provider.get("model"),
                                auth_required=provider.get("auth_required", True),
                                on_event=provider_events.put,
                            )
                        )
                        socket_dir = root / "provider"
                        environment = {
                            "OPENAI_BASE_URL": broker.base_url + "/v1",
                            "OPENAI_API_KEY": "runtime-broker",
                            "ANTHROPIC_BASE_URL": broker.base_url,
                            "ANTHROPIC_API_KEY": "runtime-broker",
                            "ANTHROPIC_AUTH_TOKEN": "runtime-broker",
                            "JEV_MODEL": provider.get("model", ""),
                            "JEV_ALLOW_WRITE": "1" if "write" in policy.actions else "0",
                            "JEV_ALLOW_EXECUTE": "1" if "execute" in policy.actions else "0",
                        }
                        command = ["python3", "/run/jev/relay.py", "--", *command]
                    result = self.sandbox.run(
                        command,
                        working,
                        timeout=policy.max_seconds,
                        readonly="write" not in policy.actions and "delete" not in policy.actions,
                        env=environment,
                        on_output=self.output,
                        on_tick=tick,
                        interactive=interactive,
                        socket_dir=socket_dir,
                        tool_roots=tool_roots,
                    )
                # The broker settles or records cancellation of outstanding calls
                # on context exit. Drain their final receipts before marking success.
                record_provider_events()
                changes = diff(working, baseline, protected_paths=protected)
                manifest = snapshot(working, protected_paths=protected)
                success = (
                    result["exit_code"] == 0
                    and not result["timed_out"]
                    and not result["stopped"]
                    and not result.get("output_truncated", False)
                )
                self.store.append(
                    "runtime_result",
                    {
                        "run_id": run_id,
                        "proposal_id": proposal_id,
                        "success": success,
                        "tree_hash": digest(manifest),
                        "changes": changes,
                        **result,
                    },
                )
                explicitly_stopped = any(
                    record["run_id"] == run_id for _, record in self.store.records("runtime_stop")
                )
                if not success and not explicitly_stopped and self.runtime._active(proposal_id)[1]:
                    self.runtime.failure(proposal_id, "Worker stopped or exited unsuccessfully")
            except BaseException as exc:
                # Preserve every failure receipt, including requests cancelled
                # while unwinding the broker after a worker error or interruption.
                record_provider_events(fail_on_error=False)
                self.store.append(
                    "runtime_result",
                    {
                        "run_id": run_id,
                        "proposal_id": proposal_id,
                        "success": False,
                        "error_type": type(exc).__name__,
                        "interrupted": isinstance(exc, KeyboardInterrupt),
                    },
                )
                if not isinstance(exc, KeyboardInterrupt):
                    self.runtime.failure(
                        proposal_id,
                        "Worker supervision failed",
                        quarantine=isinstance(exc, WorkspaceError),
                    )
                raise
            return run_id

    def get_run(self, run_id):
        if is_abandoned(self.store, run_id):
            raise ValueError("This publication was abandoned; start a new worker")
        run = self.store.get(run_id, "runtime_session")
        root = Path(run["directory"])
        if root.parent != self.directory or root.is_symlink():
            raise ValueError("Runtime session has an invalid workspace")
        results = [r for _, r in self.store.records("runtime_result") if r["run_id"] == run_id]
        if not results or not results[-1]["success"]:
            raise ValueError("Worker has not finished successfully; its output cannot be published")
        return run, root / "workspace", results[-1]

    def preview(self, run_id):
        run, working, result = self.get_run(run_id)
        manifest = snapshot(working, protected_paths=run["protected_paths"])
        if digest(manifest) != result["tree_hash"]:
            raise ValueError("Worker files changed after the recorded exit")
        return diff(working, run["baseline"], protected_paths=run["protected_paths"])

    def verify(self, run_id):
        with self._lock():
            verification_id, receipts = self._verify(run_id)
            publications = [
                (i, p) for i, p in self.store.records("publication") if p["run_id"] == run_id
            ]
            if publications:
                verification = self.store.get(verification_id, "run_verification")
                try:
                    run, _, result = self.get_run(run_id)
                    policy, active = self.runtime._active(run["proposal_id"])
                    publication_id, publication = publications[-1]
                    if (
                        not active
                        or publication["tree_hash"] != result["tree_hash"]
                        or digest(
                            snapshot(Path(policy.workspace), protected_paths=run["protected_paths"])
                        )
                        != result["tree_hash"]
                    ):
                        raise ValueError(
                            "Published artifacts changed; fresh verification cannot certify them"
                        )
                    if not self.runtime._active(run["proposal_id"])[1] or self.runtime.autonomy(
                        run["proposal_id"]
                    )["state"] in {"RESTRICTED", "QUARANTINED"}:
                        raise ValueError("Constitution or autonomy changed during verification")
                    mode = (
                        "reverification"
                        if publication.get("format") == PUBLICATION_FORMAT
                        else "historical_reverification"
                    )
                    self.store.append(
                        "completion",
                        self._completion_record(
                            publication, verification_id, publication_id, mode=mode
                        ),
                    )
                except BaseException as exc:
                    self.store.append(
                        "verification_failed",
                        {
                            "run_id": run_id,
                            "attempt_id": verification["attempt_id"],
                            "phase": "host_attestation",
                            "error_type": type(exc).__name__,
                        },
                    )
                    raise
            return receipts

    def _verify(self, run_id):
        attempt = self.store.append("verification_started", {"run_id": run_id})
        try:
            return self._verify_attempt(run_id, attempt)
        except BaseException as exc:
            self.store.append(
                "verification_failed",
                {
                    "run_id": run_id,
                    "attempt_id": attempt,
                    "error_type": type(exc).__name__,
                },
            )
            raise

    def _verify_attempt(self, run_id, attempt):
        run, working, result = self.get_run(run_id)
        policy, active = self.runtime._active(run["proposal_id"])
        if not active or not policy.checks:
            raise ValueError("An active Constitution with explicit file/command checks is required")
        if (
            result.get("output_truncated") is not False
            or result.get("timed_out") is not False
            or result.get("stopped") is not False
            or type(result.get("exit_code")) is not int
            or result["exit_code"] != 0
        ):
            raise ValueError("Worker result lacks a complete successful execution receipt")
        self.preview(run_id)
        result_id = [i for i, r in self.store.records("runtime_result") if r["run_id"] == run_id][
            -1
        ]
        receipts = []
        for check in policy.checks:
            if check.kind == "file":
                manifest = snapshot(working, protected_paths=run["protected_paths"])
                if check.path not in manifest:
                    raise ValueError("Required artifact is absent: " + check.path)
                content = read_change(
                    working,
                    {
                        "path": check.path,
                        "action": "write",
                        "after": manifest[check.path]["sha256"],
                        "before": None,
                        "size": manifest[check.path]["size"],
                        "mode": manifest[check.path]["mode"],
                    },
                    protected_paths=run["protected_paths"],
                )
                passed = check.contains in content
                receipt = {
                    "kind": "file",
                    "path": check.path,
                    "sha256": manifest[check.path]["sha256"],
                    "content": content,
                    "passed": passed,
                }
            else:
                self._authorize(
                    run["proposal_id"],
                    "execute",
                    {
                        "argv": list(check.argv),
                        "run_id": run_id,
                        "purpose": "independent_verification",
                    },
                )
                execution = self.sandbox.run(
                    list(check.argv),
                    working,
                    timeout=policy.max_seconds,
                    readonly=True,
                    env={"PYTHONDONTWRITEBYTECODE": "1"},
                    on_output=self.output,
                    tool_roots=tuple(run["tool_roots"]),
                    on_tick=lambda: (
                        self.runtime._active(run["proposal_id"])[1]
                        and self.runtime.autonomy(run["proposal_id"])["state"]
                        not in {"RESTRICTED", "QUARANTINED"}
                    ),
                )
                passed = (
                    execution["exit_code"] == check.expected_exit
                    and not execution["timed_out"]
                    and not execution["stopped"]
                    and execution.get("output_truncated") is False
                    and check.contains in execution["output"]
                )
                receipt = {
                    "kind": "command",
                    "argv": list(check.argv),
                    "expected_exit": check.expected_exit,
                    "required_output": check.contains,
                    "passed": passed,
                    **execution,
                }
            receipt.update(
                {
                    "run_id": run_id,
                    "proposal_id": run["proposal_id"],
                    "requirement": check.requirement,
                    "check_hash": digest(check.as_dict()),
                    "attempt_id": attempt,
                    "tree_hash": result["tree_hash"],
                }
            )
            receipt_id = self.store.append("verification_receipt", receipt)
            receipt["receipt_id"] = receipt_id
            receipts.append(receipt)
            if not passed:
                self.runtime.failure(run["proposal_id"], "Independent acceptance check failed")
                raise ValueError("Acceptance check failed: " + check.requirement)
        if not self.runtime._active(run["proposal_id"])[1]:
            raise ValueError("Constitution changed during verification")
        self.preview(run_id)
        judgment_id, verdict = self.runtime._judge_record(
            "completion",
            {
                "human_intent": self.store.intent_source(policy.intent_id),
                "requirements": [c.as_dict() for c in policy.checks],
                "evidence": [judgment_evidence(r) for r in receipts],
            },
        )
        self.preview(run_id)
        passed = (
            verdict.passes(policy.judgment_threshold)
            and self.runtime._active(run["proposal_id"])[1]
            and self.runtime.autonomy(run["proposal_id"])["state"]
            not in {"RESTRICTED", "QUARANTINED"}
        )
        verification_id = self.store.append(
            "run_verification",
            {
                "run_id": run_id,
                "proposal_id": run["proposal_id"],
                "tree_hash": result["tree_hash"],
                "policy_hash": digest(policy.as_dict()),
                "runtime_result_id": result_id,
                "attempt_id": attempt,
                "receipt_ids": [r["receipt_id"] for r in receipts],
                "judgment_id": judgment_id,
                "passed": passed,
            },
        )
        if not passed:
            raise ValueError("JEV could not establish completion from the recorded evidence")
        return verification_id, receipts

    def _completion_record(
        self, publication, verification_id, publication_id, *, mode="publication"
    ):
        verification = self.store.get(verification_id, "run_verification")
        return {
            "format": COMPLETION_FORMAT,
            "mode": mode,
            "run_id": publication["run_id"],
            "proposal_id": publication["proposal_id"],
            "passed": True,
            "tree_hash": publication["tree_hash"],
            "policy_hash": verification["policy_hash"],
            "verification_id": verification_id,
            "publication_id": publication_id,
        }

    def publish(self, run_id):
        with self._lock():
            run, working, result = self.get_run(run_id)
            if any(r["run_id"] == run_id for _, r in self.store.records("publication")):
                raise ValueError("This worker result was already published")
            policy, active = self.runtime._active(run["proposal_id"])
            if not active:
                raise ValueError("The worker's Constitution is no longer active")
            member_directory(policy.workspace, writable=False)
            if unresolved_publications(self.runtime, proposal_id=run["proposal_id"]):
                raise ValueError("Reconcile unresolved publication attempts before publication")
            verification_id, _ = self._verify(run_id)
            publication_attempt = self.store.append(
                "publication_started",
                {
                    "run_id": run_id,
                    "proposal_id": run["proposal_id"],
                    "verification_id": verification_id,
                },
            )
            self.runtime._publishing_attempt = publication_attempt
            phase, applied = "preflight", []
            try:
                changes = self.preview(run_id)
                if (
                    snapshot(Path(policy.workspace), protected_paths=run["protected_paths"])
                    != run["baseline"]
                ):
                    raise ValueError(
                        "Project changed while the worker ran. Start a fresh worker from the current files."
                    )
                # Judge complete file contents, never a truncated diff or a worker claim.
                for change in changes:
                    payload = dict(change)
                    if change["action"] == "write":
                        payload["content"] = read_change(
                            working, change, protected_paths=run["protected_paths"]
                        )
                    self._authorize(run["proposal_id"], change["action"], payload)
                if not self.runtime._active(run["proposal_id"])[1] or self.runtime.autonomy(
                    run["proposal_id"]
                )["state"] in {"RESTRICTED", "QUARANTINED"}:
                    raise ValueError("Constitution or autonomy changed during review")
                project = member_directory(policy.workspace, writable=bool(changes))
                for change in changes:
                    contained(project, project / change["path"])
                phase = "application"
                applied = apply_changes(
                    Path(policy.workspace),
                    working,
                    run["baseline"],
                    changes,
                    protected_paths=run["protected_paths"],
                )
                phase = "host_attestation"
                host = snapshot(Path(policy.workspace), protected_paths=run["protected_paths"])
                if digest(host) != result["tree_hash"]:
                    raise ValueError("Host artifacts do not match the verified worker tree")
                if not self.runtime._active(run["proposal_id"])[1] or self.runtime.autonomy(
                    run["proposal_id"]
                )["state"] in {"RESTRICTED", "QUARANTINED"}:
                    raise ValueError(
                        "Constitution or autonomy changed during publication; completion remains unverified"
                    )
                publication = {
                    "format": PUBLICATION_FORMAT,
                    "attempt_id": publication_attempt,
                    "verification_id": verification_id,
                    "run_id": run_id,
                    "proposal_id": run["proposal_id"],
                    "changes": applied,
                    "tree_hash": digest(host),
                }
                phase = "recording"
                publication_id = str(uuid4())
                self.store.append_many(
                    [
                        ("publication", publication),
                        (
                            "completion",
                            self._completion_record(publication, verification_id, publication_id),
                        ),
                    ],
                    identifiers=[publication_id, str(uuid4())],
                )
            except BaseException as exc:
                # After host writes, retain their outcome for explicit reconciliation.
                # Do not blindly roll back over a concurrent human change.
                self.store.append(
                    "publication_failed",
                    {
                        "run_id": run_id,
                        "proposal_id": run["proposal_id"],
                        "attempt_id": publication_attempt,
                        "verification_id": verification_id,
                        "phase": phase,
                        "error_type": type(exc).__name__,
                        "applied": getattr(exc, "applied", [])
                        if phase == "application"
                        else applied,
                        "rollback_errors": getattr(exc, "rollback_errors", []),
                    },
                )
                if phase != "preflight":
                    self.runtime.failure(
                        run["proposal_id"], "Publication needs reconciliation", quarantine=True
                    )
                raise
            finally:
                self.runtime._publishing_attempt = None
            return applied

    def recover(self, proposal_id):
        """Run only approved acceptance checks while general authority stays restricted."""
        from .recovery import RECOVERY_FORMAT

        with self._lock():
            from .git_operations import unresolved_git

            if unresolved_git(self.runtime, proposal_id):
                raise ValueError("Reconcile unresolved Git attempts before recovery")
            if unresolved_publications(self.runtime, proposal_id=proposal_id):
                raise ValueError("Reconcile unresolved publication attempts before recovery")
            policy, active = self.runtime._active(proposal_id)
            if not active or not policy.has_acceptance or "read" not in policy.actions:
                raise ValueError(
                    "Recovery needs an active Constitution with read permission and explicit checks"
                )
            member_directory(policy.workspace, writable=False)
            context = self.runtime.recovery_context(proposal_id)
            protected = self._protected(policy)
            with TemporaryDirectory(prefix="recovery-", dir=self.directory) as temporary:
                working = Path(temporary) / "workspace"
                manifest = stage(Path(policy.workspace), working, protected_paths=protected)
                attempt = self.store.append(
                    "recovery_started",
                    {
                        "proposal_id": proposal_id,
                        "policy_hash": digest(policy.as_dict()),
                        "tree_hash": digest(manifest),
                        "protected_paths": list(protected),
                        **context,
                    },
                )
                try:
                    if not self.confirm(
                        "Run these approved checks in a read-only copy of "
                        + policy.workspace
                        + " to restore this Constitution's worker capabilities?\n"
                        + canonical([c.as_dict() for c in policy.checks])
                    ):
                        raise ValueError("Recovery was not authorized")
                    approval = self.store.append(
                        "recovery_approval", {"attempt_id": attempt, "proposal_id": proposal_id}
                    )

                    def still_current():
                        return (
                            self.runtime._active(proposal_id)[1]
                            and self.runtime.recovery_context(proposal_id) == context
                        )

                    if not still_current():
                        raise ValueError("Authority changed before recovery checks")
                    receipts = []
                    for check in policy.checks:
                        if not still_current():
                            raise ValueError("Authority changed before a recovery check")
                        if check.kind == "file":
                            if check.path not in manifest:
                                raise ValueError("Recovery artifact is missing: " + check.path)
                            item = manifest[check.path]
                            content = read_change(
                                working,
                                {
                                    "path": check.path,
                                    "action": "write",
                                    "before": None,
                                    "after": item["sha256"],
                                    "size": item["size"],
                                    "mode": item["mode"],
                                },
                                protected_paths=protected,
                            )
                            receipt = {
                                "kind": "file",
                                "path": check.path,
                                "content": content,
                                "sha256": item["sha256"],
                            }
                        else:
                            execution = self.sandbox.run(
                                list(check.argv),
                                working,
                                timeout=policy.max_seconds,
                                readonly=True,
                                env={"PYTHONDONTWRITEBYTECODE": "1"},
                                on_output=self.output,
                                on_tick=still_current,
                            )
                            receipt = {
                                **execution,
                                "kind": "command",
                                "argv": list(check.argv),
                                "expected_exit": check.expected_exit,
                                "required_output": check.contains,
                            }
                        receipt.update(
                            {
                                "proposal_id": proposal_id,
                                "attempt_id": attempt,
                                "tree_hash": digest(manifest),
                                "check_hash": digest(check.as_dict()),
                                "requirement": check.requirement,
                                "passed": True,
                            }
                        )
                        receipt["passed"] = receipt_passes(check, receipt, manifest)
                        identifier = self.store.append("recovery_receipt", receipt)
                        receipts.append({**receipt, "receipt_id": identifier})
                        if not receipt["passed"]:
                            raise ValueError("Recovery verification failed: " + check.requirement)
                    if not still_current():
                        raise ValueError("Authority changed during recovery")
                    judgment_id, verdict = self.runtime._judge_record(
                        "completion",
                        {
                            "human_intent": self.store.intent_source(policy.intent_id),
                            "requirements": [c.as_dict() for c in policy.checks],
                            "evidence": [judgment_evidence(r) for r in receipts],
                        },
                    )
                    if digest(snapshot(working, protected_paths=protected)) != digest(manifest):
                        raise ValueError("Recovery workspace changed during verification")
                    verification = self.store.append(
                        "recovery_verification",
                        {
                            "format": RECOVERY_FORMAT,
                            "proposal_id": proposal_id,
                            "attempt_id": attempt,
                            "approval_id": approval,
                            "receipt_ids": [r["receipt_id"] for r in receipts],
                            "judgment_id": judgment_id,
                            "passed": verdict.passes(policy.judgment_threshold),
                        },
                    )
                    self.runtime.recover(
                        proposal_id, verification_id=verification, human_approved=True
                    )
                except BaseException as exc:
                    self.store.append(
                        "recovery_failed",
                        {
                            "proposal_id": proposal_id,
                            "attempt_id": attempt,
                            "error_type": type(exc).__name__,
                        },
                    )
                    raise
            self.output(
                "Fresh checks passed. Worker capabilities restored; publication and completion status are unchanged.\n"
            )
            return verification

    def abandon_publication(self, run_id):
        """Close an interrupted attempt without modifying or certifying host files."""
        with self._lock():
            run = self.store.get(run_id, "runtime_session")
            if Path(run["directory"]).parent.resolve() != self.directory:
                raise ValueError("Use the recorded worker directory to reconcile this run")
            report = publication_report(self.runtime, run_id)
            if not report["unresolved"] or report["abandoned"]:
                raise ValueError("No unresolved publication attempt to abandon")
            if report["host_state"] == "unreadable":
                raise ValueError("Restore access to the project before reviewing abandonment")
            if not self.confirm(
                "Close this publication as abandoned? Files remain exactly as they are; "
                "affected Constitutions stay quarantined and no completion is certified.\n"
                + canonical(report)
            ):
                raise ValueError("Publication abandonment was not authorized")
            if publication_report(self.runtime, run_id) != report:
                raise ValueError("Files or evidence changed during review; inspect again")
            records = [
                (
                    "publication_abandoned",
                    {
                        "run_id": run_id,
                        "proposal_id": run["proposal_id"],
                        "attempt_ids": report["attempt_ids"],
                        "human_approved": True,
                        "report": report,
                        "report_hash": digest(report),
                    },
                )
            ]
            for affected in report["affected_constitutions"]:
                state = self.runtime.autonomy(affected["proposal_id"])
                records.append(
                    (
                        "autonomy",
                        {
                            "constitution_id": affected["constitution_id"],
                            "state": "QUARANTINED",
                            "failures": state["failures"],
                            "reason": "Human closed an unresolved publication; fresh recovery required",
                        },
                    )
                )
            identifiers = self.store.append_many(records, expected_head=report["ledger_head"])
            return {
                "abandoned": True,
                "run_id": run_id,
                "record_id": identifiers[0],
                "files_modified": False,
                "completion_established": False,
                "authority_restored": False,
            }
