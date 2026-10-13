# PRD implementation status

The canonical PRD is unchanged. This file describes the implementation, rather than
redefining the requirements to match it. A successful local demonstration is not a
claim that every PRD requirement is complete.

## First-usable-version requirements

| PRD MVP function | Current implementation | Remaining boundary |
| --- | --- | --- |
| Python terminal application | Explicit member output folder, resumable guided setup, interview, review, run, verify, apply, reconcile, stop, revoke, recover, Git and Constitution commands | Terminal installation remains required; no desktop/web UI is claimed |
| JEV client | Official TypeSafe SDK with direct TypeSafe and native OpenRouter Decisions routes; bounded atomic questions, dimension validation, fail-closed service errors; live OpenRouter calls succeeded with the earlier rubric | The read-only sample was not a valid positive worker-launch fixture; representative calibration and successful live workflow remain open |
| Natural-language Constitution creation | Separate generative interviewer, exact human utterances, distinct operator notes, role-aware context, resumable sessions, exact input capture while the generative connection is deferred | Live generative connection is unconfigured after the user rejected the assumed OpenRouter/GPT onboarding route; finite context and unsupported restrictions remain explicit |
| Structured compiler | Capabilities, protected paths, launch rules, typed file/command checks, thresholds and limits; shared versioned runtime-control description; unsupported deployment actions rejected deterministically | Not an unrestricted natural-language-to-enforcement-code compiler |
| Semantic verification | JEV reviews original intent against each proposal; failed review prevents activation | Judgment remains probabilistic; the score is not a measured correctness guarantee |
| Versioning | Per-Constitution activation, suspension on edits, supersession, new approvals, export/import/composition and reverified rollback | Exports have checksums, not signatures or identity attestation |
| Generic agent launcher | Real Linux namespace process/PTY supervisor with cleanup and independent wall timeout | Linux only; installed binaries/dependencies must be reachable in the allowed tool installation |
| Filesystem enforcement | Private staged copies, readonly host installations, hidden home/ledger, protected-file exclusion, reviewed host promotion | Text-only bounded semantic promotion; no filesystem-wide crash-atomic publication |
| Shell enforcement | Explicit launch/check authorization; child processes inherit actual namespace/file/network restrictions | No individual descendant-command interception. A requirement such as a custom per-child-command allowlist is unsupported |
| Git enforcement | Scratch Git isolated; policy-bound staged patch/index review and complete bounded push-history path/content review; separate approval, regenerated plans, durable attempts and read-only uncertainty inspection/human abandonment | 16,000-byte text review and 256-commit push bound; binary/shallow history denied; no remote outcome attestation, automatic repair/staging or merge/conflict resolution |
| Credential isolation | Fixed model broker holds provider keys; worker receives placeholder credentials; Git token scoped to its operation | Host owner/admin is trusted; this is not hardware-backed secret storage |
| Event/evidence store | Hash-chained append-only application ledger; source snapshots, process results, changes, acceptance receipts, approvals and publications | No external anti-replacement checkpoint, archival service or full forensic replay |
| Action judgments | Launch, project read, each published change, verification and host Git plan judged with deterministic outer gates | Descendant commands and transient private-copy edits are not individually submitted to JEV before execution |
| Behavioral observations | Periodic independent file metadata snapshots, with JEV judging observed file scope | Paths/hashes/sizes do not establish file-content quality, subprocess conduct, repeated tool failures or attempted circumvention; broader observation is still needed |
| Completion judgments | Required typed file/command predicates; linked worker/check/JEV/publication records; current host manifest and authority rechecked | Command checks run in isolated copies; deployment/network receipts need separate gateways |
| Natural-language updates | New instructions suspend the previous version; new proposal must be approved | Updating the Constitution is done through JEVRUNTIME, not by typing a grant directly into the worker |
| Allow/deny/approval-required | Actual operation consumption, one-use approval binding, nonzero denied CLI status | The host process is the trusted approver; direct host-owner API calls are outside the worker boundary |
| Basic autonomy | Normal/cautious/restricted/quarantined states change operation permission; recovery binds human approval, fresh checks/JEV, unchanged artifacts and authority events to one atomic restoration | No learned behavior baseline, anomaly model, or fine-grained privilege restoration ladder |

## Defects addressed in the previous bootstrap

- A superseded write-enabled proposal cannot remain active in the same interview after
  read-only intent replaces it. A pending change also suspends old authority.
- A failed JEV proposal judgment can be retried without a duplicate human statement.
- Hard-link aliases, symlinks, credential variants and special files cannot pass the
  normal workspace/path gate. Staging and decisions share protected-path rules.
- Denied CLI decisions return a failure status; approval-required returns a distinct status.
- Individual operations have a concrete approval/consumption record rather than an
  approval-required label with no executable path.
- Completion for typed checks requires actual independent process receipts and the
  published manifest. A worker's text or self-created success file cannot substitute
  for a required command invocation.
- Worker errors, interrupts, timeouts, excessive output and provider errors do not
  activate a policy, publish changes, or claim completion.
- A host edit to an untouched file is detected before publication, avoiding the earlier
  case where some worker changes could be applied before whole-tree drift was noticed.
- Policy revocation interrupts workers and verification commands, and is checked again
  before publication and host Git execution.
- Approval review exposes exit-status/content predicates and runtime limits rather than
  hiding material conditions behind a requirement's friendly description.
- Onboarding cannot offer a new proposal without independent typed checks. Protected,
  excluded, aliased and directory file predicates fail deterministic pre-activation
  validation before JEV. Safe future artifact paths remain valid.
- Atomic judgments retain each required dimension and request/rubric provenance.
  Strong dimensions cannot compensate for a missing, malformed or failed dimension.

- Unsupported deployment actions fail before semantic review and cannot be approved,
  activated, reported as current or consumed from legacy records. Historical policies
  remain readable. `jev capabilities` exposes the same implementation boundaries used
  by the compiler and judgment context, without implying authorization or live readiness.

- Setup and the example configuration defer generative connections until explicit
  selection. Worker provider selection is exact and separate from selecting an
  interviewer. Native-JEV-only configurations load; local policy operations and saved
  interview review do not initialize a model or request a credential. Deferred replies
  preserve the exact input, suspend prior authority and await explicit connection/retry.
- JEV/generative credential references must differ. Hidden credential caching includes
  purpose, and a known JEV key value cannot be sent through the generative CLI paths.
  A rejected prompted key is not cached, allowing a corrected key on the next attempt.

- Legacy file-evidence-only completion has been removed. Every new policy needs
  typed checks covering every acceptance requirement; historical unchecked policies
  cannot regain authority through approval or stored activation records.
- Completion inspection verifies the latest linked execution, check receipts, JEV
  judgment, publication and unchanged host artifacts without invoking a provider.
  Incomplete output, mismatched provenance, later failed/unfinished attempts and
  authority changes fail closed. Historical publications can receive an explicitly
  labeled fresh attestation after independent verification.
- Publication failures record their phase and applied paths, including failures after
  host writes. Such failures quarantine the runtime for explicit reconciliation;
  this does not make multi-file publication crash-atomic.

- Recovery keeps ordinary capabilities restricted while it runs only approved checks
  in a read-only copy. The exact verification is consumed once, and a new failure,
  changed artifact, changed authority, incomplete output or failed JEV response blocks
  restoration. The ledger transaction rejects concurrent events before writing NORMAL.
  Recovery does not reconcile or certify an interrupted publication.

- Setup and onboarding use one explicitly selected member-owned folder; no implicit
  current-directory ledger or shared worker-output location is chosen. Saved sessions
  retain their project, and selected policies cannot silently override an explicit
  workspace argument. Actual publication destinations are checked again after review.
- History inspection opens existing SQLite records read-only, never inventing a new
  empty history. Explicit older layouts and recorded staged-run directories remain
  usable, while setup preserves old settings in a dated backup. These checks use
  filesystem ownership; they do not add a membership or application-login system.

- An unfinished publication is now an enforced restriction even if no error handler
  ran. Read-only reconciliation distinguishes baseline, matching target, divergent
  and unreadable projects. Human abandonment closes the exact attempts and retains
  quarantine for affected active Constitutions, without changing files or certifying
  completion. Unambiguous legacy publications still require fresh attestation.
- An additional failure cannot reduce an existing quarantine. A new restriction
  during publication review is checked before host writes. Recovery refuses unresolved
  attempts before running checks; an abandoned run cannot be reused for publication.

- Git operations now enforce the same protected-path rules against staged index entries,
  deleted/renamed paths and every reachable historical tree. Matching blob content under
  another filename cannot hide a protected history path. Observable hard links are denied.
  Protected worktree names are excluded before status subprocesses or content reads.
- Versioned Git plans bind the current policy, complete bounded content and exact objects.
  Legacy/malformed plans are rejected before JEV; human approval cannot bypass the gate.
  Complete text review rejects oversized, binary or shallow histories. This does not
  classify all secrets or prove a live remote push.
- Git approval/consumption/start are atomic. An unresolved attempt restricts affected
  Constitutions in the same ledger, even after abrupt process loss. Actual Git children
  inherit the common-directory lock, preventing abandonment while a child still runs.
  Read-only inspection works with revoked policies and unconfigured models, lists legacy
  attempt IDs, and distinguishes observations from proof of execution. Exact human
  abandonment retains quarantine and modifies no files/refs; fresh recovery remains
  required. Valid old receipts need matching plan checksum, request and human approval.

## Open work and pitfalls

1. Individual descendant-command gating remains an architectural gap for policies that
   need it. The current boundary constrains capabilities and host publication, and must
   not be described as a syscall-by-syscall semantic gate.
2. Binary/large artifacts and large JEV contexts currently stop with an error. They need
   typed artifact validators and a deliberate context strategy, not silent truncation.
3. Existing project virtual environments and dependency caches are excluded. A controlled
   dependency/package gateway and reproducible worker images would improve real projects.
4. Aggregate memory/disk and monetary budgets are not enforced by cgroups/account billing.
   Current limits bound time, output, files, process resources and inference requests.
5. Interrupted file publication now has read-only inspection and explicit human
   abandonment. Unfinished attempts and failures after host writes block ordinary
   authority, and abandonment preserves files and quarantine. Automatic rollback,
   certification of interrupted results, and remote Git outcome attestation remain
   unimplemented; multi-file publication is not crash-atomic. Git has local inspection
   and explicit uncertain-attempt abandonment, but no remote cancellation or automatic
   retry. Its common-directory lock coordinates Git operations only; independently
   configured file publishers and separate ledgers do not share all these controls.
6. Live human onboarding and a complete live-model worker flow remain unverified. The
   attempted generative route failed with HTTP 404; the user rejected reuse of the JEV
   credential for that model, and the temporary onboarding configuration was removed.
   A generative connection must be explicitly selected. Live
   OpenRouter JEV returned typed answers and rejected authority expansion and empty evidence,
   but the read-only sample explicitly forbade execution and was not a valid positive
   worker-launch fixture. Atomic question design and calibration need representative
   labeled examples; the existing 0.95 floor remains unchanged. Real Crush execution was exercised
   with a local simulated generative provider and explicit simulated JEV. Codex/Claude
   configuration has not received equivalent live exercise.
7. Deployment, arbitrary network operations, cross-machine/multiple-agent supervision,
   local-vs-cloud classification/routing, signed Constitutions, remote/mobile interfaces,
   forensic replay and automated distributed recovery remain later-phase PRD work.

See VERIFICATION.md for actual executed evidence and README.md for the supported flow.


## 2026-10-13 interview-launch verification

Interview `/run` now accepts `--no-provider` and `--interactive`, preserves the
worker's own arguments and recovers from malformed input without ending the interview.
A non-demo installed-CLI terminal walkthrough verified a real interactive local
worker, independent checks, explicit reviewed publication, restored terminal settings
and return to the saved interview. Compiler/JEV responses were synthetic (JEV passed
through the official SDK to a local fixture). Live natural-language compilation and
a live-JEV-governed worker task remain unverified; no generative provider was selected.
See the dated entry in VERIFICATION.md for failures, commands and receipt scope.
