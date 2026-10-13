"""Standard-library-only worker relay, copied into /run/jev by ProviderBroker.

Run: python3 /run/jev/relay.py -- worker-command [arguments...]
The worker sees placeholder API credentials and a loopback provider endpoint.
The host Unix-socket broker holds the real credential and validates every call.
"""

import argparse
import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BODY = 2 * 1024 * 1024


def _write_config(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as target:
        target.write(content)
    path.chmod(0o600)


def prepare_worker(command, environment, port=8765):
    """Configure known harnesses inside the fresh worker home only.

    JEV_MODEL chooses the provider model. JEV_ALLOW_WRITE and JEV_ALLOW_EXECUTE
    are explicit root policy flags; omitted flags deny the corresponding tools.
    Generic commands retain their exact arguments and environment.
    """
    vendor = Path(command[0]).name
    if vendor not in {"crush", "codex", "claude"}:
        return command, environment
    if environment.get("HOME") != "/home/worker":
        raise ValueError("Vendor configuration requires the isolated worker home")
    model = environment.get("JEV_MODEL", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}", model):
        raise ValueError("Set a valid worker model in the runtime provider configuration")
    writable = environment.get("JEV_ALLOW_WRITE") == "1"
    executable = environment.get("JEV_ALLOW_EXECUTE") == "1"
    environment = dict(environment)
    base_url = f"http://127.0.0.1:{port}"
    home = Path("/home/worker")
    environment.update(
        {
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "OPENAI_BASE_URL": base_url + "/v1",
            "OPENAI_API_KEY": "runtime-broker",
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_API_KEY": "runtime-broker",
            "ANTHROPIC_AUTH_TOKEN": "runtime-broker",
            "DO_NOT_TRACK": "1",
        }
    )
    if vendor == "crush":
        # Crush project config is executable and overrides global config. The
        # supervisor should omit these files when constructing the workspace.
        for name in ("crush.json", ".crush.json", "crushrc", ".crushrc", ".crush"):
            if (Path.cwd() / name).exists():
                raise ValueError("Crush project configuration must be excluded from worker staging")
        config_dir = home / ".config/crush"
        state_dir = home / ".local/share/crush"
        allowed = ["view", "ls", "glob", "grep"]
        denied = ["fetch", "web_search", "sourcegraph"]
        if writable:
            allowed.extend(("write", "edit", "multiedit"))
        else:
            denied.extend(("write", "edit", "multiedit"))
        if executable:
            allowed.append("bash")
        else:
            denied.append("bash")
        config = {
            "providers": {
                "jev": {
                    "id": "jev",
                    "name": "JEVRUNTIME provider broker",
                    "type": "openai-compat",
                    "base_url": base_url + "/v1",
                    "api_key": "runtime-broker",
                    "discover_models": False,
                    "models": [
                        {
                            "id": model,
                            "name": model,
                            "context_window": 64000,
                            "default_max_tokens": 4096,
                            "cost_per_1m_in": 0,
                            "cost_per_1m_out": 0,
                            "cost_per_1m_in_cached": 0,
                            "cost_per_1m_out_cached": 0,
                            "can_reason": False,
                            "supports_attachments": False,
                        }
                    ],
                }
            },
            "models": {
                slot: {"provider": "jev", "model": model, "max_tokens": 4096}
                for slot in ("large", "small")
            },
            "permissions": {"allowed_tools": allowed},
            "mcp": {},
            "lsp": {},
            "options": {
                "data_directory": str(state_dir / "workspace"),
                "disable_provider_auto_update": True,
                "disable_default_providers": True,
                "disable_metrics": True,
                "auto_lsp": False,
                "notifications": "disabled",
                "disabled_tools": denied,
                "disabled_skills": ["crush-config"],
                "request_timeout": 60,
            },
        }
        _write_config(config_dir / "crush.json", json.dumps(config))
        environment.update(
            {
                "CRUSH_GLOBAL_CONFIG": str(config_dir),
                "CRUSH_GLOBAL_DATA": str(state_dir),
                "CRUSH_DISABLE_PROVIDER_AUTO_UPDATE": "1",
                "CRUSH_DISABLE_DEFAULT_PROVIDERS": "1",
            }
        )
    elif vendor == "codex":
        config_dir = home / ".codex"
        config = (
            f"model = {json.dumps(model)}\n"
            'model_provider = "jev"\n'
            'approval_policy = "never"\n'
            f'sandbox_mode = "{"workspace-write" if writable else "read-only"}"\n'
            'web_search = "disabled"\n'
            "allow_login_shell = false\n"
            "[analytics]\nenabled = false\n"
            "[feedback]\nenabled = false\n"
            '[otel]\nexporter = "none"\n'
            '[history]\npersistence = "none"\n'
            "[features]\n"
            f"shell_tool = {str(executable).lower()}\n"
            "skill_mcp_dependency_install = false\n"
            "[model_providers.jev]\n"
            'name = "JEVRUNTIME provider broker"\n'
            f"base_url = {json.dumps(base_url + '/v1')}\n"
            'env_key = "OPENAI_API_KEY"\n'
            'wire_api = "responses"\n'
            "requires_openai_auth = false\n"
            "supports_websockets = false\n"
            "request_max_retries = 0\n"
            "stream_max_retries = 0\n"
            "stream_idle_timeout_ms = 60000\n"
        )
        _write_config(config_dir / "config.toml", config)
        environment["CODEX_HOME"] = str(config_dir)
    else:
        config_dir = home / ".claude"
        allowed = ["Read", "Glob", "Grep"]
        if writable:
            allowed.extend(("Write", "Edit"))
        if executable:
            allowed.append("Bash")
        _write_config(
            config_dir / "settings.json",
            json.dumps(
                {
                    "model": model,
                    "disableAllHooks": True,
                    "permissions": {
                        "defaultMode": "dontAsk",
                        "allow": allowed,
                        "deny": ["WebSearch", "WebFetch"],
                    },
                }
            ),
        )
        mcp = config_dir / "empty-mcp.json"
        _write_config(mcp, '{"mcpServers":{}}')
        environment.update(
            {
                "CLAUDE_CONFIG_DIR": str(config_dir),
                "ANTHROPIC_MODEL": model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_TELEMETRY": "1",
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_ERROR_REPORTING": "1",
            }
        )
        command = [
            command[0],
            "--setting-sources",
            "user",
            "--strict-mcp-config",
            "--mcp-config",
            str(mcp),
            "--model",
            model,
            *command[1:],
        ]
    return command, environment


class _UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.host)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 4

    def handle_error(self, request, client_address):
        pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "JEVRelay"
    sys_version = ""

    def setup(self):
        self.request.settimeout(15)
        super().setup()

    def log_message(self, *_args):
        pass

    def _error(self, status):
        body = b'{"error":"Provider request rejected"}\n'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _forward(self):
        connection, responded = None, False
        try:
            if self.command not in {"GET", "POST"}:
                self._error(403)
                return
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) > 1:
                self._error(400)
                return
            if self.command == "POST" and not lengths:
                self._error(411)
                return
            try:
                size = int(lengths[0]) if lengths else 0
            except ValueError:
                size = -1
            if not 0 <= size <= MAX_BODY:
                self._error(413)
                return
            body = self.rfile.read(size)
            if len(body) != size:
                self._error(400)
                return
            connection = _UnixConnection(self.server.socket_path, timeout=70)
            headers = {"Content-Type": "application/json", "Connection": "close"}
            for name in ("accept", "anthropic-version", "anthropic-beta", "openai-beta"):
                if self.headers.get(name):
                    headers[name] = self.headers[name]
            path = self.path
            # Anthropic's SDK adds this constant beta query to inference. It is
            # not a routing capability; preserve beta headers, normalize only
            # this exact documented spelling, and let the host reject all others.
            if path in {"/v1/messages?beta=true", "/messages?beta=true"}:
                path = path.split("?", 1)[0]
            connection.request(self.command, path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Connection", "close")
            self.end_headers()
            responded = True
            # read1 returns currently available data without waiting for a full
            # buffer, preserving server-sent-event delivery during generation.
            while chunk := response.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, ValueError, http.client.HTTPException):
            if not responded:
                try:
                    self._error(502)
                except OSError:
                    pass
        finally:
            self.close_connection = True
            if connection is not None:
                connection.close()

    do_GET = _forward
    do_POST = _forward
    do_CONNECT = _forward
    do_PUT = _forward
    do_DELETE = _forward
    do_PATCH = _forward
    do_OPTIONS = _forward
    do_HEAD = _forward


def main(argv=None):
    parser = argparse.ArgumentParser(description="JEVRUNTIME isolated provider relay")
    parser.add_argument("--socket", default="/run/jev/provider.sock")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("A worker command is required after --")
    try:
        command, environment = prepare_worker(command, os.environ.copy(), args.port)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    server = _Server(("127.0.0.1", args.port), _Handler)
    server.socket_path = args.socket
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = None
    previous = {}

    def stop(signum, _frame):
        if process is not None and process.poll() is None:
            process.send_signal(signum)

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, stop)
        # The parent constructs the sanitized environment, including placeholder
        # keys. Never discover or read provider credentials inside the worker.
        process = subprocess.Popen(command, env=environment)
        code = process.wait()
        return code if code >= 0 else 128 - code
    except FileNotFoundError:
        print("Worker executable was not found", file=sys.stderr)
        return 127
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
