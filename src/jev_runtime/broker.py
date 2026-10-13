"""A fixed-destination model broker; provider credentials never enter the worker.

The Unix socket is the worker's sole provider capability. It is not a general HTTP
proxy. The caller must put it inside the worker's isolated network namespace via
the accompanying loopback relay and keep the ledger outside this directory.
"""

import ipaddress
import json
import os
import shutil
import socket
import socketserver
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

import httpx

DEFAULT_PATHS = ("/chat/completions", "/responses", "/messages", "/models")
MAX_RESPONSE = 32 * 1024 * 1024
MAX_SECONDS = 180
SHUTDOWN_SECONDS = 5


class _ActiveCall:
    """Track transport sockets so revocation also interrupts stalled streams."""

    def __init__(self, broker, connection):
        self.broker = broker
        self.lock = threading.Lock()
        self.sockets = {connection}
        self.cancelled = threading.Event()
        self.recorded = False
        self.method, self.path = "UNKNOWN", "/rejected"
        self.request_bytes = self.response_bytes = 0

    @staticmethod
    def _abort(connection):
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass

    def trace(self, name, info):
        # HTTPX forwards its documented transport trace extension to HTTPCore.
        # Track TCP and replacement TLS sockets before request headers are sent.
        if name in {"connection.connect_tcp.complete", "connection.start_tls.complete"}:
            stream = info.get("return_value")
            connection = stream.get_extra_info("socket") if stream is not None else None
            with self.lock:
                if connection is not None:
                    self.sockets.add(connection)
            if self.cancelled.is_set():
                if connection is not None:
                    self._abort(connection)
                raise RuntimeError("Provider request was cancelled")

    def attach(self, client):
        if self.cancelled.is_set():
            client.close()
            raise RuntimeError("Provider request was cancelled")

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            connections = tuple(self.sockets)
        for connection in connections:
            self._abort(connection)
        # The request thread owns Client.close(). Calling it here could wait for
        # a transport lock and defeat the supervisor's teardown deadline. Socket
        # shutdown interrupts current I/O; cancelled traces stop later connects.

    def finish(self, status):
        with self.lock:
            if self.recorded:
                return
            self.recorded = True
        self.broker._event(self.method, self.path, status, self.request_bytes, self.response_bytes)


def _inference_body(content, endpoint):
    """Do not turn inference access into provider-hosted tools or file access."""
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("A model request must be an object")
    for name in (
        "web_search_options",
        "mcp_servers",
        "container",
        "previous_response_id",
        "conversation",
        "background",
        # OpenRouter plugins and presets can activate hosted web search, while
        # models/route can substitute a model after the explicit model check.
        "plugins",
        "preset",
        "models",
        "route",
    ):
        if value.get(name):
            raise ValueError("Provider-side external capabilities are unavailable")
    requested_model = value.get("model")
    if isinstance(requested_model, str) and ":online" in requested_model.split("/")[-1]:
        raise ValueError("Provider-side web search model variants are unavailable")
    tools = value.get("tools", [])
    if not isinstance(tools, list) or any(
        not isinstance(tool, dict) or tool.get("type", "function") not in {"function", "custom"}
        for tool in tools
    ):
        raise ValueError("Only worker-executed function tools are available")

    def check(item):
        if isinstance(item, list):
            for child in item:
                check(child)
        elif isinstance(item, dict):
            for key, child in item.items():
                if isinstance(child, str) and (
                    key in {"file_id", "file_url"}
                    or (key in {"url", "image_url"} and not child.startswith("data:"))
                ):
                    raise ValueError("Provider-side remote file access is unavailable")
                check(child)

    check(value)
    if endpoint in {"/responses", "/chat/completions"}:
        value["store"] = False
    return json.dumps(value, separators=(",", ":")).encode()


def _base_url(value):
    parsed = urlsplit(value)
    try:
        local = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        local = parsed.hostname == "localhost"
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or (parsed.scheme == "http" and not local)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character in value for character in ("\r", "\n", "\\", "%"))
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise ValueError("Provider URL must be HTTPS or loopback HTTP without credentials")
    # Accessing port also validates malformed/out-of-range port syntax.
    parsed.port
    return value.rstrip("/"), parsed.path.rstrip("/")


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 4

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            self.broker._event("UNKNOWN", "/rejected", 503, 0, 0)
            return
        call = _ActiveCall(self.broker, request)
        # Register before starting the thread: shutdown must also account for a
        # connection whose handler has not been scheduled yet.
        with self.broker.active_condition:
            self.broker.active_calls.add(call)
            self.broker.calls_by_socket[request] = call
        try:
            super().process_request(request, client_address)
        except BaseException:
            call.cancel()
            self._finish_request(request)
            raise

    def _finish_request(self, request):
        with self.broker.active_condition:
            call = self.broker.calls_by_socket.pop(request, None)
        try:
            if call is not None and not call.recorded:
                call.finish(502 if call.cancelled.is_set() else 500)
        finally:
            with self.broker.active_condition:
                self.broker.active_calls.discard(call)
                self.broker.active_condition.notify_all()
            self.slots.release()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._finish_request(request)

    def handle_error(self, request, client_address):
        # Tracebacks can include provider exception details. Fail closed silently.
        pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "JEVProvider"
    sys_version = ""

    def setup(self):
        self.request.settimeout(15)
        super().setup()

    def handle(self):
        broker = self.server.broker
        with broker.active_condition:
            self.call = broker.calls_by_socket[self.connection]
        if broker.stopping.is_set():
            self.call.cancel()

        # A socket inactivity timeout alone permits an endless trickle of bytes.
        # Bound the full worker request as well, including header/body parsing.
        timer = threading.Timer(MAX_SECONDS, self.call.cancel)
        timer.daemon = True
        timer.start()
        try:
            super().handle()
        finally:
            timer.cancel()
            if self.call.cancelled.is_set():
                self.call.finish(502)

    def log_message(self, *_args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.call.finish(code)
        super().send_error(code, message, explain)

    def _error(self, status):
        body = b'{"error":"Provider request rejected"}\n'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _endpoint(self):
        broker = self.server.broker
        path = self.path
        # Only literal endpoint paths are capabilities. Reject encoded, absolute,
        # traversing and query-bearing targets instead of trying to normalize them.
        if not path.startswith("/") or any(c in path for c in ("%", "?", "#", "\\")):
            return None
        candidates = [path]
        for prefix in (broker.path_prefix, "/v1"):
            if prefix and path.startswith(prefix + "/"):
                candidates.append(path[len(prefix) :])
        return next((item for item in candidates if item in broker.allowed_paths), None)

    def _forward(self):
        broker = self.server.broker
        endpoint = self._endpoint()
        self.call.method = self.command
        self.call.path = endpoint or "/rejected"
        status, sent, received = 403, 0, 0
        responded = False
        try:
            if endpoint is None or self.command != ("GET" if endpoint == "/models" else "POST"):
                self._error(status)
                return
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) > 1:
                status = 400
                self._error(status)
                return
            if self.command == "POST" and not lengths:
                status = 411
                self._error(status)
                return
            try:
                size = int(lengths[0]) if lengths else 0
            except ValueError:
                size = -1
            if not 0 <= size <= broker.max_body or (self.command == "GET" and size):
                status = 413
                self._error(status)
                return
            content = self.rfile.read(size)
            received = len(content)
            self.call.request_bytes = received
            if received != size:
                status = 400
                self._error(status)
                return
            if self.command == "POST":
                try:
                    content = _inference_body(content, endpoint)
                    if broker.model and json.loads(content).get("model") != broker.model:
                        raise ValueError("The worker requested a model outside its configuration")
                except (ValueError, TypeError, RecursionError):
                    status = 403
                    self._error(status)
                    return
            with broker.request_lock:
                if broker.requests >= 128:
                    status = 403
                    self._error(status)
                    return
                broker.requests += 1
            headers = {"Accept-Encoding": "identity", "Content-Type": "application/json"}
            # An allowlist prevents worker-supplied credentials, cookies, hosts,
            # forwarding hints or proxy headers from reaching the upstream.
            for name in ("accept", "anthropic-version", "anthropic-beta", "openai-beta"):
                if self.headers.get(name):
                    headers[name] = self.headers[name]
            if endpoint == "/messages":
                if broker.api_key:
                    headers["x-api-key"] = broker.api_key
                headers.setdefault("anthropic-version", "2023-06-01")
            elif broker.api_key:
                headers["Authorization"] = "Bearer " + broker.api_key
            started = time.monotonic()
            with httpx.Client(
                timeout=httpx.Timeout(60, connect=10, write=30, pool=5),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                self.call.attach(client)
                with client.stream(
                    self.command,
                    broker.upstream_url + endpoint,
                    content=content,
                    headers=headers,
                    extensions={"trace": self.call.trace},
                ) as response:
                    # Never expose a redirect destination or forward credentials to it.
                    if 300 <= response.status_code < 400:
                        status = 502
                        self._error(status)
                        return
                    status = response.status_code
                    self.send_response(status)
                    self.send_header(
                        "Content-Type", response.headers.get("content-type", "application/json")
                    )
                    self.send_header("Connection", "close")
                    self.end_headers()
                    responded = True
                    for chunk in response.iter_bytes():
                        if (
                            sent + len(chunk) > MAX_RESPONSE
                            or time.monotonic() - started > MAX_SECONDS
                            or broker.stopping.is_set()
                        ):
                            status = 502
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        sent += len(chunk)
                        self.call.response_bytes = sent
        except (httpx.HTTPError, OSError, ValueError, RuntimeError):
            status = 502
            if not responded:
                try:
                    self._error(status)
                except OSError:
                    pass
        finally:
            self.close_connection = True
            self.call.finish(502 if self.call.cancelled.is_set() else status)

    do_GET = _forward
    do_POST = _forward
    do_PUT = _forward
    do_DELETE = _forward
    do_PATCH = _forward
    do_CONNECT = _forward
    do_OPTIONS = _forward
    do_HEAD = _forward


class ProviderBroker:
    """Expose only selected model endpoints, using host-held credentials.

    ``on_event`` receives a dictionary of method, path, status, request_bytes and
    response_bytes. Neither request/response bodies nor credentials are logged.
    ``base_url`` is the relay URL for the worker; ``upstream_url`` is host-only.
    """

    def __init__(
        self,
        socket_dir: Path,
        *,
        base_url: str,
        api_key: str,
        on_event=None,
        allowed_paths: tuple = DEFAULT_PATHS,
        max_body=2 * 1024 * 1024,
        model=None,
        auth_required=True,
    ):
        self.upstream_url, self.path_prefix = _base_url(base_url)
        if type(auth_required) is not bool:
            raise ValueError("Provider authentication requirement must be a boolean")
        if (
            not isinstance(api_key, str)
            or (auth_required and not api_key)
            or any(c in api_key for c in ("\r", "\n", "\0"))
        ):
            raise ValueError("A provider credential is required")
        if model is not None and (
            not isinstance(model, str)
            or not model.strip()
            or any(c in model for c in ("\r", "\n", "\0"))
        ):
            raise ValueError("Provider model must be a nonempty model identifier")
        if not allowed_paths or any(path not in DEFAULT_PATHS for path in allowed_paths):
            raise ValueError("Only supported model endpoints can be exposed")
        if not isinstance(max_body, int) or not 0 < max_body <= 2 * 1024 * 1024:
            raise ValueError("Invalid provider body limit")
        self.socket_dir = Path(socket_dir)
        self.socket_path = self.socket_dir / "provider.sock"
        self.relay_path = self.socket_dir / "relay.py"
        self.base_url = "http://127.0.0.1:8765"
        self.api_key, self.on_event = api_key, on_event
        self.model = model
        self.requests = 0
        self.request_lock = threading.Lock()
        self.allowed_paths, self.max_body = tuple(allowed_paths), max_body
        self.stopping = threading.Event()
        self.active_condition = threading.Condition()
        self.active_calls = set()
        self.calls_by_socket = {}
        self.event_failed = threading.Event()
        self.server = self.thread = None

    def _event(self, method, path, status, received, sent):
        if self.on_event is not None:
            try:
                self.on_event(
                    {
                        "method": method,
                        "path": path,
                        "status": status,
                        "request_bytes": received,
                        "response_bytes": sent,
                    }
                )
            except Exception:
                self.event_failed.set()
                raise

    def __enter__(self):
        if self.server is not None or self.stopping.is_set():
            raise RuntimeError("A provider broker context may be started only once")
        if self.socket_dir.is_symlink():
            raise ValueError("Broker directory cannot be a symlink")
        self.socket_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.socket_dir.chmod(0o700)
        if any(path.name not in {"relay.py"} for path in self.socket_dir.iterdir()):
            raise ValueError("Broker directory must contain only broker files")
        if self.relay_path.is_symlink() or (
            self.relay_path.exists() and not stat.S_ISREG(self.relay_path.stat().st_mode)
        ):
            raise ValueError("Invalid relay destination")
        shutil.copyfile(Path(__file__).with_name("relay.py"), self.relay_path)
        self.relay_path.chmod(0o600)
        try:
            # AF_UNIX limits the name passed to bind(), even when the filesystem
            # accepts the complete project path. A directory descriptor gives
            # Linux a short name for the same private socket inode; the worker
            # still connects through its read-only /run/jev/provider.sock mount.
            descriptor = os.open(self.socket_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                address = (
                    f"/proc/self/fd/{descriptor}/provider.sock"
                    if Path("/proc/self/fd").is_dir()
                    else str(self.socket_path)
                )
                self.server = _UnixServer(address, _Handler)
            finally:
                os.close(descriptor)
            self.server.broker = self
            self.server.slots = threading.BoundedSemaphore(4)
            self.socket_path.chmod(0o600)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
        except BaseException:
            if self.server is not None:
                self.server.server_close()
            self.socket_path.unlink(missing_ok=True)
            raise
        return self

    def __exit__(self, *_args):
        self.stopping.set()
        deadline = time.monotonic() + SHUTDOWN_SECONDS
        try:
            if self.server is not None:
                self.server.shutdown()
                with self.active_condition:
                    calls = tuple(self.active_calls)
                for call in calls:
                    call.cancel()
                with self.active_condition:
                    while self.active_calls and time.monotonic() < deadline:
                        self.active_condition.wait(timeout=max(0, deadline - time.monotonic()))
                    pending = tuple(self.active_calls)
                # A DNS/backend operation can outlive socket cancellation. Its trace
                # callback refuses any later connection, and its failed receipt must
                # exist before supervision is allowed to evaluate the worker result.
                for call in pending:
                    call.finish(502)
        finally:
            if self.server is not None:
                self.server.server_close()
            if self.thread is not None:
                self.thread.join(timeout=max(0, deadline - time.monotonic()))
            self.socket_path.unlink(missing_ok=True)
        if self.event_failed.is_set():
            raise RuntimeError("A provider audit event could not be recorded")
