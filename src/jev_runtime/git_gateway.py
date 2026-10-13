"""Host Git operations bound to a reviewed repository snapshot.

This module does not grant authority. The runtime must approve the exact preview
before calling execute, and must keep its Git metadata inaccessible to workers.
"""

import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import digest as policy_digest
from .workspace import WorkspaceError, validate_path

SAFE_CONFIG = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", "false"),
    ("core.sshCommand", "/bin/false"),
    ("core.gitProxy", "/bin/false"),
    ("core.pager", "cat"),
    ("core.attributesFile", "/dev/null"),
    ("core.alternateRefsCommand", "/bin/false"),
    ("credential.helper", ""),
    ("credential.username", ""),
    ("credential.interactive", "false"),
    ("http.extraHeader", ""),
    ("http.proxy", ""),
    ("http.followRedirects", "false"),
    ("http.sslVerify", "true"),
    ("http.emptyAuth", "false"),
    ("commit.gpgSign", "false"),
    ("push.gpgSign", "false"),
    ("push.followTags", "false"),
    ("push.pushOption", ""),
    ("submodule.recurse", "false"),
    ("protocol.allow", "never"),
    ("protocol.https.allow", "always"),
)
MAX_OUTPUT = 8 * 1024 * 1024
MAX_FILES = 20000
MAX_CONTENT = 256 * 1024 * 1024
MAX_REVIEW_BYTES = 16000
MAX_HISTORY_COMMITS = 256
REVIEW_FORMAT = "jevruntime.git-review.v1"


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _display(value, token=None):
    text = value.decode("utf-8", errors="replace")
    if token:
        text = text.replace(token, "[redacted]")
    return "".join(char for char in text if char.isprintable() or char in "\n\t")[:12000]


class GitGateway:
    def __init__(self, root, *, allow_http=False, protected_paths=(), policy_hash=""):
        if type(allow_http) is not bool:
            raise ValueError("HTTP Git transport must be explicitly enabled with a boolean")
        self.allow_http = allow_http
        self.protected_paths = tuple(protected_paths)
        self.policy_hash = policy_hash
        self._mutation_lock_fd = None
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Git workspace must be a directory")
        self.git = shutil.which("git", path=os.defpath)
        if self.git is None:
            raise ValueError("Git is not installed")

    def _run(self, arguments, *, overrides=(), extra_env=None, token=None, check=True):
        with tempfile.TemporaryDirectory(prefix="jev-git-home-") as temporary:
            environment = {
                "PATH": os.defpath,
                "HOME": temporary,
                "XDG_CONFIG_HOME": temporary,
                "LC_ALL": "C",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "/bin/false",
                "GIT_SSH": "/bin/false",
                "GIT_SSH_COMMAND": "/bin/false",
                "GIT_ALLOW_PROTOCOL": "https:http" if self.allow_http else "https",
                "GIT_PROTOCOL_FROM_USER": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_GRAFT_FILE": "/dev/null",
                "KRB5CCNAME": "/dev/null",
                "KRB5_CONFIG": "/dev/null",
            }
            environment.update(extra_env or {})
            command = [self.git, "--no-pager"]
            for key, value in (*SAFE_CONFIG, *overrides):
                command.extend(("-c", key + "=" + value))
            if self.allow_http:
                command.extend(("-c", "protocol.http.allow=always"))
            command.extend(arguments)
            with subprocess.Popen(
                command,
                cwd=self.root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                pass_fds=() if self._mutation_lock_fd is None else (self._mutation_lock_fd,),
            ) as process:
                buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
                deadline = time.monotonic() + 120
                try:
                    with selectors.DefaultSelector() as selector:
                        for pipe in buffers:
                            selector.register(pipe, selectors.EVENT_READ)
                        while selector.get_map():
                            if time.monotonic() >= deadline:
                                raise ValueError("Git operation timed out")
                            for key, _ in selector.select(timeout=0.2):
                                chunk = os.read(key.fileobj.fileno(), 65536)
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                    continue
                                buffers[key.fileobj].extend(chunk)
                                if sum(map(len, buffers.values())) > MAX_OUTPUT:
                                    raise ValueError("Git output exceeded the bounded result limit")
                    code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
                except BaseException:
                    # Kill Git and its HTTPS/askpass subprocesses together.
                    import signal

                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    raise
            out, error = bytes(buffers[process.stdout]), bytes(buffers[process.stderr])
            if check and code:
                detail = _display(error or out, token)
                raise ValueError("Git operation failed: " + (detail or str(code)))
            return code, out, error

    def _configuration(self):
        # Reading config does not execute its contents. Includes and URL rewrite
        # rules are rejected; otherwise an apparent HTTPS target could become an
        # arbitrary helper invocation before Git's network request.
        _, raw, _ = self._run(["config", "--null", "--list", "--no-includes"])
        overrides = []
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            key = entry.split(b"\n", 1)[0].decode("utf-8", errors="strict")
            lower = key.lower()
            if (
                lower == "include.path"
                or lower.startswith("includeif.")
                or (lower.startswith("url.") and lower.endswith((".insteadof", ".pushinsteadof")))
                or (lower.startswith("remote.") and lower.endswith(".vcs"))
                or lower == "extensions.partialclone"
                or (lower.startswith("remote.") and lower.endswith(".promisor"))
            ):
                raise ValueError(
                    "Git includes, URL rewrites and custom transport helpers are disabled"
                )
            if lower.startswith("filter."):
                if lower.endswith((".clean", ".smudge", ".process")):
                    overrides.append((key, ""))
                elif lower.endswith(".required"):
                    overrides.append((key, "false"))
            if lower.startswith("credential.") and lower.endswith(".helper"):
                overrides.append((key, ""))
            if lower.startswith("credential.") and lower.endswith(".username"):
                overrides.append((key, ""))
            if lower.startswith("http."):
                # Scoped URL config can override ordinary http.* settings.
                tail = lower.rsplit(".", 1)[-1]
                values = {
                    "extraheader": "",
                    "proxy": "",
                    "followredirects": "false",
                    "sslverify": "true",
                    "emptyauth": "false",
                    "cookiefile": "",
                    "savecookies": "false",
                    "sslcert": "",
                    "sslkey": "",
                    "sslcainfo": "",
                    "sslcapath": "",
                    "curloptresolve": "",
                }
                if tail in values:
                    overrides.append((key, values[tail]))
        return tuple(overrides), _digest(raw)

    def _state(self, overrides, config_digest):
        _, top, _ = self._run(["rev-parse", "--show-toplevel"], overrides=overrides)
        if Path(os.fsdecode(top).rstrip("\n")).resolve() != self.root:
            raise ValueError("Choose the exact Git repository root")
        code, head, _ = self._run(
            ["rev-parse", "--verify", "HEAD"], overrides=overrides, check=False
        )
        head = head.decode().strip() if not code else ""
        if head and not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head):
            raise ValueError("Invalid Git HEAD")
        code, ref, _ = self._run(["symbolic-ref", "-q", "HEAD"], overrides=overrides, check=False)
        ref = ref.decode().strip() if not code else "HEAD"
        _, index, _ = self._run(
            ["rev-parse", "--path-format=absolute", "--git-path", "index"], overrides=overrides
        )
        index_path = Path(os.fsdecode(index).rstrip("\n"))
        index_digest = _digest(index_path.read_bytes()) if index_path.exists() else _digest(b"")
        _, files, _ = self._run(
            ["ls-files", "-z", "--cached", "--others", "--exclude-standard"], overrides=overrides
        )
        names = sorted(set(files.split(b"\0")) - {b""})
        if len(names) > MAX_FILES:
            raise ValueError("Repository exceeds the bounded Git review limit")
        hasher, size = hashlib.sha256(), 0
        excluded = []
        for name in names:
            path_name = name.decode("utf-8", errors="strict")
            try:
                validate_path(path_name, protected_paths=self.protected_paths)
            except WorkspaceError:
                # Status can report a protected filename, but never reads its bytes.
                # Mutation review separately rejects protected index/history entries.
                excluded.append(path_name)
                continue
            path = self.root / path_name
            if path.is_absolute() and (b".." in name.split(b"/") or name.startswith(b"/")):
                raise ValueError("Invalid tracked path")
            parent = path.parent
            while parent != self.root:
                if parent.is_symlink():
                    raise ValueError("Tracked paths cannot traverse a directory symlink")
                parent = parent.parent
            hasher.update(len(name).to_bytes(8, "big") + name)
            try:
                info = path.lstat()
            except FileNotFoundError:
                hasher.update(b"missing")
                continue
            hasher.update(str(info.st_mode).encode() + b"\0")
            if stat.S_ISLNK(info.st_mode):
                hasher.update(os.fsencode(os.readlink(path)))
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise ValueError("Hard-linked worktree files cannot participate in Git review")
                size += info.st_size
                if size > MAX_CONTENT:
                    raise ValueError("Repository exceeds the bounded Git content review limit")
                with open(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
                    before = os.fstat(source.fileno())
                    if before.st_nlink != 1:
                        raise ValueError(
                            "Hard-linked worktree files cannot participate in Git review"
                        )
                    while chunk := source.read(65536):
                        hasher.update(chunk)
                    after = os.fstat(source.fileno())
                if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                    raise ValueError("Repository changed while preparing its Git review")
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError("Special files cannot participate in Git review")
        # Git status itself may hash worktree files. Give it only prevalidated
        # literal names, in bounded batches, after checking ordinary-file aliases.
        status = bytearray()
        permitted = [name.decode("utf-8") for name in names if name.decode("utf-8") not in excluded]
        for offset in range(0, len(permitted), 100):
            _, output, _ = self._run(
                [
                    "--literal-pathspecs",
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                    "--ignore-submodules=all",
                    "--",
                    *permitted[offset : offset + 100],
                ],
                overrides=overrides,
            )
            status.extend(output)
            if len(status) > MAX_OUTPUT:
                raise ValueError("Git status exceeds the bounded review limit")
        return {
            "head": head,
            "head_ref": ref,
            "index_path": str(index_path),
            "index_digest": index_digest,
            "worktree_digest": hasher.hexdigest(),
            "status_digest": _digest(status),
            "config_digest": config_digest,
            "status": _display(status),
            "excluded_worktree_paths": excluded,
        }

    def _paths(self, raw, *, index=False):
        entries = []
        for row in raw.split(b"\0"):
            if not row:
                continue
            metadata, name = row.split(b"\t", 1)
            fields = metadata.split()
            if len(fields) != 3 or fields[0] not in {b"100644", b"100755"}:
                raise ValueError("Git review supports ordinary files, not symlinks or submodules")
            if (index and fields[2] != b"0") or (not index and fields[1] != b"blob"):
                raise ValueError("Git review requires a merged ordinary-file index/tree")
            path = name.decode("utf-8", errors="strict")
            self._validate_path(path)
            entries.append(
                {
                    "path": path,
                    "mode": fields[0].decode(),
                    "object": fields[1 if index else 2].decode(),
                }
            )
            if len(entries) > MAX_FILES:
                raise ValueError("Git path review exceeds the bounded file limit")
        return entries

    def _validate_path(self, path):
        try:
            validate_path(path, protected_paths=self.protected_paths)
        except WorkspaceError as exc:
            raise ValueError(
                "Git operation includes a protected or unsupported path: " + path
            ) from exc

    def _review(self, operation, state, overrides):
        if operation == "status":
            return None
        if operation == "commit":
            _, raw, _ = self._run(["ls-files", "--stage", "-z"], overrides=overrides)
            entries = self._paths(raw, index=True)
            _, raw_paths, _ = self._run(
                [
                    "diff",
                    "--cached",
                    "--name-only",
                    "-z",
                    "--no-renames",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--",
                ],
                overrides=overrides,
            )
            changed = [
                value.decode("utf-8", errors="strict") for value in raw_paths.split(b"\0") if value
            ]
            for path in changed:
                self._validate_path(path)
            if not changed:
                raise ValueError("No staged changes to commit; stage reviewed paths explicitly")
            _, stats, _ = self._run(
                [
                    "diff",
                    "--cached",
                    "--numstat",
                    "-z",
                    "--no-renames",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--",
                ],
                overrides=overrides,
            )
            if any(row.startswith(b"-\t-\t") for row in stats.split(b"\0")):
                raise ValueError("Binary Git changes cannot receive complete text review")
            _, patch, _ = self._run(
                [
                    "diff",
                    "--cached",
                    "--no-renames",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-color",
                    "--",
                ],
                overrides=overrides,
            )
            if len(patch) > MAX_REVIEW_BYTES:
                raise ValueError(
                    "Staged Git changes exceed the complete text-review limit; split the commit"
                )
            text = patch.decode("utf-8", errors="strict")
            if any(ord(char) < 32 and char not in "\n\r\t" or ord(char) == 127 for char in text):
                raise ValueError("Git changes contain unsupported control-byte content")
            return {
                "format": REVIEW_FORMAT,
                "scope": "staged_changes",
                "index_entries": entries,
                "paths": sorted(set(changed)),
                "patch": text,
            }
        _, shallow, _ = self._run(["rev-parse", "--is-shallow-repository"], overrides=overrides)
        if shallow.strip() != b"false":
            raise ValueError(
                "Push requires complete local history; shallow repositories are unsupported"
            )
        _, raw, _ = self._run(
            ["rev-list", "--max-count=" + str(MAX_HISTORY_COMMITS + 1), state["head"]],
            overrides=overrides,
        )
        commits = raw.decode().splitlines()
        if not commits or len(commits) > MAX_HISTORY_COMMITS:
            raise ValueError(
                f"Push history exceeds the complete review limit ({MAX_HISTORY_COMMITS} commits)"
            )
        paths, trees, objects = set(), [], {}
        for commit in commits:
            _, raw, _ = self._run(
                ["ls-tree", "-r", "-z", "--full-tree", commit], overrides=overrides
            )
            entries = self._paths(raw)
            paths.update(entry["path"] for entry in entries)
            if len(paths) > MAX_FILES:
                raise ValueError("Push history exceeds the complete path-review file limit")
            trees.append({"commit": commit, "entries_hash": _digest(_canonical(entries))})
            for entry in entries:
                objects.setdefault(entry["object"], set()).add(entry["path"])
            if len(objects) > MAX_FILES:
                raise ValueError("Push history exceeds the complete object-review limit")
        # Validate every historical path before opening any historical blob. Include
        # each distinct blob's full text; a deleted file still travels with history.
        blobs, remaining = [], MAX_REVIEW_BYTES
        for object_id in sorted(objects):
            _, raw_size, _ = self._run(["cat-file", "-s", object_id], overrides=overrides)
            size = int(raw_size)
            if size > remaining:
                raise ValueError("Push history exceeds the complete content-review limit")
            _, content, _ = self._run(["cat-file", "blob", object_id], overrides=overrides)
            if len(content) != size:
                raise ValueError("Git object changed during review")
            text = content.decode("utf-8", errors="strict")
            if any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in text):
                raise ValueError("Push history contains binary or unsupported control-byte content")
            remaining -= size
            blobs.append({"object": object_id, "paths": sorted(objects[object_id]), "text": text})
        for tree in trees:
            _, raw_size, _ = self._run(["cat-file", "-s", tree["commit"]], overrides=overrides)
            size = int(raw_size)
            if size > remaining:
                raise ValueError("Push history exceeds the complete content-review limit")
            _, content, _ = self._run(["cat-file", "commit", tree["commit"]], overrides=overrides)
            if len(content) != size:
                raise ValueError("Git commit changed during review")
            text = content.decode("utf-8", errors="strict")
            if any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in text):
                raise ValueError("Push commit metadata contains unsupported control-byte content")
            remaining -= size
            tree["metadata"] = text
        return {
            "format": REVIEW_FORMAT,
            "scope": "reachable_history",
            "paths": sorted(paths),
            "commits": trees,
            "head": state["head"],
            "blobs": blobs,
            "content_reviewed": True,
        }

    def preview(self, operation, *, message="", remote="", branch=""):
        if operation not in {"status", "commit", "push"}:
            raise ValueError("Supported Git operations are status, commit and push")
        if any(not isinstance(value, str) or "\0" in value for value in (message, remote, branch)):
            raise ValueError("Git operation arguments must be text")
        if operation == "commit" and (not message.strip() or len(message) > 10000):
            raise ValueError("Commit requires a nonempty message of at most 10000 characters")
        if operation != "commit" and message:
            raise ValueError("Only a commit accepts a message")
        if operation != "push" and (remote or branch):
            raise ValueError("Only a push accepts a remote and branch")
        overrides, config_digest = self._configuration()
        state = self._state(overrides, config_digest)
        remote_url, target_ref = "", ""
        if operation == "push":
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", remote):
                raise ValueError("Push requires an explicit configured remote name")
            if not branch or branch.startswith("-") or branch.startswith("refs/"):
                raise ValueError("Push requires an explicit branch name")
            self._run(["check-ref-format", "refs/heads/" + branch], overrides=overrides)
            _, urls, _ = self._run(
                ["remote", "get-url", "--push", "--all", remote], overrides=overrides
            )
            values = urls.decode().splitlines()
            if len(values) != 1:
                raise ValueError("Push requires exactly one configured destination")
            remote_url = values[0]
            parsed = urlsplit(remote_url)
            if (
                parsed.scheme not in ({"https", "http"} if self.allow_http else {"https"})
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or any(char.isspace() for char in remote_url)
                or "\\" in remote_url
            ):
                raise ValueError(
                    "Push requires HTTPS without embedded credentials; trusted HTTP remotes require explicit --allow-http"
                )
            parsed.port
            if not state["head"]:
                raise ValueError("Push requires an existing commit")
            target_ref = "refs/heads/" + branch
        plan = {
            "version": 2,
            "protected_paths": list(self.protected_paths),
            "policy_hash": self.policy_hash,
            "git_review": self._review(operation, state, overrides),
            "root": str(self.root),
            "operation": operation,
            "arguments": {"message": message, "remote": remote, "branch": branch},
            "state": state,
            "remote_url": remote_url,
            "target_ref": target_ref,
        }
        plan["plan_id"] = _digest(_canonical(plan))
        return plan

    def execute(self, plan, *, username=None, token=None, before_mutation=None):
        if not isinstance(plan, dict) or plan.get("root") != str(self.root):
            raise ValueError("Git plan belongs to a different repository")
        try:
            current = self.preview(plan["operation"], **plan["arguments"])
        except (KeyError, TypeError) as error:
            raise ValueError("Invalid Git plan") from error
        if current != plan:
            raise ValueError("Repository or operation changed; a fresh Git approval is required")
        operation = plan["operation"]
        if operation != "push" and (username is not None or token is not None):
            raise ValueError("Credentials are accepted only for an explicit push")
        if operation == "status":
            return {"operation": "status", "plan_id": plan["plan_id"], **plan["state"]}
        overrides, _ = self._configuration()
        if operation == "commit":
            state = plan["state"]
            if not plan["git_review"]["paths"]:
                raise ValueError("No staged changes to commit; stage reviewed paths explicitly")
            with tempfile.TemporaryDirectory(prefix="jev-reviewed-index-") as temporary:
                index = Path(temporary) / "index"
                shutil.copyfile(state["index_path"], index)
                if _digest(index.read_bytes()) != state["index_digest"]:
                    raise ValueError("The staged index changed after approval")
                _, tree, _ = self._run(
                    ["write-tree"], overrides=overrides, extra_env={"GIT_INDEX_FILE": str(index)}
                )
                args = ["commit-tree", tree.decode().strip()]
                if state["head"]:
                    args.extend(("-p", state["head"]))
                args.extend(("-m", plan["arguments"]["message"]))
                _, commit, _ = self._run(args, overrides=overrides)
            commit = commit.decode().strip()
            # Publishing the new commit is an atomic compare-and-swap against the
            # approved parent. The reviewed index is never expanded or restaged.
            if self.preview(operation, **plan["arguments"]) != plan:
                raise ValueError("Repository changed before commit publication; approval expired")
            old = state["head"] or ("0" * len(commit))
            if before_mutation:
                before_mutation()
            self._run(
                [
                    "update-ref",
                    "--no-deref",
                    "-m",
                    "jev: approved commit",
                    state["head_ref"],
                    commit,
                    old,
                ],
                overrides=overrides,
            )
            return {"operation": "commit", "plan_id": plan["plan_id"], "commit": commit}
        environment = {}
        with tempfile.TemporaryDirectory(prefix="jev-push-credential-") as temporary:
            if token is not None:
                username = username or "x-access-token"
                if any(
                    not isinstance(value, str) or not value or any(c in value for c in "\r\n\0")
                    for value in (username, token)
                ):
                    raise ValueError("Invalid runtime-only Git credentials")
                askpass = Path(temporary) / "askpass"
                askpass.write_text(
                    '#!/bin/sh\ncase "$1" in\n'
                    '  *Username*) printf "%s\\n" "$JEV_GIT_USERNAME" ;;\n'
                    '  *Password*) printf "%s\\n" "$JEV_GIT_TOKEN" ;;\n'
                    "  *) exit 1 ;;\nesac\n"
                )
                askpass.chmod(0o700)
                environment = {
                    "GIT_ASKPASS": str(askpass),
                    "JEV_GIT_USERNAME": username,
                    "JEV_GIT_TOKEN": token,
                }
                overrides = tuple(
                    (
                        key,
                        username
                        if key.lower().startswith("credential.")
                        and key.lower().endswith(".username")
                        else value,
                    )
                    for key, value in overrides
                ) + (("credential.username", username),)
            elif username is not None:
                raise ValueError("A username alone cannot authenticate a push")
            if before_mutation:
                before_mutation()
            _, output, error = self._run(
                [
                    "push",
                    "--porcelain",
                    "--no-verify",
                    "--no-follow-tags",
                    "--signed=false",
                    "--recurse-submodules=no",
                    "--receive-pack=git-receive-pack",
                    "--",
                    plan["remote_url"],
                    plan["state"]["head"] + ":" + plan["target_ref"],
                ],
                overrides=overrides,
                extra_env=environment,
                token=token,
            )
        return {
            "operation": "push",
            "plan_id": plan["plan_id"],
            "commit": plan["state"]["head"],
            "remote_url": plan["remote_url"],
            "target_ref": plan["target_ref"],
            "output": _display(output + error, token),
        }


def validate_policy_plan(policy, action, plan):
    """A semantic score or human confirmation cannot override Git path restrictions."""
    try:
        if (
            not isinstance(plan, dict)
            or plan.get("version") != 2
            or plan.get("operation") != ("commit" if action == "git" else "push")
            or Path(plan["root"]).resolve() != Path(policy.workspace).resolve()
            or plan.get("protected_paths") != list(policy.protected_paths)
            or plan.get("policy_hash") != policy_digest(policy.as_dict())
            or plan.get("plan_id")
            != _digest(_canonical({k: v for k, v in plan.items() if k != "plan_id"}))
        ):
            return False
        review = plan["git_review"]
        if review.get("format") != REVIEW_FORMAT or not isinstance(review.get("paths"), list):
            return False
        if len(review["paths"]) > MAX_FILES:
            return False
        for path in review["paths"]:
            validate_path(path, protected_paths=policy.protected_paths)
        if action == "git":
            if review.get("scope") != "staged_changes" or not review["paths"]:
                return False
            if (
                not isinstance(review.get("patch"), str)
                or not 0 < len(review["patch"].encode()) <= MAX_REVIEW_BYTES
            ):
                return False
            entries = review["index_entries"]
            if not isinstance(entries, list) or len(entries) > MAX_FILES:
                return False
            for entry in entries:
                validate_path(entry["path"], protected_paths=policy.protected_paths)
                if entry["mode"] not in {"100644", "100755"} or not re.fullmatch(
                    r"(?:[0-9a-f]{40}|[0-9a-f]{64})", entry["object"]
                ):
                    return False
        else:
            if (
                review.get("scope") != "reachable_history"
                or review.get("head") != plan["state"]["head"]
                or review.get("content_reviewed") is not True
                or not isinstance(review.get("commits"), list)
                or not 0 < len(review["commits"]) <= MAX_HISTORY_COMMITS
            ):
                return False
            if review["commits"][0]["commit"] != review["head"]:
                return False
            for commit in review["commits"]:
                if not isinstance(commit["metadata"], str) or any(
                    ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in commit["metadata"]
                ):
                    return False
                if not re.fullmatch(
                    r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit["commit"]
                ) or not re.fullmatch(r"[0-9a-f]{64}", commit["entries_hash"]):
                    return False
            blobs = review["blobs"]
            if (
                not isinstance(blobs, list)
                or len(blobs) > MAX_FILES
                or sum(len(b["text"].encode()) for b in blobs)
                + sum(len(c["metadata"].encode()) for c in review["commits"])
                > MAX_REVIEW_BYTES
            ):
                return False
            blob_paths = set()
            for blob in blobs:
                if (
                    not isinstance(blob["paths"], list)
                    or not blob["paths"]
                    or not isinstance(blob["text"], str)
                ):
                    return False
                for path in blob["paths"]:
                    validate_path(path, protected_paths=policy.protected_paths)
                    blob_paths.add(path)
                if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", blob["object"]) or any(
                    ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in blob["text"]
                ):
                    return False
            if blob_paths != set(review["paths"]):
                return False
        return True
    except (KeyError, TypeError, ValueError, AttributeError, WorkspaceError):
        return False
