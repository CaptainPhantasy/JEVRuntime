"""Staged workspaces and deterministic promotion of reviewed file changes.

Only a copy is given to a worker. Git metadata, runtime state, common credential
files, virtual environments, dependencies, and disposable caches are excluded. Extra
private paths can be supplied through ``protected_paths`` (relative paths/globs).
The caller owns policy approval: this module never interprets approval itself.
Build output directories remain ordinary governed artifacts. Text review is
limited to complete UTF-8 files within the supplied bound; binary and larger
artifacts fail closed until a suitable independent review channel is available.

All traversal uses directory descriptors and O_NOFOLLOW on POSIX. This prevents
symlink redirection; it is not isolation from a hostile process running as the
same host user. Promotion preflights every change and prepares replacements and
rollback copies before modifying files. A caught I/O failure triggers rollback,
but a power loss/process kill is not an atomic filesystem transaction. Callers
must record successful promotion or the error's remaining ``applied`` entries.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

MAX_BYTES = 64 * 1024 * 1024
MAX_FILES = 20_000
_PRIVATE = {
    ".crush",
    "crush.json",
    ".crush.json",
    "crushrc",
    ".crushrc",
    ".git",
    ".jev",
    ".env",
    ".ssh",
    ".aws",
    ".azure",
    ".kube",
    ".gnupg",
    ".config",
    ".codex",
    ".claude",
    "auth.json",
    ".docker",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    ".gitconfig",
    ".envrc",
    ".pgpass",
    ".my.cnf",
    "credentials",
    "credential",
    "secrets",
    ".secrets",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "authorized_keys",
    "known_hosts",
}
_PRIVATE_GLOBS = (
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "credentials.*",
    "credential.*",
    "secrets.*",
    "service-account*.json",
    "service_account*.json",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".jev-promotion-*",
)
_CACHES = {
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    ".nox",
    ".cache",
    ".coverage",
}


def protected_path_rules():
    """Expose actual baseline exclusions to policy review; no duplicated list."""
    return {"names_at_any_depth": sorted(_PRIVATE), "patterns": list(_PRIVATE_GLOBS)}


class WorkspaceError(RuntimeError):
    def __init__(self, message, *, applied=None, rollback_errors=None):
        super().__init__(message)
        self.applied = list(applied or [])
        self.rollback_errors = list(rollback_errors or [])


class WorkspaceChanged(WorkspaceError):
    """A live observation raced ordinary file replacement; no snapshot was accepted."""


def _parts(path):
    if not isinstance(path, str) or not path or path.startswith("/"):
        raise WorkspaceError("Workspace paths must be nonempty and relative")
    parts = path.split("/")
    if any(p in {"", ".", ".."} for p in parts) or "\\" in path:
        raise WorkspaceError(f"Unsafe workspace path: {path!r}")
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise WorkspaceError("Control characters are not allowed in workspace paths")
    # Avoid names that alias private names on common case-insensitive filesystems.
    if any(p != p.rstrip(" .") or ":" in p for p in parts):
        raise WorkspaceError(f"Ambiguous workspace path: {path!r}")
    return parts


def _patterns(protected_paths):
    if isinstance(protected_paths, (str, bytes)):
        raise WorkspaceError("protected_paths must be a sequence of relative paths/globs")
    patterns = []
    for pattern in protected_paths:
        if not isinstance(pattern, str):
            raise WorkspaceError("Protected paths must be strings")
        _parts(pattern.rstrip("/"))
        patterns.append(pattern.rstrip("/").casefold())
    return tuple(patterns)


def _excluded(path, patterns):
    parts = _parts(path)
    folded = [part.casefold() for part in parts]
    if any(
        part in _PRIVATE or any(fnmatch.fnmatchcase(part, g) for g in _PRIVATE_GLOBS)
        for part in folded
    ):
        return "private"
    for i in range(1, len(parts) + 1):
        prefix = "/".join(folded[:i])
        if any(
            fnmatch.fnmatchcase(prefix, p) or fnmatch.fnmatchcase(folded[i - 1], p)
            for p in patterns
        ):
            return "private"
    if any(part in _CACHES or part.endswith(".egg-info") for part in folded):
        return "cache"
    return None


def _usable(path, patterns):
    if _excluded(path, patterns):
        raise WorkspaceError(f"Protected workspace path: {path}")


def validate_path(path: str, protected_paths=()) -> None:
    """Reject unsafe/protected relative paths using the same rules as staging."""
    _usable(path, _patterns(protected_paths))


def _flags():
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise WorkspaceError("Safe workspace operations require POSIX O_NOFOLLOW support")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _absolute(path):
    path = Path(path)
    if ".." in path.parts:
        raise WorkspaceError("Workspace roots cannot contain parent traversal")
    return path.absolute()


@contextmanager
def _root(path, *, create=False):
    path = _absolute(path)
    fd = os.open("/", _flags())
    try:
        for component in path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, _flags(), dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    except OSError as exc:
        raise WorkspaceError(f"Cannot safely access workspace root {path}: {exc.strerror}") from exc
    finally:
        os.close(fd)


@contextmanager
def _parent(root_fd, path, *, create=False, created=None):
    parts = _parts(path)
    fd = os.dup(root_fd)
    prefix = []
    try:
        for component in parts[:-1]:
            prefix.append(component)
            if create:
                try:
                    os.mkdir(component, mode=0o755, dir_fd=fd)
                    if created is not None:
                        created.append("/".join(prefix))
                except FileExistsError:
                    pass
            next_fd = os.open(component, _flags(), dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _stamp(st):
    return (
        st.st_dev,
        st.st_ino,
        st.st_mode,
        st.st_nlink,
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
    )


def _file(parent_fd, name, *, max_bytes, content=False):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise WorkspaceError(f"Only independent regular files are allowed: {name}")
        if before.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
            raise WorkspaceError(f"Special permission bits are not allowed: {name}")
        if before.st_size > max_bytes:
            raise WorkspaceError(f"Workspace file exceeds the byte limit: {name}")
        sha = hashlib.sha256()
        chunks = []
        size = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, max_bytes - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise WorkspaceError(f"Workspace file exceeds the byte limit: {name}")
            sha.update(chunk)
            if content:
                chunks.append(chunk)
        after = os.fstat(fd)
        # Detect replacement, linking, or a concurrent edit during the read.
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _stamp(before) != _stamp(after) or _stamp(after) != _stamp(current):
            raise WorkspaceChanged(f"Workspace file changed while reading: {name}")
        record = {"sha256": sha.hexdigest(), "size": size, "mode": stat.S_IMODE(after.st_mode)}
        return record, b"".join(chunks) if content else None
    finally:
        os.close(fd)


def _scan(root_fd, *, max_bytes, patterns, reject_private=False):
    if type(max_bytes) is not int or max_bytes < 0:
        raise WorkspaceError("max_bytes must be a nonnegative integer")
    result = {}
    used = 0

    def visit(fd, prefix):
        nonlocal used
        with os.scandir(fd) as listing:
            names = sorted(entry.name for entry in listing)
        for name in names:
            path = f"{prefix}/{name}" if prefix else name
            excluded = _excluded(path, patterns)
            if excluded:
                # A supervisor may initialize an isolated Git repository for
                # harnesses that require one. Its metadata is never promoted.
                private_git = ".git" in path.split("/")
                if excluded == "private" and reject_private and not private_git:
                    raise WorkspaceError(f"Worker created a protected path: {path}")
                continue
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, _flags(), dir_fd=fd)
                try:
                    if (
                        os.fstat(child).st_ino != info.st_ino
                        or os.fstat(child).st_dev != info.st_dev
                    ):
                        raise WorkspaceChanged(f"Directory changed while scanning: {path}")
                    visit(child, path)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if len(result) >= MAX_FILES:
                    raise WorkspaceError("Workspace has too many files")
                record, _ = _file(fd, name, max_bytes=max_bytes - used)
                used += record["size"]
                result[path] = record
            else:
                raise WorkspaceError(f"Symlinks and special files are not allowed: {path}")

    try:
        visit(root_fd, "")
    except (OSError, RecursionError) as exc:
        if isinstance(exc, FileNotFoundError):
            raise WorkspaceChanged("Workspace changed during observation") from exc
        raise WorkspaceError("Workspace changed or contains an inaccessible/unsafe path") from exc
    return result


def snapshot(root: Path, *, max_bytes=MAX_BYTES, protected_paths=()) -> dict[str, dict]:
    """Hash allowed regular files; omit private paths and reproducible caches."""
    patterns = _patterns(protected_paths)
    with _root(root) as fd:
        return _scan(fd, max_bytes=max_bytes, patterns=patterns)


def _temporary(parent_fd, data, mode):
    name = f".jev-promotion-{uuid4().hex}.tmp"
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write(data)
            output.flush()
        os.fchmod(fd, mode)
        os.fsync(fd)
    except BaseException:
        os.unlink(name, dir_fd=parent_fd)
        raise
    finally:
        os.close(fd)
    return name


def stage(
    source: Path, destination: Path, *, max_bytes=MAX_BYTES, protected_paths=()
) -> dict[str, dict]:
    """Copy allowed source files into a new or empty private staging directory."""
    source, destination = _absolute(source), _absolute(destination)
    if source == destination or source in destination.parents or destination in source.parents:
        raise WorkspaceError("Source and staged workspaces must not overlap")
    patterns = _patterns(protected_paths)
    with _root(source) as source_fd, _root(destination, create=True) as destination_fd:
        if os.listdir(destination_fd):
            raise WorkspaceError("Staging destination must be empty")
        baseline = _scan(source_fd, max_bytes=max_bytes, patterns=patterns)
        try:
            for path, expected in baseline.items():
                with _parent(source_fd, path) as (parent_fd, name):
                    record, data = _file(parent_fd, name, max_bytes=max_bytes, content=True)
                if record != expected:
                    raise WorkspaceError(f"Source changed while staging: {path}")
                with _parent(destination_fd, path, create=True) as (parent_fd, name):
                    temporary = _temporary(parent_fd, data, record["mode"])
                    try:
                        # No destination file can be silently overwritten.
                        os.link(
                            temporary,
                            name,
                            src_dir_fd=parent_fd,
                            dst_dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    finally:
                        os.unlink(temporary, dir_fd=parent_fd)
            if _scan(source_fd, max_bytes=max_bytes, patterns=patterns) != baseline:
                raise WorkspaceError("Source changed while staging")
        except OSError as exc:
            raise WorkspaceError("Could not safely create the staged workspace") from exc
        return baseline


def _baseline(baseline, patterns):
    if not isinstance(baseline, dict) or len(baseline) > MAX_FILES:
        raise WorkspaceError("Invalid workspace baseline")
    for path, record in baseline.items():
        _usable(path, patterns)
        if (
            not isinstance(record, dict)
            or set(record) != {"sha256", "size", "mode"}
            or not isinstance(record["sha256"], str)
            or len(record["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in record["sha256"])
            or type(record["size"]) is not int
            or record["size"] < 0
            or type(record["mode"]) is not int
            or not 0 <= record["mode"] <= 0o777
        ):
            raise WorkspaceError(f"Invalid baseline record: {path}")


def _difference(current, baseline):
    changes = []
    for path in sorted(set(baseline) | set(current)):
        before, after = baseline.get(path), current.get(path)
        if before != after:
            changes.append(
                {
                    "path": path,
                    "action": "write" if after else "delete",
                    "before": before["sha256"] if before else None,
                    "after": after["sha256"] if after else None,
                    "size": after["size"] if after else 0,
                    "mode": after["mode"] if after else None,
                }
            )
    return changes


def diff(root: Path, baseline: dict, *, max_bytes=MAX_BYTES, protected_paths=()) -> list[dict]:
    patterns = _patterns(protected_paths)
    _baseline(baseline, patterns)
    with _root(root) as fd:
        current = _scan(fd, max_bytes=max_bytes, patterns=patterns, reject_private=True)
    return _difference(current, baseline)


def _change(change, patterns):
    if (
        not isinstance(change, dict)
        or not {"path", "action", "before", "after", "size"} <= set(change)
        or not isinstance(change.get("path"), str)
    ):
        raise WorkspaceError("Invalid change record")
    _usable(change["path"], patterns)
    if change.get("action") not in {"write", "delete"}:
        raise WorkspaceError("Unknown workspace change action")
    if type(change.get("size")) is not int or change["size"] < 0:
        raise WorkspaceError("Invalid change size")
    for key in ("before", "after"):
        value = change.get(key)
        if value is not None and (
            not isinstance(value, str)
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise WorkspaceError("Invalid change content hash")
    if change["action"] == "write" and change["after"] is None:
        raise WorkspaceError("A write requires the staged content hash")
    if change["action"] == "delete" and (
        change["before"] is None or change["after"] is not None or change["size"] != 0
    ):
        raise WorkspaceError("Invalid deletion record")


def read_change(root: Path, change, max_bytes=32000, *, protected_paths=()) -> str:
    """Read a complete UTF-8 change for judgment; never truncate an oversized file."""
    patterns = _patterns(protected_paths)
    _change(change, patterns)
    if change["action"] != "write":
        raise WorkspaceError("A deletion has no staged content to read")
    if type(max_bytes) is not int or max_bytes < 0:
        raise WorkspaceError("max_bytes must be a nonnegative integer")
    try:
        with _root(root) as fd, _parent(fd, change["path"]) as (parent_fd, name):
            record, data = _file(parent_fd, name, max_bytes=max_bytes, content=True)
        if record["sha256"] != change["after"] or record["size"] != change["size"]:
            raise WorkspaceError("Staged content changed after review")
        text = data.decode("utf-8")
        if any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in text):
            raise WorkspaceError("Binary/control-byte content cannot receive text-only review")
        return text
    except UnicodeDecodeError as exc:
        raise WorkspaceError("Binary/non-UTF-8 content cannot receive text-only review") from exc
    except OSError as exc:
        raise WorkspaceError("Cannot safely read staged change") from exc


def _existing(root_fd, path, *, max_bytes, content=False):
    try:
        with _parent(root_fd, path) as (parent_fd, name):
            return _file(parent_fd, name, max_bytes=max_bytes, content=content)
    except FileNotFoundError:
        return None, None


def apply_changes(
    source: Path,
    staged: Path,
    baseline: dict,
    changes: list[dict],
    *,
    max_bytes=MAX_BYTES,
    protected_paths=(),
) -> list[dict]:
    """Promote the whole reviewed diff, with conflict detection and best-effort rollback.

    This function must only be called after deterministic policy and human gates.
    No symlinks, directory replacement, recursive deletion, or unreviewed changes
    are promoted. An error may expose ``applied`` if rollback could not finish.
    """
    source, staged = _absolute(source), _absolute(staged)
    if source == staged or source in staged.parents or staged in source.parents:
        raise WorkspaceError("Source and staged workspaces must not overlap")
    patterns = _patterns(protected_paths)
    _baseline(baseline, patterns)
    if not isinstance(changes, list):
        raise WorkspaceError("Changes must be a list")
    for change in changes:
        _change(change, patterns)
    if len({c["path"] for c in changes}) != len(changes):
        raise WorkspaceError("Duplicate workspace change")
    with _root(source) as source_fd, _root(staged) as staged_fd:
        current = _scan(staged_fd, max_bytes=max_bytes, patterns=patterns, reject_private=True)
        expected_changes = _difference(current, baseline)
        if sorted(changes, key=lambda c: c["path"]) != expected_changes:
            raise WorkspaceError("Approved changes do not match the complete staged diff")
        prepared = []
        created = []
        applied = []
        rollback_errors = []
        try:
            # Read and verify every target before even creating directories or temps.
            originals = {}
            for change in expected_changes:
                path = change["path"]
                original, data = _existing(source_fd, path, max_bytes=max_bytes, content=True)
                if original != baseline.get(path):
                    raise WorkspaceError(f"Host changed since staging: {path}")
                originals[path] = (original, data)
            for change in expected_changes:
                path = change["path"]
                data = None
                if change["action"] == "write":
                    with _parent(staged_fd, path) as (parent_fd, name):
                        record, data = _file(parent_fd, name, max_bytes=max_bytes, content=True)
                    if record != current[path]:
                        raise WorkspaceError(f"Staged file changed before promotion: {path}")
                with _parent(source_fd, path, create=True, created=created) as (parent_fd, name):
                    item = {
                        "change": change,
                        "fd": os.dup(parent_fd),
                        "name": name,
                        "new": None,
                        "backup": None,
                    }
                    prepared.append(item)
                    if data is not None:
                        item["new"] = _temporary(parent_fd, data, current[path]["mode"])
                    original, old_data = originals[path]
                    if original is not None:
                        item["backup"] = _temporary(parent_fd, old_data, original["mode"])
            # Recheck after preparation. Fail before any replacement if a target drifted.
            for item in prepared:
                path = item["change"]["path"]
                original, _ = _existing(source_fd, path, max_bytes=max_bytes)
                if original != baseline.get(path):
                    raise WorkspaceError(f"Host changed before promotion: {path}")
            if (
                _scan(staged_fd, max_bytes=max_bytes, patterns=patterns, reject_private=True)
                != current
            ):
                raise WorkspaceError("Staged workspace changed before promotion")
            for item in prepared:
                change, fd, name = item["change"], item["fd"], item["name"]
                # The parent descriptor must still identify its path in the source.
                with _parent(source_fd, change["path"]) as (live_parent, _):
                    a, b = os.fstat(fd), os.fstat(live_parent)
                    if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                        raise WorkspaceError("Host directory moved before promotion")
                original, _ = _existing(source_fd, change["path"], max_bytes=max_bytes)
                if original != baseline.get(change["path"]):
                    raise WorkspaceError(f"Host changed during promotion: {change['path']}")
                if change["action"] == "delete":
                    os.unlink(name, dir_fd=fd)
                else:
                    os.replace(item["new"], name, src_dir_fd=fd, dst_dir_fd=fd)
                    item["new"] = None
                applied.append(item)
                os.fsync(fd)
            return [dict(item["change"]) for item in applied]
        except BaseException as exc:
            remaining = []
            for item in reversed(applied):
                try:
                    change, fd, name = item["change"], item["fd"], item["name"]
                    with _parent(source_fd, change["path"]) as (live_parent, _):
                        a, b = os.fstat(fd), os.fstat(live_parent)
                        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                            raise WorkspaceError("Host directory moved before rollback")
                    actual, _ = _existing(source_fd, change["path"], max_bytes=max_bytes)
                    if actual != current.get(change["path"]):
                        raise WorkspaceError("Host changed before rollback")
                    if item["backup"] is not None:
                        os.replace(item["backup"], name, src_dir_fd=fd, dst_dir_fd=fd)
                        item["backup"] = None
                    else:
                        os.unlink(name, dir_fd=fd)
                    os.fsync(fd)
                except (OSError, WorkspaceError) as rollback_exc:
                    remaining.append(dict(item["change"]))
                    rollback_errors.append(f"{item['change']['path']}: {rollback_exc}")
                    if item["backup"] is not None:
                        recovery = str(Path(item["change"]["path"]).parent / item["backup"])
                        rollback_errors.append(f"Original content retained at {recovery}")
                        item["backup"] = None  # Keep the recovery file if rollback failed.
            detail = (
                "promotion stopped; rollback incomplete"
                if rollback_errors
                else "promotion stopped; no changes retained"
            )
            raise WorkspaceError(
                f"{detail}: {exc}", applied=remaining, rollback_errors=rollback_errors
            ) from exc
        finally:
            for item in prepared:
                for key in ("new", "backup"):
                    if item[key] is not None:
                        try:
                            os.unlink(item[key], dir_fd=item["fd"])
                        except OSError:
                            pass
                os.close(item["fd"])
            for path in reversed(created):
                try:
                    with _parent(source_fd, path) as (parent_fd, name):
                        os.rmdir(name, dir_fd=parent_fd)
                except OSError:
                    pass  # Only remove newly created directories if still empty.
