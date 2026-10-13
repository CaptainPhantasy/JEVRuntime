import json
import os
import shlex
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4


def endpoint(value):
    parsed = urlparse(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Endpoint must not contain credentials, query, or fragment")
    if not parsed.hostname or (
        parsed.scheme != "https"
        and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
    ):
        raise ValueError("Use HTTPS or a loopback HTTP model endpoint")
    return value.rstrip("/")


class ConnectionRequired(ValueError):
    """A local explanation, safe to show without provider response contents."""


def _connection(settings, label, *, generative=False):
    if not isinstance(settings, dict):
        raise ValueError(label + " settings must be a table")
    if "api_key" in settings:
        raise ValueError("Use environment references, never saved API keys")
    if settings.get("provider", "unconfigured") == "unconfigured":
        if settings.get("base_url") or settings.get("model"):
            raise ValueError(label + " is deferred but still contains an endpoint or model")
        return False
    endpoint(settings["base_url"])
    for field in ("model", "api_key_env"):
        if not isinstance(settings.get(field), str) or not settings[field].strip():
            raise ValueError(label + " needs a model and credential environment name")
    if type(settings.get("auth_required", True)) is not bool:
        raise ValueError("auth_required must be true or false")
    if generative and settings.get("provider") != "openai-compatible":
        raise ValueError(label + " must use an explicitly selected OpenAI-compatible endpoint")
    return True


def load(path):
    """Read references only; validate connection semantics when that role is used."""
    raw = tomllib.loads(Path(path).read_text())
    for section in ("onboarding", "judgment"):
        raw.setdefault(section, {"provider": "unconfigured"})
    raw.setdefault("agent", {})
    for section in ("onboarding", "judgment", "agent_provider", "agent"):
        if section not in raw:
            continue
        settings = raw[section]
        if not isinstance(settings, dict):
            raise ValueError(section + " settings must be a table")
        if "api_key" in settings:
            raise ValueError("Use environment references, never saved API keys")
    # Preserve older explicit direct-TypeSafe and separate-worker sections.
    raw["judgment"].setdefault("provider", "typesafe")
    if "agent_provider" in raw:
        raw["agent_provider"].setdefault("provider", "openai-compatible")
    raw["agent"].setdefault("provider", "unconfigured")
    return raw


def judgment_settings(config):
    settings = config.get("judgment", {"provider": "unconfigured"})
    if not _connection(settings, "JEV"):
        raise ConnectionRequired("JEV is not connected. Run jev setup to select its connection.")
    if settings["provider"] not in {"typesafe", "openrouter"}:
        raise ValueError("Judgment provider must be typesafe or openrouter")
    if (
        settings["provider"] == "openrouter"
        and endpoint(settings["base_url"]) != "https://openrouter.ai"
    ):
        raise ValueError("OpenRouter JEV base_url must be https://openrouter.ai")
    timeout = settings.get("timeout_seconds", 15)
    if type(timeout) not in (int, float) or not 0 < timeout <= 60:
        raise ValueError("Judgment timeout must be between 0 and 60 seconds")
    return settings


def generative_settings(config, role):
    if role == "onboarding":
        section = "onboarding"
    elif role == "worker":
        section = config.get("agent", {}).get("provider", "unconfigured")
        if section == "none":
            return None
        if section == "unconfigured":
            raise ConnectionRequired(
                "Worker model is not connected. Choose it with jev setup, or use --no-provider."
            )
        if section not in {"onboarding", "agent_provider"}:
            raise ValueError("Unknown worker connection; no provider was selected")
    else:
        raise ValueError("Unknown generative connection role")
    settings = config.get(section, {"provider": "unconfigured"})
    if not _connection(settings, role, generative=True):
        raise ConnectionRequired(
            role.capitalize()
            + " model is not connected. Run jev setup to explicitly select a connection."
        )
    if settings.get("auth_required", True) and settings["api_key_env"] == config.get(
        "judgment", {}
    ).get("api_key_env"):
        raise ValueError("The JEV credential reference cannot be used for a generative connection")
    return settings


def secret(settings):
    if settings.get("auth_required") is False:
        return ""
    value = os.environ.get(settings["api_key_env"], "")
    if not value.strip():
        raise ValueError("Required provider credential is not configured")
    return value


def setup(path):
    """Persist explicit connection choices and key references, never credentials."""

    def ask(label, default=""):
        suffix = f" [{default}]" if default else ""
        return input(label + suffix + ": ").strip() or default

    def choose_connection(label, key_env):
        base_url = ask(label + " API base URL (required)")
        if not base_url:
            raise ValueError("Choose an endpoint explicitly or leave the connection deferred")
        base_url = endpoint(base_url)
        model = ask(label + " model (required)")
        if not model:
            raise ValueError("Choose a model explicitly or leave the connection deferred")
        requires_key = ask(
            "Does this endpoint require an API key? yes/no",
            "no" if urlparse(base_url).hostname in {"localhost", "127.0.0.1", "::1"} else "yes",
        ).lower()
        if requires_key not in {"yes", "no"}:
            raise ValueError("Choose yes or no for API-key authentication")
        return {
            "provider": "openai-compatible",
            "base_url": base_url,
            "model": model,
            "api_key_env": ask(label + " credential environment name", key_env),
            "auth_required": requires_key == "yes",
        }

    print("Select connections explicitly. Setup makes no API calls and saves no keys.")
    connect = ask("Connect a generative onboarding model now? yes/no", "no").lower()
    if connect not in {"yes", "no"}:
        raise ValueError("Choose yes or no for onboarding setup")
    onboarding = (
        choose_connection("Onboarding", "JEV_ONBOARDING_API_KEY")
        if connect == "yes"
        else {"provider": "unconfigured"}
    )
    judgment_provider = ask("JEV provider: openrouter/typesafe", "openrouter").lower()
    if judgment_provider not in {"openrouter", "typesafe"}:
        raise ValueError("Choose openrouter or typesafe")
    if judgment_provider == "openrouter":
        judgment_url = "https://openrouter.ai"
        judgment_model = ask("JEV model", "typesafe/jev-1.13")
        judgment_key_env = "OPENROUTER_API_KEY"
    else:
        judgment_url = endpoint(ask("TypeSafe API base URL", "https://api.typesafe.ai"))
        judgment_model = ask("JEV model", "jev-1.13.0")
        judgment_key_env = "TYPESAFE_API_KEY"
    agent_command = shlex.split(
        ask("Terminal agent command ({task} inserts your instructions)", "crush run {task}")
    )
    if not agent_command:
        raise ValueError("Choose an installed terminal agent command")
    worker = ask(
        "Worker model connection: unconfigured/none/onboarding/agent_provider", "unconfigured"
    ).lower()
    if worker not in {"unconfigured", "none", "onboarding", "agent_provider"}:
        raise ValueError("Choose an explicit worker connection")
    if worker == "onboarding" and onboarding["provider"] == "unconfigured":
        raise ValueError("Connect onboarding before explicitly reusing it for the worker")
    sections = {
        "onboarding": onboarding,
        "judgment": {
            "provider": judgment_provider,
            "base_url": judgment_url,
            "model": judgment_model,
            "api_key_env": judgment_key_env,
            "timeout_seconds": 15,
        },
        "agent": {"command": agent_command, "provider": worker, "tool_roots": []},
    }
    if worker == "agent_provider":
        sections["agent_provider"] = choose_connection("Worker", "JEV_WORKER_API_KEY")
    content = "\n".join(
        "["
        + name
        + "]\n"
        + "".join(key + " = " + json.dumps(value) + "\n" for key, value in settings.items())
        for name, settings in sections.items()
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".providers-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        configured = load(temporary)
        judgment_settings(configured)
        if onboarding["provider"] != "unconfigured":
            generative_settings(configured, "onboarding")
        if worker != "unconfigured":
            generative_settings(configured, "worker")
        if path.exists():
            backup = path.with_name(
                path.name
                + ".backup-"
                + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                + "-"
                + uuid4().hex[:8]
            )
            with backup.open("xb") as saved:
                backup.chmod(0o600)
                saved.write(path.read_bytes())
            print("Previous settings preserved: " + str(backup))
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
    print(
        "Connection settings saved. Deferred models remain disconnected; keys are requested only when needed."
    )
