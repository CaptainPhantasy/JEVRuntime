import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import StrEnum


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True)
class Judgment:
    # For a multi-question rubric this is min(dimensions), a host gate value.
    # It is not a probability of the conjunction or a calibrated success estimate.
    probability: float | None
    model: str
    error: str | None = None
    dimensions: dict[str, float] = field(default_factory=dict)
    rubric_version: str | None = None
    request_hash: str | None = None
    provider_metadata: dict[str, int | str] = field(default_factory=dict)

    def passes(self, threshold=0.95):
        # Fixed bootstrap floor; model output cannot lower this threshold.
        p = self.probability
        return (
            self.error is None
            and type(p) in (float, int)
            and math.isfinite(p)
            and threshold <= p <= 1
            and (self.rubric_version is None or bool(self.dimensions))
            and all(
                type(value) in (float, int) and math.isfinite(value) and threshold <= value <= 1
                for value in self.dimensions.values()
            )
            and (not self.dimensions or p == min(self.dimensions.values()))
        )


@dataclass(frozen=True)
class EvidenceCheck:
    requirement: str
    kind: str
    path: str = ""
    argv: tuple[str, ...] = ()
    expected_exit: int = 0
    contains: str = ""

    @classmethod
    def parse(cls, raw):
        if not isinstance(raw, dict) or set(raw) - {
            "requirement",
            "kind",
            "path",
            "argv",
            "expected_exit",
            "contains",
        }:
            raise ValueError("Invalid evidence check")
        if raw.get("kind") not in {"file", "command"} or not isinstance(
            raw.get("requirement"), str
        ):
            raise ValueError("Evidence needs a requirement and a file or command check")
        path = raw.get("path", "")
        argv = raw.get("argv", [])
        if (
            not isinstance(path, str)
            or not isinstance(argv, list)
            or any(not isinstance(x, str) or not x or "\0" in x for x in argv)
        ):
            raise ValueError("Invalid check path or command")
        if raw["kind"] == "file" and (not safe_relative(path) or argv):
            raise ValueError("File checks need a relative path and no command")
        if raw["kind"] == "command" and (not argv or path):
            raise ValueError("Command checks need an argument list and no path")
        if (
            type(raw.get("expected_exit", 0)) is not int
            or not 0 <= raw.get("expected_exit", 0) <= 255
        ):
            raise ValueError("Invalid expected exit status")
        if not isinstance(raw.get("contains", ""), str):
            raise ValueError("Invalid expected file content")
        return cls(
            raw["requirement"],
            raw["kind"],
            path,
            tuple(argv),
            raw.get("expected_exit", 0),
            raw.get("contains", ""),
        )

    def as_dict(self):
        return {
            "requirement": self.requirement,
            "kind": self.kind,
            "path": self.path,
            "argv": list(self.argv),
            "expected_exit": self.expected_exit,
            "contains": self.contains,
        }


def safe_relative(value):
    from pathlib import PurePosixPath

    return (
        bool(value)
        and not PurePosixPath(value).is_absolute()
        and ".." not in PurePosixPath(value).parts
        and "\0" not in value
    )


@dataclass(frozen=True)
class Policy:
    intent_id: str
    workspace: str
    actions: tuple[str, ...]
    required_evidence: tuple[str, ...]
    name: str = "Project Constitution"
    protected_paths: tuple[str, ...] = ()
    shell_commands: tuple[tuple[str, ...], ...] = ()
    checks: tuple[EvidenceCheck, ...] = ()
    judgment_threshold: float = 0.95
    max_seconds: int = 900
    max_failures: int = 3
    behavior_interval: int = 30

    @classmethod
    def parse(cls, raw):
        required = {
            "intent_id",
            "workspace",
            "actions",
            "required_evidence",
        }
        optional = {
            "name",
            "protected_paths",
            "shell_commands",
            "checks",
            "judgment_threshold",
            "max_seconds",
            "max_failures",
            "behavior_interval",
        }
        if not isinstance(raw, dict) or not required <= set(raw) or set(raw) - required - optional:
            raise ValueError("Invalid policy fields")
        if not all(isinstance(raw[k], str) and raw[k] for k in ("intent_id", "workspace")):
            raise ValueError("Policy requires source intent and workspace")
        for key in ("actions", "required_evidence"):
            if (
                not isinstance(raw[key], list)
                or not all(isinstance(x, str) and x for x in raw[key])
                or len(set(raw[key])) != len(raw[key])
            ):
                raise ValueError("Policy lists must contain unique nonempty strings")
        if not set(raw["actions"]) <= {
            "read",
            "write",
            "delete",
            "execute",
            "git",
            "push",
            "deploy",
        }:
            raise ValueError("Unknown capability")
        from pathlib import Path

        if not Path(raw["workspace"]).is_absolute():
            raise ValueError("Workspace must be absolute")
        protected = raw.get("protected_paths", [])
        if not isinstance(protected, list) or any(
            not isinstance(p, str) or not safe_relative(p) for p in protected
        ):
            raise ValueError("Protected paths must be relative project paths or patterns")
        commands = raw.get("shell_commands", [])
        if not isinstance(commands, list) or any(
            not isinstance(c, list)
            or not c
            or any(not isinstance(x, str) or not x or "\0" in x for x in c)
            for c in commands
        ):
            raise ValueError("Shell rules must contain command argument prefixes")
        checks = tuple(EvidenceCheck.parse(c) for c in raw.get("checks", []))
        if checks and (
            len(checks) != len(raw["required_evidence"])
            or {c.requirement for c in checks} != set(raw["required_evidence"])
        ):
            raise ValueError("Every acceptance requirement needs exactly one deterministic check")
        if (commands or any(c.kind == "command" for c in checks)) and "execute" not in raw[
            "actions"
        ]:
            raise ValueError("Command rules or checks require explicit execute permission")
        threshold = raw.get("judgment_threshold", 0.95)
        if type(threshold) not in (int, float) or not 0.95 <= threshold <= 1:
            raise ValueError("Judgment threshold cannot weaken the 0.95 host floor")
        limits = {
            "max_seconds": (1, 86400, 900),
            "max_failures": (1, 10, 3),
            "behavior_interval": (5, 600, 30),
        }
        for name, (low, high, default) in limits.items():
            if type(raw.get(name, default)) is not int or not low <= raw.get(name, default) <= high:
                raise ValueError("Invalid runtime limit: " + name)
        if (
            not isinstance(raw.get("name", "Project Constitution"), str)
            or not raw.get("name", "Project Constitution").strip()
        ):
            raise ValueError("A Constitution needs a name")
        return cls(
            raw["intent_id"],
            raw["workspace"],
            tuple(raw["actions"]),
            tuple(raw["required_evidence"]),
            raw.get("name", "Project Constitution"),
            tuple(protected),
            tuple(tuple(c) for c in commands),
            checks,
            threshold,
            raw.get("max_seconds", 900),
            raw.get("max_failures", 3),
            raw.get("behavior_interval", 30),
        )

    @property
    def has_acceptance(self):
        return (
            bool(self.required_evidence)
            and len(self.checks) == len(self.required_evidence)
            and {check.requirement for check in self.checks} == set(self.required_evidence)
        )

    def as_dict(self):
        return {
            "intent_id": self.intent_id,
            "workspace": self.workspace,
            "actions": list(self.actions),
            "required_evidence": list(self.required_evidence),
            "name": self.name,
            "protected_paths": list(self.protected_paths),
            "shell_commands": [list(c) for c in self.shell_commands],
            "checks": [c.as_dict() for c in self.checks],
            "judgment_threshold": self.judgment_threshold,
            "max_seconds": self.max_seconds,
            "max_failures": self.max_failures,
            "behavior_interval": self.behavior_interval,
        }
