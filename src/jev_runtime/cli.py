import argparse
import getpass
import json
import os
import shlex
import sys
from contextlib import ExitStack
from pathlib import Path

from .config import ConnectionRequired, generative_settings, judgment_settings, load, setup
from .contracts import Decision, digest
from .destinations import (
    DestinationError,
    command_prefix,
    member_directory,
    readonly_command,
    resolve_destinations,
    validate_settings_destination,
    validate_workspace_selection,
)
from .judgment import FakeJudge, TypeSafeJudge, judgment_client
from .onboarding import (
    FakeOnboarding,
    OnboardingModel,
    OnboardingServiceError,
    OnboardingSession,
    worker_brief,
)
from .runtime import Runtime
from .store import Store
from .supervisor import Supervisor

_CREDENTIALS = {}


def display(text):
    # Provider output cannot hide or redraw the human approval prompt.
    print("".join(char for char in text if char.isprintable() or char in "\n\t"))


def credential(settings, label, *, purpose, disallowed=()):
    if settings.get("auth_required") is False:
        return ""
    identity = (purpose, settings["api_key_env"], settings.get("base_url", ""))
    from_environment = os.environ.get(settings["api_key_env"], "").strip()
    value = from_environment or _CREDENTIALS.get(identity, "")
    if not value:
        if not sys.stdin.isatty():
            raise ValueError(
                "Set " + settings["api_key_env"] + " before starting a noninteractive session"
            )
        value = getpass.getpass(label + " API key (hidden, used only for this run): ").strip()
        if not value:
            raise ValueError("An API key is required for " + label)
    if value in disallowed:
        if _CREDENTIALS.get(identity) == value:
            del _CREDENTIALS[identity]
        raise ValueError("A JEV credential cannot be used for a generative model")
    if not from_environment:
        _CREDENTIALS[identity] = value
    return value


def model_credential(config, settings, label, purpose):
    judgment = config.get("judgment", {})
    reference = judgment.get("api_key_env")
    known = (
        [os.environ.get(reference, "").strip()] if isinstance(reference, str) and reference else []
    )
    known.extend(secret for identity, secret in _CREDENTIALS.items() if identity[0] == "judgment")
    return credential(settings, label, purpose=purpose, disallowed=known)


class LazyJudge:
    """No provider or credential access until a deterministic gate needs judgment."""

    def __init__(self, factory):
        self.factory, self.instance = factory, None

    def judge(self, kind, state):
        if self.instance is None:
            self.instance = self.factory()
        return self.instance.judge(kind, state)


class ConnectedOnboarding:
    """Keep saved interviews usable without initializing a generative connection."""

    def __init__(self, config):
        self.config = config

    def turn(self, conversation, intent_id, workspace):
        settings = generative_settings(self.config, "onboarding")
        model = OnboardingModel(
            {**settings, "agent_command": self.config.get("agent", {}).get("command", [])},
            api_key=model_credential(self.config, settings, "Onboarding model", "onboarding"),
        )
        return model.turn(conversation, intent_id, workspace)


def review(runtime, proposal_id):
    proposal = runtime.store.get(proposal_id, "policy_proposal")
    policy = proposal["policy"]
    print("\nProposed Constitution")
    display("Project: " + policy["workspace"])
    display("Requested capabilities: " + (", ".join(policy["actions"]) or "none"))
    missing = {"read", "execute"} - set(policy["actions"])
    if missing:
        print(
            "Worker launch unavailable: this scope does not authorize "
            + " and ".join(sorted(missing))
            + "."
        )
        print(
            "Keep this restricted scope or clarify permissions in the interview; /run cannot launch it."
        )
    print("Completion requires:")
    for requirement in policy["required_evidence"]:
        display("  • " + requirement)
    display(
        "Protected paths: "
        + (", ".join(policy.get("protected_paths", [])) or "host baseline protections")
    )
    for check in policy.get("checks", []):
        display(
            "  Check: "
            + (shlex.join(check["argv"]) if check["kind"] == "command" else check["path"])
        )
    for command in policy.get("shell_commands", []):
        display("  Launch/check prefix without an extra operation approval: " + shlex.join(command))
    for check in policy.get("checks", []):
        if check["kind"] == "command":
            print("    Required exit status: " + str(check["expected_exit"]))
        if check.get("contains"):
            display("    Must contain exactly this text: " + repr(check["contains"]))
    print("Judgment floor: " + str(policy.get("judgment_threshold", 0.95)))
    print(
        "Time limit: "
        + str(policy.get("max_seconds", 900))
        + " seconds; failure limit: "
        + str(policy.get("max_failures", 3))
        + "; behavior review interval: "
        + str(policy.get("behavior_interval", 30))
        + " seconds"
    )
    print(
        "Child commands share the same isolated files/network permissions; launch prefixes do not filter every child command."
    )
    print(
        "Workers run in a private copy. Delete and host Git operations require separate approval."
    )
    print("JEV review: " + ("passed" if proposal["semantic_pass"] else "did not pass"))
    if result := proposal.get("semantic_result"):
        display("JEV model: " + result["model"])
        if result.get("error"):
            display("JEV unavailable: " + result["error"] + "; authority remains inactive.")
        else:
            if result.get("dimensions"):
                print("JEV dimension probabilities (every dimension must meet the floor):")
                for dimension, probability in result["dimensions"].items():
                    display("  " + dimension + ": " + str(probability))
                print("Lowest dimension: " + str(result["probability"]))
            else:
                print("JEV probability: " + str(result["probability"]))
            if not proposal["semantic_pass"]:
                print(
                    "Below the required threshold. Clarify the scope or completion conditions before activation."
                )
    print("Proposal: " + proposal_id)


def human_approval(runtime, proposal_id):
    review(runtime, proposal_id)
    if not runtime.store.get(proposal_id, "policy_proposal")["semantic_pass"]:
        print("This proposal cannot activate. Continue the interview to revise it.")
        return False
    if not sys.stdin.isatty():
        raise ValueError("Human approval requires an interactive terminal")
    answer = input("Authorize this exact proposed scope? Type yes or no: ").strip().lower()
    if answer != "yes":
        print("No approval recorded. You can continue describing changes.")
        return False
    runtime.approve(proposal_id)
    print("Your approval is recorded for this policy version.")
    return True


def confirm_operation(message):
    display(message)
    if not sys.stdin.isatty():
        return False
    return input("Type yes to authorize, or no to leave it unchanged: ").strip().lower() == "yes"


def supervisor(runtime, args):
    directory = Path(args.run_dir).resolve()
    if getattr(args, "run_id", None) and not getattr(args, "explicit_run_dir", None):
        recorded = runtime.store.get(args.run_id, "runtime_session")
        policy, _ = runtime._active(recorded["proposal_id"])
        validate_workspace_selection(args, policy.workspace)
        directory = member_directory(Path(recorded["directory"]).parent)

    def output(text):
        print("".join(c for c in text if c.isprintable() or c in "\n\t"), end="", flush=True)

    return Supervisor(runtime, directory, confirm=confirm_operation, output=output)


def selected_policy(runtime, args):
    if getattr(args, "proposal", None):
        policy, _ = runtime._active(args.proposal)
        validate_workspace_selection(args, policy.workspace)
        return args.proposal
    workspace = str(Path(getattr(args, "workspace", None) or ".").resolve())
    sessions = [
        identifier
        for identifier, value in runtime.store.records("onboarding_session")
        if not getattr(args, "explicit_workspace", False) or value["workspace"] == workspace
    ]
    session = getattr(args, "session", None) or (sessions[-1] if sessions else None)
    proposal = runtime.current(session) if session else None
    if proposal is None:
        raise ValueError(
            "No active Constitution for this project. Run jev and approve the current proposal."
        )
    policy, _ = runtime._active(proposal)
    validate_workspace_selection(args, policy.workspace)
    return proposal


def worker_arguments(parser):
    parser.add_argument(
        "--no-provider", action="store_true", help="No model connection for this worker"
    )
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("commandline", nargs=argparse.REMAINDER)


class InterviewRunParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(
            "Cannot start worker: "
            + message
            + ". Use /run [--no-provider] [--interactive] [--] COMMAND [ARG ...]."
        )


def interview_run_arguments(text, args):
    parser = InterviewRunParser(prog="/run", add_help=False, allow_abbrev=False)
    worker_arguments(parser)
    options = parser.parse_args(shlex.split(text.strip()[4:]))
    launch = argparse.Namespace(**vars(args))
    vars(launch).update(vars(options))
    return launch


def start_worker(runtime, args, config, proposal, argv=None):
    policy, active = runtime._active(proposal)
    if not active:
        raise ValueError("The selected Constitution is not active")
    validate_workspace_selection(args, policy.workspace)
    agent = (config or {}).get("agent", {})
    command = list(argv or agent.get("command", []))
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("Configure an installed agent with jev setup, or use jev run -- COMMAND")
    source = runtime.store.intent_source(policy.intent_id)
    task = worker_brief(policy, source)
    command = [part.replace("{task}", task) for part in command]
    provider = None
    if not args.demo and not getattr(args, "no_provider", False):
        settings = generative_settings(config or {}, "worker")
        if settings is not None:
            provider = {
                **settings,
                "api_key": model_credential(config or {}, settings, "Worker model", "worker"),
            }
    runner = supervisor(runtime, args)
    run_id = runner.run(
        proposal,
        command,
        provider=provider,
        interactive=getattr(args, "interactive", False),
        tool_roots=tuple(agent.get("tool_roots", [])),
    )
    changes = runner.preview(run_id)
    print("\nWorker exited successfully. Proposed project changes:")
    for change in changes:
        display("  " + change["action"] + " " + change["path"])
    print("Nothing has been applied to the project yet. Session: " + run_id)
    if confirm_operation(
        "Run the acceptance checks and apply this reviewed result to the project?"
    ):
        applied = runner.publish(run_id)
        print(
            "Applied "
            + str(len(applied))
            + " reviewed file changes; recorded acceptance evidence for this result."
        )
    else:
        print("Saved for review. Use " + command_prefix(args) + " apply " + run_id + " when ready.")
    return run_id


def interview(store, runtime, model, args, config=None):
    if args.session:
        session = OnboardingSession(store, runtime, model, args.session)
    else:
        workspace = str(Path(args.workspace or ".").resolve())
        matching = [
            (identifier, value)
            for identifier, value in store.records("onboarding_session")
            if not args.explicit_workspace or value["workspace"] == workspace
        ]
        if matching and not args.new:
            session = OnboardingSession(store, runtime, model, matching[-1][0])
        else:
            if not args.explicit_workspace:
                Path(workspace).mkdir(exist_ok=True, mode=0o700)
            session = OnboardingSession.start(store, runtime, model, workspace)
    validate_workspace_selection(args, session.settings["workspace"])
    print("Session: " + session.id)
    display("Project: " + session.settings["workspace"])
    print("Talk naturally. /review shows the proposal; /approve records your authorization.")
    print(
        "/run starts your configured worker; /handoff exports a brief; /retry retries a failed reply; /done pauses."
    )
    print("Launch options: /run [--no-provider] [--interactive] [--] COMMAND [ARG ...]")
    transcript = session.transcript()
    if len(transcript) == 1:
        display(session.settings["opening"])
    elif session.pending():
        print("Your last message is saved and awaiting a model response. Use /retry to continue.")
    else:
        display(json.loads(transcript[-1]["content"])["question"])
    while True:
        try:
            text = input("You: ")
        except (EOFError, KeyboardInterrupt):
            print(
                "\nConversation saved. Resume with "
                + command_prefix(args)
                + " onboard --session "
                + session.id
            )
            return 0
        if text.strip() in {"/done", "/quit"}:
            print(
                "Conversation saved. Resume with "
                + command_prefix(args)
                + " onboard --session "
                + session.id
            )
            return 0
        if not text.strip():
            continue
        proposal = session.latest_proposal()
        if text.strip() == "/run" or text.startswith("/run "):
            try:
                if proposal is None:
                    raise ValueError("Finish and approve the current Constitution first")
                launch = interview_run_arguments(text, args)
                start_worker(runtime, launch, config, proposal, launch.commandline)
            except (ValueError, RuntimeError) as exc:
                display(str(exc))
            continue
        if text.strip() in {"/review", "/approve", "/handoff"}:
            if proposal is None:
                print("Continue the interview until there is a current proposal to review.")
                continue
            if text.strip() == "/review":
                review(runtime, proposal)
            elif text.strip() == "/approve":
                human_approval(runtime, proposal)
            else:
                try:
                    content = session.handoff()
                    if args.demo:
                        content = (
                            "# DEMONSTRATION ONLY — simulated model and JEV judgments\n\n" + content
                        )
                    output = Path(args.state).resolve().parent / ("next-phase-" + proposal + ".md")
                    with output.open("x", encoding="utf-8") as stream:
                        output.chmod(0o600)
                        stream.write(content)
                    print("Next-phase brief saved: " + str(output))
                except FileExistsError:
                    print("The next-phase brief for this policy is already saved: " + str(output))
                except ValueError as exc:
                    display(str(exc))
            continue
        if text.startswith("/") and text.strip() != "/retry" and not text.startswith("/context "):
            print("Available commands: /review, /approve, /run, /handoff, /retry, /context, /done")
            continue
        try:
            if text.startswith("/context "):
                result = session.add_context(text[len("/context ") :])
            else:
                result = session.retry() if text.strip() == "/retry" else session.reply(text)
        except Exception as exc:
            if isinstance(exc, (OnboardingServiceError, ConnectionRequired)):
                display(str(exc))
            else:
                print("The reply could not be completed (" + type(exc).__name__ + ").")
            print("/retry retries a recorded unanswered message; /done pauses the conversation.")
            continue
        display(result["response"]["question"])
        if result["proposal_id"]:
            review(runtime, result["proposal_id"])
            print(
                "Describe changes in your own words, or enter /approve when this matches your intent."
            )


def main(argv=None):
    parser = argparse.ArgumentParser(description="JEVRUNTIME supervised terminal agents")
    parser.add_argument(
        "--output", help="Existing folder in your user space for this job and its records"
    )
    parser.add_argument("--config", help="Explicit provider settings file (advanced)")
    parser.add_argument("--state", help="Explicit history file (advanced)")
    parser.add_argument("--run-dir", help="Private worker storage outside the project")
    parser.add_argument("--demo", action="store_true", help="Explicit offline demonstration")
    parser.set_defaults(workspace=None, session=None, new=False)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("setup")
    onboard = sub.add_parser("onboard")
    onboard.add_argument("--workspace")
    onboard.add_argument("--session", help="Resume a particular interview")
    onboard.add_argument("--new", action="store_true", help="Start a separate interview")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--kind", default="policy_proposal")
    approve = sub.add_parser("approve")
    approve.add_argument("proposal")
    decide = sub.add_parser("decide")
    decide.add_argument("proposal")
    decide.add_argument("action")
    decide.add_argument("target")
    evidence = sub.add_parser("collect-file")
    evidence.add_argument("proposal")
    evidence.add_argument("requirement")
    evidence.add_argument("target")
    complete = sub.add_parser("complete")
    complete.add_argument("proposal")
    complete.add_argument(
        "--details", action="store_true", help="Explain missing or stale completion evidence"
    )
    sub.add_parser("doctor", help="Check real worker isolation support")
    capabilities = sub.add_parser(
        "capabilities", help="Describe actual control boundaries without model calls"
    )
    capabilities.add_argument(
        "--probe", action="store_true", help="Also run the local namespace check"
    )
    sub.add_parser("status", help="Show recorded worker sessions and results")
    stop = sub.add_parser("stop", help="Stop a running worker without applying its changes")
    stop.add_argument("run_id")
    revoke = sub.add_parser("revoke", help="Immediately suspend this Constitution's authority")
    revoke.add_argument("proposal")
    run = sub.add_parser("run", help="Launch a terminal worker under the active Constitution")
    run.add_argument("--proposal")
    run.add_argument("--workspace")
    run.add_argument("--session")
    worker_arguments(run)
    for command in ("verify", "apply"):
        item = sub.add_parser(command)
        item.add_argument("run_id")
    reconcile = sub.add_parser("reconcile", help="Inspect an active or interrupted publication")
    reconcile.add_argument("run_id")
    reconcile.add_argument(
        "--abandon",
        action="store_true",
        help="Human-reviewed closure without file changes or completion",
    )
    recover = sub.add_parser("recover")
    recover.add_argument("proposal")
    constitution = sub.add_parser("constitution")
    constitution.add_argument(
        "operation", choices=["list", "export", "import", "rollback", "compose"]
    )
    constitution.add_argument("values", nargs="*")
    constitution.add_argument("--name", default="Composed Constitution")
    git = sub.add_parser("git", help="Human-approved host Git operations")
    git.add_argument("operation", choices=["status", "commit", "push", "reconcile"])
    git.add_argument("--proposal")
    git.add_argument("--attempt", help="Exact Git attempt ID from git reconcile")
    git.add_argument(
        "--abandon",
        action="store_true",
        help="Close reviewed uncertainty without changing files or restoring authority",
    )
    git.add_argument("--message", default="")
    git.add_argument("--remote", default="")
    git.add_argument("--branch", default="")
    git.add_argument(
        "--allow-http",
        action="store_true",
        help="Explicitly permit a trusted plaintext HTTP Git remote",
    )
    args = parser.parse_args(argv)
    args.command = args.command or "onboard"
    try:
        if args.command == "capabilities":
            from .capabilities import capability_report

            result = capability_report(probe=args.probe)
            print(json.dumps(result, indent=2))
            return 1 if args.probe and not result["host_probe"]["available"] else 0
        if args.command == "doctor":
            from .sandbox import Sandbox

            result = Sandbox().check()
            print(json.dumps(result, indent=2))
            return 0 if result["available"] else 1
        resolve_destinations(args)
        if args.command == "setup":
            validate_settings_destination(args)
            setup(args.config)
            return 0
        with ExitStack() as stack:
            state = args.ledger_path
            store = Store(state, readonly=readonly_command(args))
            stack.callback(store.close)
            if args.command == "git" and args.operation == "reconcile":
                from .git_operations import abandon_git, git_attempts, git_report

                runtime = Runtime(store, None)
                if not args.attempt:
                    if args.abandon:
                        raise ValueError(
                            "Git abandonment requires an exact --attempt ID from git reconcile"
                        )
                    print(
                        json.dumps(
                            [
                                {
                                    "attempt_id": a["attempt_id"],
                                    "request_id": a["request_id"],
                                    "proposal_id": a["request"]["proposal_id"],
                                    "workspace": a["request"]["payload"]["root"],
                                    "operation": a["request"]["payload"]["operation"],
                                    "legacy": a["legacy"],
                                    "unresolved": a["unresolved"],
                                    "abandoned": a["abandoned"],
                                    "receipt_id": a["receipt_id"],
                                }
                                for a in git_attempts(runtime)
                            ],
                            indent=2,
                        )
                    )
                    return 0
                result = (
                    abandon_git(runtime, args.attempt, confirm_operation)
                    if args.abandon
                    else git_report(runtime, args.attempt)
                )
                print(json.dumps(result, indent=2))
                return 0
            if args.command == "reconcile":
                from .reconciliation import publication_report

                runtime = Runtime(store, None)
                result = (
                    supervisor(runtime, args).abandon_publication(args.run_id)
                    if args.abandon
                    else publication_report(runtime, args.run_id)
                )
                print(json.dumps(result, indent=2))
                return 0
            if args.command == "inspect":
                print(json.dumps(store.records(args.kind), indent=2))
                return 0
            if args.command == "status":
                print(
                    json.dumps(
                        store.events(
                            (
                                "runtime_session",
                                "runtime_result",
                                "verification_started",
                                "verification_failed",
                                "run_verification",
                                "publication_started",
                                "publication_failed",
                                "publication_abandoned",
                                "publication",
                                "completion",
                                "recovery_started",
                                "recovery_approval",
                                "recovery_receipt",
                                "recovery_verification",
                                "recovery_failed",
                                "autonomy",
                                "git_attempt_started",
                                "git_failure",
                                "git_receipt",
                                "git_abandoned",
                            )
                        ),
                        indent=2,
                    )
                )
                return 0
            if args.command == "stop":
                store.get(args.run_id, "runtime_session")
                store.append(
                    "runtime_stop", {"run_id": args.run_id, "reason": "Human requested stop"}
                )
                print(
                    "Stop requested. The supervisor will terminate this worker; its files remain staged."
                )
                return 0
            if args.command == "revoke":
                host = Runtime(store, None)
                host.suspend(host.constitution_id(args.proposal), "Human revoked authority")
                print(
                    "Constitution authority suspended. Running workers will stop and pending publication is blocked."
                )
                return 0
            if args.command == "constitution" and args.operation == "list":
                from .constitutions import history

                print(json.dumps(history(Runtime(store, None)), indent=2))
                return 0
            config = None
            if args.demo:
                print(
                    "DEMONSTRATION: simulated model replies and judgments; separate demo records."
                )
                judge, model = FakeJudge(), FakeOnboarding()
            else:
                if (
                    not Path(args.config).exists()
                    and args.command == "onboard"
                    and not store.records("onboarding_session")
                    and sys.stdin.isatty()
                ):
                    validate_settings_destination(args)
                    setup(args.config)

                def get_config():
                    nonlocal config
                    if config is None:
                        config = (
                            load(args.config)
                            if Path(args.config).exists()
                            else {
                                "onboarding": {"provider": "unconfigured"},
                                "judgment": {"provider": "unconfigured"},
                                "agent": {"provider": "unconfigured"},
                            }
                        )
                    return config

                def connect_judge():
                    j = judgment_settings(get_config())
                    client = stack.enter_context(
                        judgment_client(
                            j,
                            credential(
                                j, "JEV (" + j.get("provider", "typesafe") + ")", purpose="judgment"
                            ),
                        )
                    )
                    return TypeSafeJudge(client)

                judge = LazyJudge(connect_judge)
                model = None
                if args.command == "onboard":
                    config = get_config()
                    model = ConnectedOnboarding(config)
                    if config["onboarding"].get("provider") == "unconfigured":
                        print(
                            "Onboarding is not connected. Your messages will be saved without model replies; use "
                            + command_prefix(args)
                            + " setup to select a connection."
                        )
                elif args.command == "run":
                    config = get_config()
            runtime = Runtime(store, judge)
            if args.command == "onboard":
                return interview(store, runtime, model, args, config)
            if args.command == "run":
                start_worker(
                    runtime, args, config, selected_policy(runtime, args), args.commandline
                )
                return 0
            if args.command in {"verify", "apply"}:
                runner = supervisor(runtime, args)
                result = (
                    runner.verify(args.run_id)
                    if args.command == "verify"
                    else runner.publish(args.run_id)
                )
                print(json.dumps(result, indent=2))
                return 0
            if args.command == "recover":
                supervisor(runtime, args).recover(args.proposal)
                return 0
            if args.command == "constitution":
                from .constitutions import compose, export_policy, import_policy, rollback

                if args.operation == "export" and len(args.values) == 2:
                    print(export_policy(runtime, *args.values))
                    return 0
                if args.operation == "import" and len(args.values) == 1:
                    proposal = import_policy(runtime, args.values[0])
                elif args.operation == "rollback" and len(args.values) == 1:
                    proposal = rollback(runtime, args.values[0])
                elif args.operation == "compose" and len(args.values) >= 2:
                    proposal = compose(runtime, args.values, args.name)
                else:
                    raise ValueError(
                        "Choose a valid Constitution operation and its file/proposal arguments"
                    )
                human_approval(runtime, proposal)
                return 0
            if args.command == "git":
                from .git_gateway import GitGateway

                proposal = selected_policy(runtime, args)
                policy, active = runtime._active(proposal)
                if not active:
                    raise ValueError("Git operation needs an active Constitution")
                gateway = GitGateway(
                    Path(policy.workspace),
                    allow_http=args.allow_http,
                    protected_paths=policy.protected_paths,
                    policy_hash=digest(policy.as_dict()),
                )
                plan = gateway.preview(
                    args.operation, message=args.message, remote=args.remote, branch=args.branch
                )
                # Status is read-only; write operations always bind human approval to this plan.
                runner = supervisor(runtime, args)
                if args.operation == "status":
                    if "read" not in policy.actions:
                        raise ValueError("This Constitution does not grant repository reads")
                    print(json.dumps(plan, indent=2))
                    return 0
                member_directory(policy.workspace)
                username = token = None
                if args.operation == "push" and sys.stdin.isatty():
                    if plan["remote_url"].startswith("http://"):
                        display(
                            "This explicitly selected Git destination uses plaintext HTTP: "
                            + plan["remote_url"]
                        )
                    username = input("HTTPS Git username (blank for anonymous): ").strip() or None
                    if username:
                        token = getpass.getpass("HTTPS Git token (hidden; this push only): ")
                from .git_operations import execute_git

                result = execute_git(
                    runner, proposal, gateway, plan, username=username, token=token
                )
                print(json.dumps(result, indent=2))
                return 0
            if args.command == "approve":
                return 0 if human_approval(runtime, args.proposal) else 1
            if args.command == "decide":
                decision = runtime.decide(args.proposal, args.action, args.target)
                print(decision)
                return (
                    0
                    if decision == Decision.ALLOW
                    else 3
                    if decision == Decision.APPROVAL_REQUIRED
                    else 1
                )
            elif args.command == "collect-file":
                print(runtime.collect_file(args.proposal, args.requirement, args.target))
            elif args.command == "complete":
                result = runtime.completion_status(args.proposal)
                print(
                    json.dumps(result, indent=2)
                    if args.details
                    else "passed"
                    if result["passed"]
                    else "not_complete"
                )
                return 0 if result["passed"] else 1
        return 0
    except DestinationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("\nStopped. Recorded conversation and approvals are preserved.")
        return 0
    except Exception as exc:
        detail = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        print(
            "Operation stopped: " + detail,
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
