# Research and attribution

Reviewed 2026-09-22 against upstream source, tests, and MIT license files.
The collection is maintained in this repository's `docs/` directory;
temporary source clones used during inspection are not a second product repository.
The original [PRD Summary and Next Steps discussion](https://chatgpt.com/share/6ab2bdac-07c0-83ea-a6f5-ef7f8876a78c)
identified the initial sources. The human PRD remains unchanged in `PRD.md`.
No upstream implementation code was copied or vendored. The official SDK is a
dependency; its installed distribution retains its own license. Future copying
must retain the applicable copyright and full MIT permission notice.

| Source | Inspected revision | Use in this bootstrap |
| --- | --- | --- |
| [TypeSafe-AI/typesafe-sdk-python](https://github.com/TypeSafe-AI/typesafe-sdk-python) | `0ffd094c72ed9445223060b24ffd7a56aa781fb4` | Official `typesafe-sdk==0.7.1`; reviewed synchronous client, response types, README, tests/helpers, and LICENSE. Calls `TypeSafeClient.system_one(state=..., questions={...: Noul(instructions=...)})`; reads `response.nouls[name].noul` and `response.model`. Mock HTTP transport exercises the real SDK parser. |
| [kenhuangus/jev-usecases](https://github.com/kenhuangus/jev-usecases) | `d4b289586b6b21e07bf17f195acf06de920bb2a6` | Reviewed coding_agent_guardrails and decision safety tests. Adopted the separation of judgment, deterministic decision, and caller execution. Rejected substring matching as a filesystem containment mechanism. MIT: copyright 2026 Ken Huang. |
| [BYK/jev-mcp](https://github.com/BYK/jev-mcp) | `cee2e6d58112d006ccd3ebad1163a31083262ae9` | Reviewed metrics.ts and evaluation structure. Threshold calibration needs labeled task data, error-cost analysis, and held-out evaluation. This bootstrap's fixed 0.95 Noul floor is provisional, not a measured accuracy guarantee. MIT: copyright 2026 Burak Yigit Kaya. |
| [burnigtm/jev-mcp](https://github.com/burnigtm/jev-mcp) | `9448f6120015f2f6c6dad7137c7f234545209918` | Reviewed policy.ts and repository/test layout. Adopted the principle that incomplete context cannot auto-pass and semantic scores cannot soften deterministic restrictions. No MCP code or numerical scoring formulas reused. MIT: copyright 2026 jev-mcp contributors. |

Noul is a probability that the supplied statement is true, not a separate
confidence statistic. Choice and Score exist in the SDK but are unnecessary for
the narrow runtime questions. Correctly typed output does not establish
correct judgment. JEV does not generate onboarding text, execute commands, collect
evidence, or authorize capabilities.

## Runtime and provider integration

- [OpenRouter's official Python SDK](https://github.com/OpenRouterTeam/python-sdk),
  revision `49bb158e9b1d33363e7fb18c01f824f114dff7ef`: inspected
  `src/openrouter/decisions.py`, `operations/createapialphadecisions.py`, and the
  Decisions request/response/Noul schemas. The native route is
  `POST https://openrouter.ai/api/alpha/decisions`, with `model`, `state`, and
  typed `questions`; answers use `answers[name].noul`. The installed official
  TypeSafe SDK shares this wire contract. An original transport adapter changes
  its fixed endpoint while retaining its request serialization and strict response
  parsing. No OpenRouter source was copied; no chat prompt impersonates JEV.
- [OpenRouter JEV model](https://openrouter.ai/typesafe/jev-1.13) and
  [native Decisions example](https://openrouter.ai/labs/jev/compile): reviewed the
  model identifier and typed decision usage. Live calls resolved the requested
  `typesafe/jev-1.13` to `typesafe/jev-1.13-20260917`. This is an alpha API; pinning
  and contract tests do not guarantee future endpoint availability.
- [bubblewrap](https://github.com/containers/bubblewrap): used the installed
  bubblewrap 0.11.1 CLI for Linux mount, user, PID and network namespaces. Real
  host probes exercised isolation, readonly mounts, cleanup and timeout behavior.
- [Crush](https://github.com/charmbracelet/crush): used the installed, unmodified
  Crush 0.89.0 with temporary custom-provider configuration. Its actual write tool
  was exercised through the broker with a local simulated model response.
- Codex and Claude provider configuration was implemented from their published
  configuration contracts. Those adapters have not received the same live harness
  verification as Crush; they are not represented as proven integrations.

## Setup audit and corrections

Fresh default-branch inspections on 2026-09-22 matched all five revisions above.
The official [TypeSafe skills repository](https://github.com/typesafe-ai/skills/tree/65a39f393687675ce170e6094757de20370365b9),
release 0.5.7 at `65a39f393687675ce170e6094757de20370365b9`, adds API/architecture
guidance and agent plugin descriptors. It is not an enforcement hook. MIT, copyright
2026 TypeSafe AI. No installation is required for this Python runtime.

The [official building guide](https://docs.typesafe.ai/concepts/how-to-build-with-system-one)
and [Noul guide](https://docs.typesafe.ai/primitives/noul) recommend independently
useful questions composed by host code. The runtime now batches four policy dimensions
(capability coverage, restriction preservation, completion preservation and restriction
enforceability), two action dimensions, and one question per completion requirement.
Every required dimension must meet the existing floor. The scalar compatibility value
is the minimum dimension, not a probability of the conjunction or measured accuracy.
Missing/wrong-type answers and missing or duplicate requirement receipts fail closed.

Judgment records now retain rubric version `jev-runtime-atomic-1`, individual scores,
and a digest of the complete supplied state and serialized questions, including actual
protected-path rules and fixed runtime invariants. Available numeric SDK usage and a
hashed provider request ID are retained. Credentials and raw response bodies are not.
Behavior questions only assess observed file scope: path/hash/size observations cannot
establish file-content quality, subprocess conduct, or absence of attempted circumvention.
Broader behavioral observation remains application work.

New onboarding proposals require typed independent checks. File-check paths must pass
the same exclusions and alias checks used by staging/evidence before JEV is called;
protected paths, dependency caches, directories, symlinks and hard links are rejected.
A safe nonexistent future artifact is valid. Existing legacy policy parsing remains
compatible and is not relabeled as independent command verification.

BYK and burnigtm currently expect a direct TypeSafe key and append `/v1/systemone`.
Changing their base URL to OpenRouter is insufficient. They are optional advisory
interfaces, not runtime isolation or approval controls. Numerical example thresholds
are not imported. A snippet that runs an action solely because `Noul >= 0.90` would
violate this PRD unless the host has separately established permission and any required
human approval. The runtime's 0.95 floor is unchanged.

The earlier live read-only sample forbade execution and was not a positive fixture
for worker launch. Its rejection does not demonstrate a threshold defect. Results
from that compound rubric are not validation of this atomic rubric. Representative
calibration and a complete live application self-build remain outstanding.

## Additional repositories from the collected discussion

These exact repositories were publicly accessible on 2026-09-22. Inspected their
current `main`, documentation, selected implementation files and license notices.
They are references, not installed runtime dependencies; no source code was copied.

| Source | Inspected revision | Useful pattern and limits |
| --- | --- | --- |
| [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) | `1231850a0bf1a0c0341fe408ef1668dbbfdfac46` | Indexed action candidates, freshness checks, one-use decision consumption and independent post-run verification. Browser execution is separate from terminal isolation. MIT, copyright 2026 Browser Use. |
| [awlevin/typesafe-computer-use](https://github.com/awlevin/typesafe-computer-use) | `cc7b5066ae1a07b5e3182e8f87a9b5b6dfdcffc1` | macOS OCR/Accessibility observations, typed actions, separate generative writing and deterministic execution. Natural-language goal input is relevant; it does not implement Constitutions. MIT, copyright 2026 Aaron Levin. |
| [droidrun/mobile-jev](https://github.com/droidrun/mobile-jev) | `395fc222beac4f059f9a0beb337d114a2b066e99` | Hosted Android/model APIs, observed target binding, stale-target rejection and no blind retry of uncertain mutations. Optional confidence cutoff defaults to zero; do not import that as authority policy. MIT, copyright 2026 Jev Mobile Agent contributors. |
| [moritzkremb/jev-voice-browser](https://github.com/moritzkremb/jev-voice-browser) | `198a0764395a666f8398026c0d8abdaf6d1866c5` | Speech-to-bounded-candidates and visible decision reasons. Heuristic confirmation and fallback answers are not suitable replacements for fail-closed permission checks. MIT, copyright 2026 Moritz Kremb. |
| [jkudish/jev-mcp](https://github.com/jkudish/jev-mcp) | `7162ac4440522b6c19cf1a4f336e0ef7f0f24d8f` | Ten advisory tools, bounded response validation/deadlines, direct TypeSafe and native OpenRouter provider routes. MIT, copyright 2026 Joey Kudish. Review/completion design credits burnigtm/jev-mcp through PR #2; retain that attribution chain if adapting it. |

The supplied performance descriptions need qualification. Browser Use reports
178 ms median JEV latency in its [narrow benchmark](https://github.com/browser-use/jev-ultrafast/blob/1231850a0bf1a0c0341fe408ef1668dbbfdfac46/docs/performance.md),
not a general 50 ms workflow guarantee; its matched comparison is three pairs of
one task, and independent verification is outside the main timer. Aaron Levin's
[README](https://github.com/awlevin/typesafe-computer-use/blob/cc7b5066ae1a07b5e3182e8f87a9b5b6dfdcffc1/README.md)
reports model latency of 0.13–0.38 seconds and approximately 1.5 seconds per full step.
The mobile [demo evidence](https://github.com/droidrun/mobile-jev/blob/395fc222beac4f059f9a0beb337d114a2b066e99/docs/DEMO.md)
does not show a completed Uber booking and documents failed experiments. Voice
Browser's [demo guide](https://github.com/moritzkremb/jev-voice-browser/blob/198a0764395a666f8398026c0d8abdaf6d1866c5/DEMO.md)
distinguishes a cold first call from warmed model timings. The inspected primary
documentation supplies no battery/overheating measurements. Typed bounded output
does not prove repeatable model choices or correct intent interpretation.

The [jkudish provider source](https://github.com/jkudish/jev-mcp/blob/7162ac4440522b6c19cf1a4f336e0ef7f0f24d8f/src/provider.ts)
independently confirms the native OpenRouter Decisions route. The runtime keeps its
official Python SDK boundary; installing all these MCP servers would not supply the
missing supervision capabilities. The directly applicable optimization is the
official [question fan-out pattern](https://docs.typesafe.ai/patterns/fan-out): ask
independent questions in a batch and compose only the relevant answers in code.
