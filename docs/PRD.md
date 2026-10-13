PRD — Jev-Governed Supervised Terminal Runtime

1. Product Summary

A terminal-first software platform where a human describes, in plain language, how an agent runtime should behave. Jev acts as the semantic judgment layer, while deterministic Python code performs enforcement.

The platform can launch and supervise terminal agents such as Claude Code, Codex, Crush, or other command-line agent harnesses. Agents operate inside a runtime governed by a selected Constitution rather than one permanent, network-wide policy.

The core separation is:

> **Human defines intent → compiler translates → Jev judges meaning → deterministic code enforces → agents execute → runtime observes → Jev evaluates → deterministic code responds.**

Jev does not enforce actions itself. It returns bounded judgments. The runtime converts those judgments into concrete actions such as allow, deny, constrain, require approval, reduce privileges, restore privileges, quarantine, or stop.

────────

2. Product Goals

The platform must:

1. Let the user describe desired runtime behavior in natural language.
2. Preserve that description as the authoritative source of intent.
3. Convert human intent into machine-enforceable policy.
4. Use Jev to verify that compiled policy still means what the human requested.
5. Prefer deterministic Python enforcement over probabilistic LLM enforcement.
6. Run arbitrary supported terminal agent harnesses inside the supervised runtime.
7. Observe and grade agent actions continuously.
8. Intercept protected actions before they occur whenever technically possible.
9. Let the human modify active policy through natural language.
10. Support multiple independent Constitutions for different jobs and risk profiles.
11. Maintain evidence sufficient to distinguish actual completion from an agent merely claiming completion.
12. Keep enforcement, judgment, generation, execution, and evidence collection as separate responsibilities.

────────

3. Non-Goals

The system is not intended to make Jev an autonomous controller. Jev is a judge.

The system must not depend on an LLM voluntarily following policy when a deterministic control can enforce the same boundary.

The system must not require one universal Constitution governing every machine, project, agent, or session.

The system must not treat terminal text alone as proof that an action occurred successfully.

────────

4. Core Architecture

Human Intent Layer

The user creates or selects a Constitution and describes the desired operating conditions in plain language.

The original human-authored text is immutable source material for that Constitution version.

Policy Compiler

A compiler converts natural-language intent into:

• structured policy
• deterministic rules
• Jev judgment questions
• thresholds
• protected-resource definitions
• escalation rules
• evidence requirements
• completion requirements

An LLM may assist with compilation and may generate Python rule candidates, but generated enforcement code is never trusted merely because it was generated successfully.

Semantic Verification Layer

Jev compares the human’s source intent with the compiled representation.

Example questions include:

• Does this rule preserve the user’s stated restriction?
• Does this generated Python check permit behavior the user prohibited?
• Does this policy introduce authority that the user did not grant?
• Does this policy omit an important requirement from the source intent?

A failed semantic verification prevents activation.

Deterministic Validation Layer

Before activation, Python performs mechanical checks including:

• syntax validation
• schema validation
• unit tests
• policy conflict detection
• permission-boundary checks
• test action simulations
• fail-closed verification for protected operations

Runtime Supervisor

The supervisor launches and monitors terminal agents.

It owns the session state, selected Constitution, active policy version, permissions, event stream, enforcement state, and evidence record.

Enforcement Gateways

Protected capabilities must pass through actual enforcement controls rather than relying exclusively on terminal observation.

Possible gateways include:

• filesystem operations
• shell command execution
• Git operations
• network access
• credential access
• external APIs
• process control
• package installation
• deployment actions
• production systems
• destructive operations

Jev Judgment Layer

Jev is used when semantic interpretation is needed.

Judgment can occur at three principal levels:

Action Judgment

Evaluate a proposed action before execution.

Examples:

• Is this destructive?
• Is this action within the active Constitution?
• Does this exceed the authority granted for this session?
• Is this command materially different from what the user requested?

Behavioral Judgment

Evaluate patterns across multiple actions.

Examples:

• Is the agent drifting away from the task?
• Is repeated failure becoming unsafe or wasteful?
• Is the agent attempting alternate routes around a denied operation?
• Does recent behavior justify reducing autonomy?

Completion Judgment

Evaluate whether the requested outcome is actually supported by evidence.

Examples:

• Do the recorded results demonstrate completion?
• Are required verification steps missing?
• Is the agent claiming success without direct evidence?
• Do the artifacts match the user’s requested outcome?

Enforcement Controller

Python consumes Jev judgments plus deterministic state and performs the actual response.

Possible responses include:

• allow
• deny
• allow with restrictions
• require human approval
• redact
• route locally
• remove credential access
• restrict filesystem scope
• disable network access
• reduce command privileges
• quarantine a process
• terminate an agent
• restore privileges after successful verification

Jev never directly performs these actions.

────────

5. Constitutions

A Constitution defines the operating contract for a runtime session.

The platform supports many Constitutions rather than one system-wide Constitution.

Examples:

• Local Development
• Production Deployment
• Read-Only Research
• Repository Maintenance
• Infrastructure Administration
• Experimental Agent Sandbox
• Financially Sensitive Operations
• Client Production Environment

A session can select one Constitution or compose compatible layers.

Example:

```text
Base Host Protections
    + Local Development Constitution
    + Project-Specific Rules
    + Temporary User Instruction
```

Each Constitution is versioned and independently exportable.

Suggested Constitution Layout

```text
constitutions/
└── local-development/
    ├── intent.md
    ├── policy.yaml
    ├── jev_questions.yaml
    ├── thresholds.yaml
    ├── evidence.yaml
    ├── permissions.yaml
    ├── rules/
    │   ├── filesystem.py
    │   ├── network.py
    │   ├── git.py
    │   └── process.py
    └── tests/
        ├── allowed_cases.yaml
        └── denied_cases.yaml
```

────────

6. Natural-Language Policy Editing

The user can modify a running Constitution through plain language.

Example:

> For this session, allow changes anywhere in this repository, but require my approval before deleting files or pushing to a remote repository.

The system then:

1. records the human instruction;
2. compiles a proposed policy change;
3. shows the semantic difference internally;
4. asks Jev whether the compiled change preserves the instruction;
5. validates deterministic rules;
6. runs policy tests;
7. versions the Constitution;
8. activates the new version;
9. retains rollback capability.

The LLM that creates a policy modification cannot approve its own modification.

────────

7. Agent Integration

The platform should not require every agent vendor to implement Jev support.

Instead, the platform launches existing terminal harnesses inside its controlled runtime.

Initial adapters should support a generic process adapter plus specific integrations where useful.

Examples:

```text
runtime run --constitution local-dev --agent codex
runtime run --constitution production --agent claude
runtime run --constitution sandbox --agent crush
runtime run --constitution research -- command "some-agent"
```

Agent adapters handle launch, process state, terminal input/output, proposed actions where observable, and lifecycle events.

Enforcement remains outside the agent adapter whenever possible.

────────

8. API and Model Configuration

The platform requires a secure configuration layer for:

• Jev API credentials
• model provider credentials
• optional policy-compilation model
• optional agent-model credentials
• local model endpoints
• external service credentials

Credentials should be scoped so agents receive only the minimum access required for the active Constitution.

The runtime should support both hosted and local model endpoints without changing the Constitution model.

────────

9. Adaptive Autonomy

Autonomy is a runtime state controlled by deterministic code.

Jev can judge whether observed behavior warrants a change, but Python applies that change.

Example progression:

```text
NORMAL
  ↓ repeated unexplained failures
CAUTIOUS
  ↓ attempted protected action
RESTRICTED
  ↓ policy circumvention or high-risk anomaly
QUARANTINED
```

Recovery can also move in the opposite direction after defined verification requirements are satisfied.

Autonomy changes should modify concrete capabilities rather than merely changing prompt wording.

────────

10. Evidence and Completion

The runtime maintains an independent event and evidence record.

Possible evidence includes:

• command exit status
• before/after file hashes
• Git state
• test results
• process state
• filesystem changes
• network requests
• deployment receipts
• artifact checks
• policy decisions
• Jev judgments
• human approvals

An agent statement such as “done” is not evidence by itself.

Completion is evaluated against explicit acceptance conditions derived from the human’s request and active Constitution.

────────

11. Proposed Repository Scaffold

```text
jev-runtime/
├── README.md
├── pyproject.toml
├── config/
│   ├── providers.yaml
│   └── runtime.yaml
├── constitutions/
├── src/
│   └── jev_runtime/
│       ├── cli/
│       │   ├── main.py
│       │   ├── constitution.py
│       │   ├── run.py
│       │   └── inspect.py
│       ├── constitution/
│       │   ├── loader.py
│       │   ├── compiler.py
│       │   ├── verifier.py
│       │   ├── versioning.py
│       │   └── composition.py
│       ├── judgment/
│       │   ├── jev_client.py
│       │   ├── action.py
│       │   ├── behavior.py
│       │   └── completion.py
│       ├── enforcement/
│       │   ├── controller.py
│       │   ├── filesystem.py
│       │   ├── shell.py
│       │   ├── network.py
│       │   ├── git.py
│       │   ├── credentials.py
│       │   └── process.py
│       ├── agents/
│       │   ├── base.py
│       │   ├── generic.py
│       │   ├── codex.py
│       │   ├── claude.py
│       │   └── crush.py
│       ├── autonomy/
│       │   ├── controller.py
│       │   └── states.py
│       ├── evidence/
│       │   ├── events.py
│       │   ├── store.py
│       │   ├── verification.py
│       │   └── completion.py
│       ├── secrets/
│       │   └── manager.py
│       └── runtime/
│           ├── supervisor.py
│           ├── session.py
│           └── state.py
└── tests/
    ├── constitutions/
    ├── enforcement/
    ├── judgment/
    └── runtime/
```

────────

12. Core Runtime Flow

```text
USER INTENT
    │
    ▼
CONSTITUTION SOURCE
    │
    ▼
POLICY COMPILER
    │
    ├────► deterministic policy
    ├────► Python enforcement rules
    └────► Jev questions
    │
    ▼
JEV SEMANTIC VERIFICATION
    │
    ▼
PYTHON VALIDATION + TESTS
    │
    ▼
ACTIVATE POLICY VERSION
    │
    ▼
LAUNCH TERMINAL AGENT
    │
    ▼
PROPOSED / OBSERVED ACTION
    │
    ├────► deterministic checks
    ├────► Jev judgment when semantic judgment is required
    │
    ▼
PYTHON ENFORCEMENT CONTROLLER
    │
    ├────► ALLOW
    ├────► CONSTRAIN
    ├────► APPROVAL REQUIRED
    ├────► DENY
    └────► QUARANTINE
    │
    ▼
EXECUTION
    │
    ▼
EVIDENCE COLLECTION
    │
    ▼
BEHAVIOR + COMPLETION JUDGMENT
    │
    ▼
AUTONOMY / POLICY RESPONSE
```

────────

13. Security Principles

1. Human intent outranks generated policy.
2. Generated code cannot approve itself.
3. Jev judges; deterministic code enforces.
4. Agents receive minimum necessary authority.
5. Protected actions are intercepted before execution whenever possible.
6. Failure of a judgment service must not silently expand authority.
7. Policy changes are versioned and reversible.
8. Evidence is collected independently of agent claims.
9. Credentials belong to the runtime, not the agent.
10. Constitutions are scoped to the job instead of globally imposed.

────────

14. MVP

The first usable version should implement:

• Python terminal application
• Jev API client
• natural-language Constitution creation
• structured policy compiler
• Jev semantic verification
• Constitution versioning
• generic terminal-agent launcher
• filesystem enforcement
• shell-command enforcement
• Git enforcement
• credential isolation
• event/evidence store
• action-level Jev judgments
• completion judgments
• natural-language Constitution updates
• allow / deny / approval-required decisions
• basic autonomy states

The MVP should prove one central proposition:

> An unmodified terminal coding agent can operate normally while an independent Jev judgment layer and deterministic enforcement runtime continuously keep its actions aligned with a human-defined Constitution.

────────

15. Later Phases

Later releases can add:

• network-level enforcement
• multiple simultaneous agents
• cross-machine supervision
• shared home-network runtime control
• local-model routing
• sensitive-data classification and redaction
• automatic local-vs-cloud routing
• behavior baselines
• anomaly detection
• Constitution marketplace/import/export
• signed Constitutions
• remote runtime dashboard
• mobile approval interface
• machine-specific capability profiles
• temporary capability grants
• replay and forensic inspection
• automated recovery workflows
• Jev-based comparison of intended versus observed long-running behavior

────────

16. Home-Network Direction

The larger home-network implementation can run this platform as a distributed supervision layer without imposing one policy on the entire network.

Each machine, project, service, or task can activate its own Constitution while sharing common runtime infrastructure.

This creates a network in which:

• Mac, Linux, and other hosts can expose controlled capabilities;
• terminal agents can run on whichever machine is appropriate;
• Jev supplies independent semantic judgment;
• deterministic local controls remain responsible for enforcement;
• sensitive work can be kept local;
• cloud models can be used selectively;
• authority can expand or contract according to the active job;
• every important action can be tied back to human intent and evidence.

The end state is not one omnipotent AI controlling the network. It is a human-defined, multi-Constitution runtime fabric in which agents are replaceable workers, Jev is an independent judgment layer, and deterministic software remains the enforcement authority.
