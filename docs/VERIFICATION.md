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
Bounded field-specific correction feedback for a rejected compiler response remains
subsequent core work; it is not claimed implemented here.

This bound covers new accepted responses. It does not cap HTTP bytes before download,
repair older saved outputs, or remove the existing overall conversation-size limit.
