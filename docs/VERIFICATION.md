# Verification status

The local suite returned 262 passed in 12.66 seconds on 2026-10-13, with no skips.
Ruff lint and formatting checks passed, and the canonical PRD remained unchanged.

An actual installed-CLI terminal walkthrough exercised an isolated interactive
Python worker, independent command checks, explicit approval, reviewed publication,
return to the same interview, terminal restoration and launch-option isolation.
The compiler and JEV responses in this walkthrough were synthetic. The JEV fixture
used the official TypeSafe SDK against a loopback responder. This proves local
runtime behavior, not a complete workflow with live models.

Live natural-language onboarding and a successful live-JEV-governed worker task
remain unverified. A few earlier native OpenRouter connectivity and negative
judgment probes do not establish model calibration or full completion.

See PRD_STATUS.md for the implemented controls and remaining limitations.
Internal server records and operational transcripts are kept outside this public
repository. Tests use disposable projects and deterministic doubles; no live API
keys are required.

Run local checks with:
```sh
python -m pytest -q
ruff check src tests
ruff format --check src tests
```


## Core onboarding corrections — 2026-10-13

The compiler prompt now advertises only implemented actions. It previously listed
deployment as an option despite rejecting deployment later in the same prompt.
The complete generated response is now limited to 16000 UTF-8 bytes before an
accepted output is saved. This prevents a single oversized generated policy from
exhausting the next-turn context. A rejected output leaves the exact human input
pending for explicit retry, with no activation or automatic generation retry.

The new provider-contract assertion failed on the unsupported deployment option
before correction. Focused provider/connection checks then passed 49 cases in 0.20s;
the full suite passed 263 cases in 12.32s, with no skips. Lint, formatting and diff
checks passed, and the canonical PRD remained unchanged. These checks used doubles
or mocked HTTP for generation. Live generative-model selection is still pending.
The subsequent explicit compiler-feedback increment is recorded below.

This bound covers new accepted responses. It does not cap HTTP bytes before download,
repair older saved outputs, or remove the existing overall conversation-size limit.


## Explicit compiler correction on retry — 2026-10-13

Compiler failures now produce fixed, bounded host diagnostics for malformed JSON,
response shape/size, policy schema/checks, changed source provenance, unsupported
actions and the known deterministic unusable-acceptance failure. The terminal shows
the safe diagnostic. It does not show rejected provider content.

Only an explicit retry supplies a validated diagnostic from the latest failure for
the exact interview and pending input. Changed human input, operator context, an
accepted reply or a newer non-contract failure cannot reuse stale correction text.
Unknown or modified stored diagnostics are not replayed. The runtime reconstructs
correction messages from fixed constants; they do not grant authority or become
immutable human-source context. The attempt records the referenced failure and a
hash of its effective model input. There is no automatic generation retry, output
coercion or activation. Missing-workspace, storage and JEV semantic failures are
not classified as compiler-schema corrections.

The full suite returned 279 passed in 12.38 seconds, with no skips. Lint, formatting
and diff checks passed; the PRD is unchanged. These new cases exercise repair,
source/approval separation, malformed provider payloads, protected acceptance
paths, stale failures and forged diagnostic rejection with offline doubles or
mocked HTTP. No live generative or JEV call was made. Explicit live-model selection
and the complete real interview-to-worker workflow remain pending.


### Host failures do not rewrite acceptance checks

Source review found that the generic deterministic-validation error could also
represent a host permissions or enforcement problem. The mapping now distinguishes
known unusable policy acceptance predicates from host I/O and failed protected-path
simulations. Only the former produces compiler correction feedback. Host failures
remain non-contract failures and block proposal creation/JEV review; the existing
policy-validation evidence records the distinction. The compiler must not rewrite
valid human requirements to work around a broken host.

Controlled permission-error and enforcement-simulation cases verified that no
schema feedback is sent, human input stays pending and unchanged, and explicit
retry after host restoration creates no approval. The final full suite passed
281 cases in 12.38 seconds, with no skips. Lint, formatting and diff checks passed;
the canonical PRD is unchanged. An independent source-only critic found no remaining
blocking classification or provenance defect in this increment. No live model calls
were made, and the pending explicit generative-model selection is unchanged.
