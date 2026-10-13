# JEVRUNTIME

A terminal application for describing a job in ordinary language, approving its
Constitution, and running a terminal agent under an independent supervisor.
The canonical requirements are preserved in [docs/PRD.md](docs/PRD.md).
Implementation boundaries are tracked in [docs/PRD_STATUS.md](docs/PRD_STATUS.md).

## Start

Python 3.11+, Linux, Git, bubblewrap and util-linux are required for worker execution.
The operating system must permit unprivileged user namespaces. There is no unconfined
fallback when isolation is unavailable. Onboarding and inspecting records do not
require the execution backend.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
jev doctor
mkdir -p "$HOME/JevWork"
jev --output "$HOME/JevWork"
```

Inspect the implemented controls before configuration with `jev capabilities`.
This command reads no credentials, creates no ledger and calls no model. Add `--probe`
to check the host namespace backend; a successful probe only runs a readonly system
command, so it does not establish that a harness, provider or complete workflow works.
The same versioned control description is supplied to onboarding and JEV. It describes
staged file publication and launch authorization, including the limits of descendant
command and file-metadata observation. Deployment has no executor and cannot be
proposed, activated or approved as an operation, including from older stored policies.

Choose an existing folder owned by your account with `--output`. It holds your
project in `project/`, provider settings and history in `.jev/`, and isolated worker
copies in `.jev/workers/`. Reopen the same folder to resume your most recent interview.
The tool never creates the top-level folder for you or chooses the current directory
as an output location. With no destination it exits with code 2 before any prompts
or persistent writes. Help, `jev doctor` and `jev capabilities` need no output folder.

First interactive launch offers connection setup. Onboarding and worker models start unconfigured;
accepting defaults does not choose a generative endpoint or model. `jev --output "$HOME/JevWork" setup` lets you
explicitly select an endpoint and model, including an authentication-free local endpoint,
and separately choose whether a worker may reuse it. API keys are requested privately
only when that connection is needed, or read through its named environment variable.
`{task}` in the installed agent command is replaced with the approved requirements and
policy, for example `crush run {task}`.

You can reopen a saved interview, review its proposal, and record a revised request
while onboarding is disconnected. The exact request is saved and old authority is
suspended; no model reply or replacement policy is invented. After explicitly connecting
onboarding with `jev --output "$HOME/JevWork" setup`, reopen the interview and use `/retry`. Local policy review,
approval, export and already-verified typed completion checks do not initialize providers.

JEV setup defaults to OpenRouter's native Decisions API with `typesafe/jev-1.13`
and the `OPENROUTER_API_KEY` environment reference. The judgment base URL is
`https://openrouter.ai`, not the chat-completions `/api/v1` base. The official
TypeSafe SDK still builds Noul questions and validates answers; a fixed transport
routes them to `/api/alpha/decisions`. Direct TypeSafe service remains selectable,
and existing configurations without a judgment `provider` keep their direct route.
Review displays the resolved JEV model, each dimension's probability, threshold and API failure.
The onboarding model is a separately selected generative connection. A credential supplied
for JEV must not be assumed to authorize that connection. This project does not include
a ChatGPT subscription-to-API bridge; no additional model is selected by a JEV judgment.

Live OpenRouter calls have succeeded. The read-only sample scored below the current
0.95 floor and could not activate. That sample also explicitly forbade execution,
so it was not a valid positive fixture for launching a worker, which needs read and
execute authority. It does not establish a threshold defect. The floor is unchanged;
a complete live onboarding and governed worker flow still needs to be demonstrated.
See [the actual live results](docs/VERIFICATION.md#live-openrouter-jev).

Describe the job, permitted actions, protected files, and how to establish success.
The interviewer compiles a proposal with concrete file or command checks. A request
to run tests must have a command check; a text file saying they passed does not count.
Review includes the exact capabilities, checks, expected exit status/content, command
launch rules, thresholds and limits. JEV reviews meaning; only you can activate it.

- `/review` displays the current proposed Constitution.
- `/approve` records your approval and activates that version.
- `/context <note>` records operator observations or implementation suggestions separately
  from your human instructions. Notes cannot grant authority; changing them requires a
  fresh proposal and review. This supports a delegated operator using the app without
  attributing its own implementation plan to the human.
- `/run` launches the configured worker. `/run COMMAND ARG...` selects a command explicitly.
- `/retry` retries an unanswered message or a failed JEV review without duplicating your words.
- `/handoff` exports the approved requirements and interview as a brief.
- `/done` pauses. Starting `jev --output "$HOME/JevWork"` again resumes the interview.

An instruction changing the Constitution immediately suspends that session's old
version. Approving the replacement does not leave an older approval usable for the
same session. Each independent Constitution has its own activation history.

## What happens when an agent runs

1. The supervisor validates the active policy and authorizes project reads and the
   exact launch command. Missing authority or a failed judgment stops execution.
2. It copies eligible files into a separate private workspace, excluding credentials,
   runtime state, host Git metadata, agent configuration and dependency caches.
3. The agent runs inside new Linux namespaces. The host home and ledger are absent;
   system files are readonly; general networking is unavailable. A private Git
   baseline lets ordinary repository tools work without exposing host Git authority.
4. A fixed-destination model broker supplies the configured model connection. Real
   provider credentials remain in the supervisor. The worker sees placeholder keys.
   Remote tools, arbitrary proxy requests, redirects and alternate models are rejected.
5. The supervisor records process results and periodically reviews observed file
   changes with JEV. Stop requests, policy suspension, unsafe paths, repeated failures,
   failed behavior review, timeout or excessive output prevent publication.
6. File requirements and independent commands run against the result. Their content
   predicates/exit statuses are checked mechanically before JEV evaluates completion.
7. Proposed file changes are reviewed against the current Constitution and applied
   only after verification. The source must still match the captured baseline.
   Deletes require separate human authorization. Recorded completion binds to the
   actual published file hashes; later changes invalidate that result.

Completion inspection is read-only and makes no model calls. It checks the latest
completion, the exact worker and independent acceptance receipts, the recorded JEV
review, the publication, current authority and current host files. Missing evidence,
a later unfinished or failed attempt, or changed files returns `not_complete`.
Use `jev --output "$HOME/JevWork" complete PROPOSAL_ID --details` for the reason.

Old policies without explicit acceptance checks remain readable and exportable, but
need revision and human approval before use. Old completion labels cannot establish
success. For an existing typed-check job with retained worker files and unchanged
published artifacts, `jev --output "$HOME/JevWork" verify RUN_ID` can collect fresh evidence and record a
new attestation without republishing files. If its evidence or artifacts are gone,
start a fresh run; do not restore an old success label.

`CAUTIOUS` requires additional human approval for operations. `RESTRICTED` and
`QUARANTINED` prevent ordinary execution/publication. Recovery requires fresh checks
and human approval; JEV cannot restore capabilities on its own.

`jev --output "$HOME/JevWork" recover PROPOSAL_ID` shows the approved checks and asks before running them
in a read-only copy. Worker capabilities stay restricted throughout verification.
Recovery requires unchanged files, current authority and a fresh JEV review; a
new failure invalidates the attempt. The resulting evidence can restore
capabilities once. Recovery records appear in `jev --output "$HOME/JevWork" status`; publication and
completion records remain unchanged.

## Inspect an interrupted publication

If the process stops while applying files, an unfinished publication record blocks
ordinary operations for that Constitution and other Constitutions targeting the same
project. This restriction is derived from the evidence even when the process died
before it could record an error. An unrelated project remains independent.

Run `jev --output "$HOME/JevWork" reconcile RUN_ID` to inspect the attempt. The report
compares the project with its original baseline and recorded worker result, reports
missing or divergent files, and lists affected Constitutions. This command is read-only
and makes no model calls. A matching tree alone never certifies completion.

To close an interrupted attempt, use
`jev --output "$HOME/JevWork" reconcile RUN_ID --abandon`. The app refuses while the
publisher is busy, shows the observed state and asks for human confirmation. It leaves
all project files as they are, retains the original evidence, and records abandonment
with quarantine for the affected active Constitutions. It rejects files or evidence
that change during review. An abandoned run cannot later be verified or published.

Review and repair divergent or incomplete files before recovery. Existing outcome
checks may fail on an unchanged baseline, so recovery is not guaranteed to be available;
a separately approved repair Constitution is another option after the attempt closes.
Recovery requires fresh checks and judgment. Start a new worker for a new completion
record. No automatic rollback or crash-atomic multi-file publication is claimed.

## Continue and manage a job

```sh
jev --output "$HOME/JevWork" run -- python3 worker.py
jev --output "$HOME/JevWork" run --no-provider -- python3 local-worker.py
jev --output "$HOME/JevWork" run --interactive -- some-terminal-agent
jev --output "$HOME/JevWork" status
jev --output "$HOME/JevWork" stop RUN_ID
jev --output "$HOME/JevWork" verify RUN_ID
jev --output "$HOME/JevWork" apply RUN_ID
jev --output "$HOME/JevWork" complete PROPOSAL_ID
jev --output "$HOME/JevWork" complete PROPOSAL_ID --details
jev --output "$HOME/JevWork" revoke PROPOSAL_ID
jev --output "$HOME/JevWork" recover PROPOSAL_ID
jev --output "$HOME/JevWork" reconcile RUN_ID
jev --output "$HOME/JevWork" reconcile RUN_ID --abandon
jev --output "$HOME/JevWork" constitution list
jev --output "$HOME/JevWork" constitution export PROPOSAL_ID "$HOME/JevWork/constitution.json"
jev --output "$HOME/JevWork" constitution import "$HOME/JevWork/constitution.json"
jev --output "$HOME/JevWork" constitution rollback OLD_PROPOSAL_ID
jev --output "$HOME/JevWork" constitution compose FIRST_ID SECOND_ID --name 'Combined job'
```

Import, rollback and composition create a new proposal, re-run verification and require
human approval. Composition intersects capabilities, combines protections/requirements,
and refuses conflicting checks. Use `--proposal ID` on `run` to select an independently
approved Constitution. Interviews can also be selected with `jev --output "$HOME/JevWork" onboard --session ID`.

To work on an existing project, start with
`jev --output "$HOME/JevWork" onboard --workspace /absolute/path/to/your/project`.
Reopening that output folder preserves the recorded project. Use `onboard --new` to
start a separate interview. A selected session or Constitution cannot be retargeted
by passing a different `--workspace`; its intent and policy keep their recorded path.
The output folder and publication destinations must be owned by the calling account.
Read-only research can use a project the caller can read and traverse without requiring
write permission. Existing worker storage must belong to the caller and stay outside
the project.

Existing explicit layouts remain usable with `--state /absolute/path/to/ledger.sqlite`,
optionally `--config /absolute/path/to/providers.toml` and `--run-dir /absolute/path/to/workers`.
Use those flags instead of `--output`; their parent folders must already exist.
Without explicit overrides, legacy settings and worker storage are resolved beside
the selected ledger. `apply` and `verify` reuse a saved run's recorded worker directory,
including older layouts. Supply the previous `--config` path when reconnecting an old
interview whose settings were stored elsewhere. Configuration alone is not an output
selection for a job. Setup preserves previous settings in a dated adjacent backup.

`status`, `inspect`, `complete` and Constitution listing/export open existing history
read-only. They do not create an empty replacement when the ledger is missing, change
its permissions or initialize a model. Exports still require an explicit output file
in a folder owned by the caller.

## Agent models and installations

Crush uses the broker's Chat Completions endpoint. Codex configuration uses Responses;
Claude configuration uses Anthropic Messages. The selected provider must support the
protocol for that harness. Choose `[agent].provider` explicitly:

- `unconfigured` defers the connection and prevents a model-backed worker launch.
- `none` launches without a model broker; `jev --output "$HOME/JevWork" run --no-provider` also selects this mode.
- `onboarding` uses exactly the selected onboarding connection.
- `agent_provider` uses exactly the separate `[agent_provider]` connection, with its own
  `provider = "openai-compatible"`, `base_url`, `model`, `api_key_env` and optional
  `auth_required` fields.

An unused worker section cannot override an explicit onboarding selection. There is
no fallback between connections. Generative connections cannot reference JEV's
credential environment name. Hidden credential caching is scoped by connection purpose;
a known matching JEV credential value is rejected before generation, without retaining
that rejected entry in the generative cache. Credentials are never saved in the config.

The relay creates temporary vendor configuration in the isolated home. Existing host
login sessions, keys, MCP configurations and project agent configuration are not copied.
Crush was exercised through its real write tool against a local simulated provider.
Codex and Claude configuration adapters have not been exercised against live providers.

Executables under system installation paths are visible. Non-system installations need
an explicit absolute command and narrowly scoped `[agent].tool_roots` paths. Source,
credential and ledger directories cannot be mounted as tool installations. Dependencies
must be available in the isolated installation: host `.venv` and `node_modules` are not
silently exposed, and general package downloads are not enabled.

## Host Git operations

Worker Git operations affect only the private copy. Host commits and pushes use a
separate gateway with a reviewed plan, current-policy check, repository-state binding,
and individual human approval.

```sh
jev --output "$HOME/JevWork" git status
jev --output "$HOME/JevWork" git commit --message 'Describe the reviewed change'
jev --output "$HOME/JevWork" git push --remote origin --branch main
```

Commit uses only the already-staged index; it never stages unrelated files. The plan
contains the complete staged patch, the exact index object inventory, and the active
policy hash. Protected source/destination paths (including deletions and renames),
symlinks, submodules, unresolved index entries, observable hard links and unreviewable
content are rejected before semantic review. Status excludes protected worktree files
from both its Git subprocess path list and content hashing.

Push pins the reviewed HEAD to one named branch, without force, deletion, hooks or
inherited credential helpers. It checks every reachable commit tree, including deleted
historical paths and alternate filenames sharing the same blob. Its review includes
all distinct file contents and complete commit metadata. The current complete-review
bounds are 256 commits and 16,000 aggregate content/metadata bytes; commit patches
have a separate 16,000-byte limit. Large histories, binary data and shallow repositories
stop with an error. They require a future review strategy before this gateway can push
them. Local tracking refs are not treated as proof that the destination already has
historical data. A copied secret under an ordinary filename is not detectable by a
path rule alone; JEV remains a probabilistic content reviewer.

The gateway regenerates the exact versioned plan before execution. Legacy or
policy-mismatched mutation plans cannot reach JEV or gain authority through human
confirmation. Approval, consumption and the Git attempt are recorded together before
execution. An exception or missing success receipt leaves the outcome uncertain and
blocks ordinary operations, completion and recovery for affected Constitutions in
that history. Git and its transport helpers inherit the repository lock, so the loss
of the parent terminal process does not release the lock while they continue running.

Inspect an interrupted operation without model calls or active-policy requirements:

```sh
jev --output "$HOME/JevWork" git reconcile
jev --output "$HOME/JevWork" git reconcile --attempt ATTEMPT_ID
jev --output "$HOME/JevWork" git reconcile --attempt ATTEMPT_ID --abandon
```

The first command lists attempts, including legacy operations that lack a success
receipt. The second reports the exact approved ref and current repository observations.
It does not contact the remote or infer whether a push succeeded. The last asks for
human confirmation, rechecks the repository and history under the Git lock, and closes
the selected attempt while retaining quarantine. It changes no files or refs, cannot
cancel a remote push, and grants no retry or completion. Fresh `recover PROPOSAL_ID`
checks and approval are still required to restore capabilities.

The repository lock coordinates Git execution and Git abandonment. File publication
uses the supervisor's worker-directory lock; there is no universal cross-runtime
mutation lock or uncertainty propagation between separate history files. A push may
have taken effect before a connection failed; local abandonment never proves otherwise.

Optional Git credentials are requested privately for that operation.
A trusted HTTP remote requires explicit `--allow-http`; credentials on that transport
are plaintext. HTTPS is the default. No credentials are copied into worker processes.

## Deliberate limits

- Linux namespace isolation is the implemented backend; other operating systems need
  an execution backend before they can supervise workers.
- Command prefixes authorize worker launches and independent checks. Descendant shell
  commands share the sandbox's file/network capabilities; individual descendant
  commands are not intercepted and submitted to JEV. The interviewer must not promise
  unsupported command-specific restrictions.
- Publication currently requires complete UTF-8 text for semantic file review, bounded
  at 32 KB per changed file and 32 KB per JEV context. Binary/oversized changes fail
  closed. Workspace snapshots are bounded at 64 MiB and 20,000 files.
- Wall time, output, individual file size and process limits are enforced. There is
  no aggregate cgroup memory/disk quota or monetary spending guarantee. Model inference
  is capped at 128 requests per worker invocation.
- Publication preflights files and attempts rollback on caught failures. It is not a
  filesystem-wide transaction across power loss. The host user/administrator is trusted;
  worker isolation does not protect against malicious code already running as that user.
- The local ledger rejects application updates/deletes and verifies a hash chain; it
  does not provide an external signed checkpoint against host-owner replacement.
- Deployment gateways, arbitrary network access, distributed/multi-agent supervision,
  signed Constitutions and the other PRD later-phase features remain unimplemented.

`--demo` explicitly selects simulated onboarding/JEV providers and a separate ledger.
It does not turn real worker execution into a simulation. Use a disposable project for
demo worker runs. Live provider failure never falls back to simulated approval.

Research and attribution: [docs/RESEARCH.md](docs/RESEARCH.md).
Actual verification and its limits: [docs/VERIFICATION.md](docs/VERIFICATION.md).
