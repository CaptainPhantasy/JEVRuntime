"""Code-owned control boundaries, not grants or an overall readiness claim."""

SUPPORTED_ACTIONS = frozenset({"read", "write", "delete", "execute", "git", "push"})
CONTROL_VERSION = "jev-controls-4"


def control_manifest():
    """Describe implemented boundaries without probing hosts or model providers."""
    return {
        "version": CONTROL_VERSION,
        "grants_authority": False,
        "supported_host_actions": sorted(SUPPORTED_ACTIONS),
        "implemented_controls": {
            "filesystem": "Workers receive an isolated project copy. Protected paths are excluded. Each host publication change is reviewed separately; writes/deletions inside a write-enabled scratch copy are not individually intercepted.",
            "shell": "Worker launches and independent checks pass through host authorization. Command prefixes waive extra operation approval; they do not filter descendant commands. Descendants inherit namespace/file/network restrictions.",
            "network": "Worker namespaces have no general external network. A fixed model broker is the sole configured provider route; credentials remain on the host.",
            "git": "Host commit and pinned non-force push plans require policy-bound protected-path and complete bounded text review, JEV judgment and separate exact-operation approval. Push reviews all reachable history; oversized, binary and shallow history fail closed. Durable uncertain Git attempts quarantine affected authority until explicit inspection/abandonment and fresh recovery. Abandonment does not establish a remote outcome. Scratch Git is separate from host metadata.",
            "evidence": "Every acceptance requirement needs an explicit file or command predicate. Completion requires linked successful worker, independent check, JEV and publication receipts, plus unchanged host artifacts and current authority. Historical unchecked policies require revision; historical completion labels require fresh verification.",
            "observation": "Periodic file metadata snapshots support file-scope review. They do not establish subprocess activity, file-content quality, or absence of attempted circumvention.",
            "limits": "Time, output, file counts/sizes, per-process resources and model request counts are bounded. These are not aggregate cgroup or monetary budgets.",
        },
        "unsupported_controls": [
            "deployment",
            "general_external_network_gateway",
            "package_installation_gateway",
            "crash_atomic_multi_file_publication",
            "per_descendant_command_allowlist",
            "per_scratch_file_operation_interception",
            "complete_subprocess_behavior_observation",
            "aggregate_memory_disk_or_monetary_budget",
        ],
        "requirements": [
            "Linux with working bubblewrap and util-linux namespaces",
            "An installed terminal harness and explicitly exposed tool installations",
            "An active human-approved Constitution",
            "Explicit model connections when the selected workflow needs them",
        ],
    }


def capability_report(*, probe=False):
    report = control_manifest()
    report["host_probe"] = {"checked": False, "available": None}
    if probe:
        from .sandbox import Sandbox

        report["host_probe"] = {
            **Sandbox().check(),
            "checked": True,
            "scope": "Readonly /usr/bin/true under the namespace backend only; no harness or model was tested.",
        }
    return report
