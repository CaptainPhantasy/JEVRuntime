"""Linux process isolation for disposable worker workspaces.

This module deliberately has no unconfined fallback. The caller creates a disposable
workspace, decides which tool installations may be exposed, and mediates any provider
connection through a Unix socket. The real repository, credentials, and intent ledger
must not be passed as mounts. Isolation controls access, not the quality of worker output.
"""

import codecs
import errno
import math
import os
import pty
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
from pathlib import Path


class SandboxError(RuntimeError):
    """The requested isolation could not be established or supervised."""


class Sandbox:
    """A fresh, networkless bubblewrap container for each process invocation.

    Output capture is capped at 1 MiB; exceeding it stops the process rather than
    silently discarding evidence. A callback exception also terminates the worker
    before propagating. ``on_tick`` is called at least every 250 ms while running;
    an explicit False stops the invocation. An independent watchdog enforces the
    worker deadline even during a blocked callback. Callback completion itself
    remains the caller's responsibility. ``on_output`` receives decoded text.

    ``tool_roots`` are trusted, explicit readonly installation paths, mounted at
    their original absolute paths. They must never include a user's home directory,
    source repository, credentials, or the host ledger. ``socket_dir`` contains
    only the provider socket and the trusted relay script; the mount is readonly.
    The sandbox cannot make Internet connections by itself.
    """

    MAX_OUTPUT_BYTES = 1024 * 1024
    MAX_PROCESSES = 256
    MAX_OPEN_FILES = 256
    MAX_FILE_BYTES = 256 * 1024 * 1024
    TMP_BYTES = 256 * 1024 * 1024

    @staticmethod
    def _executables():
        if sys.platform != "linux":
            raise SandboxError("Worker isolation requires Linux and bubblewrap")
        # Avoid a worker/project-controlled PATH when locating trusted launchers.
        trusted_path = "/usr/bin:/bin:/usr/sbin:/sbin"
        bwrap = shutil.which("bwrap", path=trusted_path)
        prlimit = shutil.which("prlimit", path=trusted_path)
        if not bwrap or not prlimit:
            raise SandboxError("Install bubblewrap and util-linux before starting a worker")
        return bwrap, prlimit

    def _command(
        self, argv, workspace, readonly, env, socket_dir, tool_roots, timeout, interactive
    ):
        bwrap, prlimit = self._executables()
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
        ):
            raise SandboxError("A worker command must be a nonempty list of arguments")
        if not argv[0]:
            raise SandboxError("A worker executable is required")
        root = Path(workspace).resolve(strict=True)
        if not root.is_dir() or root == Path("/") or root == Path.home().resolve():
            raise SandboxError("A dedicated disposable workspace directory is required")
        # The trusted supervisor strips these before staging a worker workspace.
        # It may initialize a fresh, local .git for unmodified terminal harnesses;
        # the original repository's Git metadata must never be copied here.
        if any((root / name).exists() for name in (".jev", ".env")):
            raise SandboxError("Worker workspaces must exclude repository and runtime secrets")

        command = [
            bwrap,
            "--unshare-all",
            "--unshare-user",
            "--disable-userns",
            "--assert-userns-disabled",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--hostname",
            "jev-worker",
            "--clearenv",
            "--ro-bind",
            "/usr",
            "/usr",
        ]
        for directory in ("/bin", "/sbin", "/lib", "/lib64"):
            path = Path(directory)
            if path.is_symlink():
                command.extend(("--symlink", os.readlink(path), directory))
            elif path.exists():
                command.extend(("--ro-bind", directory, directory))
        for directory in ("/etc/ld.so.cache", "/etc/alternatives", "/etc/ssl/certs"):
            if Path(directory).exists():
                command.extend(("--ro-bind", directory, directory))
        command.extend(
            (
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--size",
                str(self.TMP_BYTES),
                "--tmpfs",
                "/tmp",
                "--size",
                str(self.TMP_BYTES),
                "--tmpfs",
                "/home",
                "--dir",
                "/home/worker",
                "--dir",
                "/run",
            )
        )
        reserved = (Path("/workspace"), Path("/run"), Path("/proc"), Path("/dev"))
        for supplied in tool_roots:
            path = Path(supplied).resolve(strict=True)
            if (
                path in (Path("/"), Path("/home"), Path("/root"), Path("/tmp"), Path("/etc"))
                or path == Path.home().resolve()
                or root.is_relative_to(path)
                or path.is_relative_to(root)
                or any(path == p or path.is_relative_to(p) for p in reserved)
            ):
                raise SandboxError("Tool roots must be isolated installation paths")
            if not path.is_dir() and not path.is_file():
                raise SandboxError("Tool roots must be regular files or directories")
            command.extend(("--ro-bind", str(path), str(path)))
        command.extend(("--ro-bind" if readonly else "--bind", str(root), "/workspace"))
        if socket_dir is not None:
            directory = Path(socket_dir).resolve(strict=True)
            if not directory.is_dir() or {item.name for item in directory.iterdir()} != {
                "provider.sock",
                "relay.py",
            }:
                raise SandboxError(
                    "The relay directory must contain only provider.sock and relay.py"
                )
            socket_info = (directory / "provider.sock").lstat()
            relay_info = (directory / "relay.py").lstat()
            if (
                not stat.S_ISSOCK(socket_info.st_mode)
                or not stat.S_ISREG(relay_info.st_mode)
                or relay_info.st_nlink != 1
            ):
                raise SandboxError("The relay requires a Unix socket and a script without links")
            command.extend(("--ro-bind", str(directory), "/run/jev"))

        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/home/worker",
            "TMPDIR": "/tmp",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TERM": "xterm-256color",
        }
        for key, value in (env or {}).items():
            if (
                not isinstance(key, str)
                or not key
                or "=" in key
                or "\0" in key
                or not isinstance(value, str)
                or "\0" in value
            ):
                raise SandboxError("Invalid worker environment variable")
            environment[key] = value
        for key, value in environment.items():
            command.extend(("--setenv", key, value))
        command.extend(("--chdir", "/workspace", "--"))
        if interactive:
            setsid = shutil.which("setsid", path="/usr/bin:/bin:/usr/sbin:/sbin")
            if not setsid:
                raise SandboxError("Interactive workers require util-linux setsid")
            # The inherited slave belongs to a dedicated PTY, never the host's
            # terminal. Establish its controlling session inside the namespace so
            # unmodified harnesses may open /dev/tty, without a threaded preexec_fn.
            command.extend((setsid, "--ctty", "--wait"))
        # Run limits inside the namespaces. No Python preexec_fn is used: the
        # supervisor may already have relay threads when it launches a process.
        command.extend(
            (
                prlimit,
                "--core=0:0",
                f"--nproc={self.MAX_PROCESSES}:{self.MAX_PROCESSES}",
                f"--nofile={self.MAX_OPEN_FILES}:{self.MAX_OPEN_FILES}",
                f"--fsize={self.MAX_FILE_BYTES}:{self.MAX_FILE_BYTES}",
                f"--cpu={max(1, math.ceil(timeout))}:{max(1, math.ceil(timeout))}",
                "--",
                *argv,
            )
        )
        return command

    def check(self):
        """Probe the actual kernel setup, not just the presence of a binary."""
        try:
            with tempfile.TemporaryDirectory(prefix="jev-isolation-check-") as directory:
                result = self.run(["/usr/bin/true"], Path(directory), timeout=10, readonly=True)
            if result["exit_code"] != 0 or result["timed_out"] or result["stopped"]:
                return {
                    "available": False,
                    "backend": "bubblewrap",
                    "reason": result["output"].strip() or "Namespace probe failed",
                }
            return {"available": True, "backend": "bubblewrap", "reason": ""}
        except (SandboxError, OSError) as exc:
            return {"available": False, "backend": "bubblewrap", "reason": str(exc)}

    @staticmethod
    def _stop(process):
        # SIGKILL avoids a malicious worker's signal handlers. The bwrap PID
        # namespace init/reaper and die-with-parent tear down escaped sessions too.
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()

    def run(
        self,
        argv: list[str],
        workspace: Path,
        *,
        timeout: float = 900,
        readonly: bool = False,
        env: dict | None = None,
        on_output=None,
        on_tick=None,
        interactive: bool = False,
        socket_dir: Path | None = None,
        tool_roots: tuple = (),
    ) -> dict:
        if not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
            raise SandboxError("The worker timeout must be a finite positive number")
        try:
            command = self._command(
                argv, workspace, readonly, env, socket_dir, tool_roots, timeout, interactive
            )
        except (OSError, ValueError) as exc:
            raise SandboxError(f"Invalid sandbox configuration: {exc}") from exc
        started = time.monotonic()
        output = bytearray()
        timed_out = stopped = truncated = False
        process = None
        reader = slave = None
        saved_terminal = None
        input_fd = None
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        selector = selectors.DefaultSelector()
        deadline_expired = threading.Event()
        process_lock = threading.Lock()
        watchdog = None

        def running():
            # Serialize reaping and process-group termination so the watchdog
            # cannot signal an already-reaped/reused process identifier.
            with process_lock:
                return process.poll() is None

        def stop():
            with process_lock:
                self._stop(process)

        def enforce_deadline():
            with process_lock:
                if process.poll() is None:
                    deadline_expired.set()
                    self._stop(process)

        try:
            if interactive:
                reader, slave = pty.openpty()
                try:
                    input_fd = sys.stdin.fileno()
                    if os.isatty(input_fd):
                        saved_terminal = termios.tcgetattr(input_fd)
                        termios.tcsetwinsize(slave, termios.tcgetwinsize(input_fd))
                        # Cbreak retains host Ctrl-C handling, so interrupting
                        # always tears down the entire isolated worker.
                        tty.setcbreak(input_fd)
                except (AttributeError, OSError, ValueError):
                    input_fd = None
                process = subprocess.Popen(
                    command,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                    start_new_session=True,
                    close_fds=True,
                )
                os.close(slave)
                slave = None
            else:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                    start_new_session=True,
                    close_fds=True,
                )
                reader = process.stdout.fileno()
            watchdog = threading.Timer(
                max(0, started + timeout - time.monotonic()), enforce_deadline
            )
            watchdog.daemon = True
            watchdog.start()
            os.set_blocking(reader, False)
            selector.register(reader, selectors.EVENT_READ, "output")
            if interactive and input_fd is not None:
                try:
                    selector.register(input_fd, selectors.EVENT_READ, "input")
                except (OSError, ValueError):
                    input_fd = None

            output_open = True
            while output_open or running():
                if running():
                    if time.monotonic() - started >= timeout:
                        timed_out = True
                        stop()
                    elif on_tick is not None and on_tick() is False:
                        stopped = True
                        stop()
                for key, _ in selector.select(0.1):
                    if key.data == "input":
                        chunk = os.read(input_fd, 4096)
                        if not chunk:
                            selector.unregister(input_fd)
                            # A PTY needs EOT to communicate end of input.
                            try:
                                os.write(reader, b"\x04")
                            except OSError:
                                pass
                        else:
                            try:
                                os.write(reader, chunk)
                            except (BlockingIOError, OSError):
                                # Worker may have exited between readiness and write.
                                if running():
                                    raise SandboxError("Worker stopped accepting terminal input")
                        continue
                    try:
                        chunk = os.read(reader, 65536)
                    except BlockingIOError:
                        continue
                    except OSError as exc:
                        if interactive and exc.errno == errno.EIO:
                            chunk = b""
                        else:
                            raise
                    if not chunk:
                        selector.unregister(reader)
                        output_open = False
                        continue
                    remaining = self.MAX_OUTPUT_BYTES - len(output)
                    accepted = chunk[:remaining]
                    output.extend(accepted)
                    if on_output is not None and accepted:
                        text = decoder.decode(accepted)
                        if text:
                            on_output(text)
                    if len(chunk) > remaining:
                        truncated = stopped = True
                        stop()
                if not running() and not output_open:
                    break
            if on_output is not None:
                tail = decoder.decode(b"", final=True)
                if tail:
                    on_output(tail)
            return {
                "exit_code": process.returncode,
                "timed_out": timed_out or deadline_expired.is_set(),
                "stopped": stopped,
                "output": output.decode("utf-8", errors="replace"),
                "output_truncated": truncated,
                "duration_seconds": round(time.monotonic() - started, 3),
            }
        except KeyboardInterrupt:
            if process is not None:
                stop()
            raise
        except OSError as exc:
            raise SandboxError(f"Unable to supervise isolated worker: {exc}") from exc
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if process is not None:
                stop()
                if process.stdout is not None:
                    process.stdout.close()
            if watchdog is not None and watchdog.ident is not None:
                watchdog.join()
            selector.close()
            if interactive and reader is not None:
                os.close(reader)
            if slave is not None:
                os.close(slave)
            if saved_terminal is not None:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved_terminal)
