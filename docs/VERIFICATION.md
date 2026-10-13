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
