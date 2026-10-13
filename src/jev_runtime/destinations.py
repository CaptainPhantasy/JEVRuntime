"""Resolve explicit member output paths before setup, prompts, or persistent writes."""

import os
import shlex
from pathlib import Path

MISSING_DESTINATION = (
    "Error: No destination folder specified. Supply an output path within your user space."
)


class DestinationError(ValueError):
    pass


def member_directory(value, *, writable=True):
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise DestinationError(
            "Error: Create the destination folder in your user space first: " + str(path)
        )
    if writable and path.stat().st_uid != os.getuid():
        raise DestinationError(
            "Error: Choose a destination folder owned by your calling account: " + str(path)
        )
    if writable and not os.access(path, os.W_OK | os.X_OK):
        raise DestinationError(
            "Error: Grant your account write and traversal access to the destination folder: "
            + str(path)
        )
    if not writable and not os.access(path, os.R_OK | os.X_OK):
        raise DestinationError(
            "Error: Grant your account read and traversal access to the project: " + str(path)
        )
    return path


def contained(root, path, *, writable=True):
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise DestinationError(
            "Error: An output path points outside the selected folder: " + str(path)
        )
    # Existing nested directories or files must not belong to another account.
    for candidate in (resolved, *resolved.parents):
        if candidate == root:
            break
        if writable and candidate.exists() and candidate.stat().st_uid != os.getuid():
            raise DestinationError(
                "Error: An output path belongs to another account: " + str(candidate)
            )
    return resolved


def readonly_command(args):
    return (
        (args.command == "reconcile" and not args.abandon)
        or (args.command == "git" and args.operation == "reconcile" and not args.abandon)
        or args.command in {"status", "inspect", "complete"}
        or (args.command == "constitution" and args.operation in {"list", "export"})
    )


def resolve_destinations(args):
    """One folder by default; explicitly supplied legacy file paths remain usable."""
    args.explicit_run_dir = args.run_dir
    args.explicit_workspace = bool(getattr(args, "workspace", None))
    read_only = readonly_command(args)
    if args.output:
        if args.state or args.config or args.run_dir:
            raise DestinationError(
                "Error: --output cannot be combined with --state, --config or --run-dir. Use one folder or explicit paths."
            )
        root = member_directory(args.output, writable=not read_only)
        args.output = str(root)
        args.config = str(contained(root, root / ".jev" / "providers.toml", writable=not read_only))
        if args.command == "setup":
            return args
        args.state = str(contained(root, root / ".jev" / "ledger.sqlite", writable=not read_only))
        args.run_dir = str(contained(root, root / ".jev" / "workers", writable=not read_only))
        default_project = contained(root, root / "project", writable=not read_only)
    else:
        selected = args.config if args.command == "setup" and args.config else args.state
        if not selected:
            raise DestinationError(MISSING_DESTINATION)
        selected_path = Path(selected).expanduser().absolute()
        root = selected_path.parent.resolve()
        if args.command == "setup":
            destination = (
                Path(args.config).expanduser().absolute()
                if args.config
                else root / "providers.toml"
            )
            args.config = str(contained(member_directory(root), destination))
            return args
        if not readonly_command(args):
            root = member_directory(root)
            contained(root, selected_path)
        args.state = (
            str(Path(args.state).expanduser().absolute())
            if args.state
            else str(root / "ledger.sqlite")
        )
        args.config = (
            str(Path(args.config).expanduser().absolute())
            if args.config
            else str(root / "providers.toml")
        )
        args.run_dir = (
            str(member_directory(args.run_dir)) if args.run_dir else str(root / "workers")
        )
        if not args.explicit_run_dir and args.command not in {"verify", "apply", "reconcile"}:
            args.run_dir = str(contained(root, Path(args.run_dir), writable=not read_only))
        default_project = contained(root, root / "project", writable=not read_only)
    if getattr(args, "workspace", None):
        args.workspace = str(member_directory(args.workspace, writable=False))
    elif args.command in {"onboard", "run"}:
        args.workspace = str(default_project)
    args.ledger_path = str(
        contained(root, Path(args.state + (".demo" if args.demo else "")), writable=not read_only)
    )
    if args.command == "constitution" and args.operation == "export" and len(args.values) == 2:
        destination = Path(args.values[1]).expanduser().absolute()
        args.values[1] = str(contained(member_directory(destination.parent), destination))
    return args


def command_prefix(args):
    if getattr(args, "output", None):
        command = "jev --output " + shlex.quote(args.output)
    else:
        command = "jev --state " + shlex.quote(args.state) + " --config " + shlex.quote(args.config)
        if getattr(args, "explicit_run_dir", None):
            command += " --run-dir " + shlex.quote(str(args.run_dir))
    return command + (" --demo" if args.demo else "")


def validate_settings_destination(args):
    # Existing settings are read inputs; setup is the only configuration writer.
    if not args.output:
        destination = Path(args.config)
        contained(member_directory(destination.parent), destination)


def validate_workspace_selection(args, recorded):
    """Validate the actual project used by a resumed session or selected policy."""
    root = member_directory(recorded, writable=False)
    if getattr(args, "explicit_workspace", False) and Path(args.workspace) != root:
        raise DestinationError(
            "Error: --workspace differs from the selected session or Constitution. "
            "Use its recorded project, or start a new interview with --new."
        )
    return root
