"""Versioned human intent and conservative Constitution composition."""

import json
from pathlib import Path
from uuid import uuid4

from .contracts import Policy, digest


def history(runtime):
    return [
        {
            "proposal_id": identifier,
            "constitution_id": runtime.constitution_id(identifier),
            "name": p["policy"].get("name", "Project Constitution"),
            "workspace": p["policy"]["workspace"],
            "semantic_pass": p["semantic_pass"],
            "active": runtime._active(identifier)[1],
        }
        for identifier, p in runtime.store.records("policy_proposal")
    ]


def export_policy(runtime, proposal_id, destination):
    proposal = runtime.store.get(proposal_id, "policy_proposal")
    bundle = {
        "format": "jevruntime.constitution.v1",
        "policy": proposal["policy"],
        "source": runtime.store.intent_source(proposal["policy"]["intent_id"]),
    }
    bundle["sha256"] = digest(bundle)
    with Path(destination).open("x", encoding="utf-8") as stream:
        Path(destination).chmod(0o600)
        json.dump(bundle, stream, indent=2, ensure_ascii=False)
    return destination


def import_policy(runtime, filename):
    raw = Path(filename).read_bytes()
    if len(raw) > 256000:
        raise ValueError("Constitution export exceeds the import limit")
    bundle = json.loads(raw)
    checksum = bundle.pop("sha256", None)
    if bundle.get("format") != "jevruntime.constitution.v1" or digest(bundle) != checksum:
        raise ValueError("Invalid Constitution format or checksum")
    source = bundle["source"]
    statements = source.get("human_statements", [{"text": source.get("text", "")}])
    identifiers = [runtime.store.record_intent(s["text"]) for s in statements]
    intent = runtime.store.append(
        "intent_bundle",
        {
            "utterance_ids": identifiers,
            "context": source.get("conversation_context", []),
            "imported_from_hash": checksum,
        },
    )
    raw_policy = {**bundle["policy"], "intent_id": intent}
    return runtime.propose(raw_policy, constitution_id=str(uuid4()))


def rollback(runtime, proposal_id):
    previous = runtime.store.get(proposal_id, "policy_proposal")
    return runtime.propose(previous["policy"], constitution_id=runtime.constitution_id(proposal_id))


def compose(runtime, proposal_ids, name):
    if len(proposal_ids) < 2:
        raise ValueError("Select at least two Constitutions to compose")
    policies = [
        Policy.parse(runtime.store.get(p, "policy_proposal")["policy"]) for p in proposal_ids
    ]
    if len({p.workspace for p in policies}) != 1:
        raise ValueError("Composed Constitutions must have the same workspace")
    actions = set.intersection(*(set(p.actions) for p in policies))
    checks = {}
    for policy in policies:
        for check in policy.checks:
            if check.requirement in checks and check != checks[check.requirement]:
                raise ValueError("Conflicting acceptance checks: " + check.requirement)
            checks[check.requirement] = check
    commands = list(policies[0].shell_commands)
    for policy in policies[1:]:
        commands = [
            a if len(a) >= len(b) else b
            for a in commands
            for b in policy.shell_commands
            if a[: len(b)] == b or b[: len(a)] == a
        ]
    utterances, context = [], []
    for policy in policies:
        source = runtime.store.intent_source(policy.intent_id)
        if "human_statements" in source:
            utterances.extend(s["id"] for s in source["human_statements"])
            context.extend(source["conversation_context"])
        else:
            utterances.append(policy.intent_id)
    intent = runtime.store.append(
        "intent_bundle",
        {
            "utterance_ids": list(dict.fromkeys(utterances)),
            "context": context,
            "composed_from": proposal_ids,
        },
    )
    raw = policies[0].as_dict()
    raw.update(
        {
            "intent_id": intent,
            "name": name,
            "actions": sorted(actions),
            "required_evidence": list(
                dict.fromkeys(r for p in policies for r in p.required_evidence)
            ),
            "protected_paths": sorted({r for p in policies for r in p.protected_paths}),
            "shell_commands": [list(c) for c in sorted(set(commands))],
            "checks": [c.as_dict() for c in checks.values()],
            "judgment_threshold": max(p.judgment_threshold for p in policies),
            "max_seconds": min(p.max_seconds for p in policies),
            "max_failures": min(p.max_failures for p in policies),
            "behavior_interval": min(p.behavior_interval for p in policies),
        }
    )
    return runtime.propose(raw, constitution_id=str(uuid4()))
