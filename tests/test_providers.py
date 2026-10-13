import json

import httpx
import httpx2
import pytest
from typesafe_sdk import RetryPolicy, TypeSafeClient

from jev_runtime.config import endpoint
from jev_runtime.contracts import Judgment, digest
from jev_runtime.judgment import RUBRIC_VERSION, TypeSafeJudge, judgment_client
from jev_runtime.onboarding import OnboardingModel, validate_result


@pytest.mark.parametrize(
    "status,answer,expected",
    [
        (200, 0.99, True),
        (200, None, False),
        (200, 1.5, False),
        (503, None, False),
    ],
)
def test_real_sdk_mock_transport_contract(status, answer, expected):
    seen = []

    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        assert request.url.path == "/v1/systemone"
        answers = (
            {name: {"type": "noul", "noul": answer} for name in body["questions"]}
            if answer is not None
            else {}
        )
        return httpx2.Response(
            status, json={"model": "contract-test", "usage": {}, "answers": answers}
        )

    with TypeSafeClient(
        api_key="test-only",
        transport=httpx2.MockTransport(handle),
        retry=RetryPolicy(max_retries=0),
    ) as client:
        result = TypeSafeJudge(client).judge("policy", {"human_intent": "Read only"})
    assert result.passes() is expected
    assert set(seen[0]["questions"]) == {
        "capability_coverage",
        "restriction_preservation",
        "completion_preservation",
        "restriction_enforceability",
    }
    assert all(question["type"] == "noul" for question in seen[0]["questions"].values())
    assert result.rubric_version == RUBRIC_VERSION
    assert result.request_hash == digest({key: seen[0][key] for key in ("state", "questions")})


def test_sdk_network_failure():
    def handle(request):
        raise httpx2.ConnectError("offline", request=request)

    with TypeSafeClient(
        api_key="test-only",
        transport=httpx2.MockTransport(handle),
        retry=RetryPolicy(max_retries=0),
    ) as client:
        assert not TypeSafeJudge(client).judge("action", {}).passes()


@pytest.mark.parametrize(
    "status,answer,expected",
    [
        (200, 0.99, True),
        (200, 0.01, False),
        (200, 1.5, False),
        (200, True, False),
        (503, 0.99, False),
        (302, 0.99, False),
    ],
)
def test_openrouter_native_decisions_uses_official_sdk(status, answer, expected):
    seen = []

    def handle(request):
        seen.append(request)
        assert str(request.url) == "https://openrouter.ai/api/alpha/decisions"
        assert request.headers["authorization"] == "Bearer test-only"
        body = json.loads(request.content)
        assert body["model"] == "typesafe/jev-1.13"
        assert set(body["questions"]) == {"task_alignment", "restriction_consistency"}
        assert all(question["type"] == "noul" for question in body["questions"].values())
        assert "messages" not in body
        return httpx2.Response(
            status,
            headers={"location": "https://elsewhere.invalid"},
            json={
                "id": "native-contract",
                "model": "typesafe/jev-1.13",
                "provider": "TypeSafe",
                "usage": {"input_tokens": 10, "output_tokens": 1, "cost": 0.0},
                "answers": {name: {"type": "noul", "noul": answer} for name in body["questions"]},
            },
        )

    with judgment_client(
        {
            "provider": "openrouter",
            "base_url": "https://openrouter.ai",
            "model": "typesafe/jev-1.13",
        },
        "test-only",
        httpx2.MockTransport(handle),
    ) as client:
        result = TypeSafeJudge(client).judge("action", {"action": "read"})
        assert result.passes() is expected
        if status == 200 and type(answer) is float and 0 <= answer <= 1:
            assert result.provider_metadata == {"input_tokens": 10, "output_tokens": 1}
    assert len(seen) == 1


@pytest.mark.parametrize("failure", ["disagreement", "missing", "wrong_type"])
def test_atomic_policy_dimensions_cannot_compensate_for_one_another(failure):
    def handle(request):
        body = json.loads(request.content)
        answers = {name: {"type": "noul", "noul": 0.99} for name in body["questions"]}
        if failure == "disagreement":
            answers["restriction_preservation"]["noul"] = 0.2
        elif failure == "missing":
            del answers["restriction_preservation"]
        else:
            answers["restriction_preservation"] = {
                "type": "choice",
                "choice": "yes",
                "probabilities": {"yes": 1.0},
                "confidence": 1.0,
            }
        return httpx2.Response(
            200,
            headers={"x-typesafe-request-id": "provider-correlation-id"},
            json={"model": "contract-test", "usage": {}, "answers": answers},
        )

    with TypeSafeClient(api_key="test-only", transport=httpx2.MockTransport(handle)) as client:
        result = TypeSafeJudge(client).judge("policy", {"human_intent": "Read only"})
    assert not result.passes()
    assert result.dimensions["capability_coverage"] == 0.99
    if failure == "disagreement":
        assert result.probability == 0.2
        assert result.error is None
        assert result.dimensions["restriction_preservation"] == 0.2
    else:
        assert result.probability is None
        assert result.error
    assert result.provider_metadata == {"request_id_hash": digest("provider-correlation-id")}


def test_completion_questions_bind_each_requirement_to_its_own_receipt():
    state = {
        "human_intent": "Create a report and pass the independent check.",
        "requirements": [
            {"requirement": "Report contains result", "kind": "file", "path": "report.md"},
            {"requirement": "Independent check succeeds", "kind": "command", "argv": ["check"]},
        ],
        "evidence": [
            {
                "requirement": "Independent check succeeds",
                "passed": True,
                "exit_code": 0,
                "output_sha256": "a" * 64,
            },
            {"requirement": "Report contains result", "passed": True, "content": "Result: 42"},
        ],
    }
    seen = []

    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        assert set(body["questions"]) == {"requirement_0", "requirement_1"}
        first = body["questions"]["requirement_0"]["instructions"]["question"]
        second = body["questions"]["requirement_1"]["instructions"]["question"]
        assert "`task.requirements[0]`" in first and "`task.evidence[1]`" in first
        assert "`task.requirements[1]`" in second and "`task.evidence[0]`" in second
        return httpx2.Response(
            200,
            json={
                "model": "contract-test",
                "usage": {},
                "answers": {
                    "requirement_0": {"type": "noul", "noul": 0.99},
                    "requirement_1": {"type": "noul", "noul": 0.2},
                },
            },
        )

    with TypeSafeClient(api_key="test-only", transport=httpx2.MockTransport(handle)) as client:
        result = TypeSafeJudge(client).judge("completion", state)
    assert len(seen) == 1
    assert result.dimensions == {"requirement_0": 0.99, "requirement_1": 0.2}
    assert not result.passes()


@pytest.mark.parametrize("problem", ["missing", "duplicate", "failed", "empty"])
def test_incomplete_completion_receipts_fail_before_api_call(problem):
    state = {
        "requirements": [{"requirement": "A", "kind": "file", "path": "a.md"}],
        "evidence": [{"requirement": "A", "passed": True, "content": "A"}],
    }
    if problem == "missing":
        state["evidence"] = []
    elif problem == "duplicate":
        state["requirements"].append({"requirement": "B", "kind": "file", "path": "b.md"})
        state["evidence"] *= 2
    elif problem == "failed":
        state["evidence"][0]["passed"] = False
    else:
        state = {"requirements": [], "evidence": []}
    calls = []

    def handle(request):
        calls.append(request)
        return httpx2.Response(500)

    with TypeSafeClient(api_key="test-only", transport=httpx2.MockTransport(handle)) as client:
        result = TypeSafeJudge(client).judge("completion", state)
    assert not result.passes()
    assert result.error == "ValueError"
    assert calls == []


def test_typed_file_completion_and_full_request_provenance():
    seen = []

    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx2.Response(
            200,
            json={
                "model": "contract-test",
                "usage": {},
                "answers": {name: {"type": "noul", "noul": 0.99} for name in body["questions"]},
            },
        )

    state = {
        "requirements": [{"requirement": "Report exists", "kind": "file", "path": "report.md"}],
        "evidence": [{"requirement": "Report exists", "passed": True, "content": "Report"}],
    }
    with TypeSafeClient(api_key="test-only", transport=httpx2.MockTransport(handle)) as client:
        judge = TypeSafeJudge(client)
        first = judge.judge("completion", state)
        state["evidence"][0]["content"] = "Changed report"
        second = judge.judge("completion", state)
    assert first.passes() and second.passes()
    assert first.request_hash != second.request_hash
    for result, body in zip((first, second), seen, strict=True):
        assert result.request_hash == digest({key: body[key] for key in ("state", "questions")})
        assert "enforced_runtime_invariants" in body["state"]
        assert "built_in_protected_paths" in body["state"]


def test_aggregate_cannot_hide_invalid_or_failed_dimension():
    assert not Judgment(0.99, "custom", dimensions={"first": 0.99, "second": 0.2}).passes()
    assert not Judgment(0.99, "custom", dimensions={"first": True}).passes()
    assert not Judgment(0.99, "custom", rubric_version=RUBRIC_VERSION).passes()


def test_generative_onboarding_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_LLM_KEY", "test-only")
    policy = dict(
        intent_id="human-1",
        workspace=str(tmp_path),
        actions=["read"],
        required_evidence=["Report exists"],
        checks=[{"requirement": "Report exists", "kind": "file", "path": "report.md"}],
    )

    def handle(request):
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        assert body["messages"][0]["role"] == "system"
        metadata = json.loads(
            body["messages"][0]["content"].split("Runtime-owned metadata: ", 1)[1]
        )
        controls = metadata["runtime_controls"]
        assert controls["grants_authority"] is False
        assert "deploy" not in controls["supported_host_actions"]
        advertised = body["messages"][0]["content"].split("actions (list drawn from ", 1)[1]
        advertised = advertised.split(")", 1)[0].split(", ")
        assert set(advertised) == {"read", "write", "delete", "execute", "git", "push"}
        assert "per_descendant_command_allowlist" in controls["unsupported_controls"]
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"question": "Review this proposal", "policy": policy}
                            )
                        }
                    }
                ]
            },
        )

    model = OnboardingModel(
        dict(base_url="http://localhost:1234/v1", model="test-model", api_key_env="TEST_LLM_KEY"),
        httpx.MockTransport(handle),
    )
    assert model.turn("Read only", "human-1", str(tmp_path))["policy"] == policy
    policy["intent_id"] = "forged"
    with pytest.raises(ValueError):
        model.turn("Read only", "human-1", str(tmp_path))


@pytest.mark.parametrize(
    "url", ["http://remote.invalid", "https://user:secret@host", "https://host?api_key=secret"]
)
def test_endpoint_rejects_embedded_credentials(url):
    with pytest.raises(ValueError):
        endpoint(url)


def test_new_onboarding_cannot_omit_independent_checks(tmp_path):
    policy = {
        "intent_id": "human",
        "workspace": str(tmp_path),
        "actions": ["read", "execute"],
        "required_evidence": ["Independent command succeeds"],
    }
    for checks in (None, []):
        candidate = dict(policy)
        if checks is not None:
            candidate["checks"] = checks
        with pytest.raises(ValueError, match="deterministic check"):
            validate_result(
                {"question": "Ready to run", "policy": candidate}, "human", str(tmp_path)
            )


@pytest.mark.parametrize(
    "body", [{}, {"choices": []}, {"choices": [{"message": {"content": "not JSON"}}]}]
)
def test_malformed_provider_payload_yields_fixed_contract_diagnostic(tmp_path, body):
    from jev_runtime.onboarding import OnboardingContractError

    model = OnboardingModel(
        {"base_url": "http://localhost:1234/v1", "model": "fixture", "auth_required": False},
        httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
        api_key="",
    )
    with pytest.raises(OnboardingContractError) as failure:
        model.turn("Keep my request intact", "source", str(tmp_path))
    assert failure.value.diagnostic["code"] == "response_json"
    assert failure.value.diagnostic["authoritative"] is False
    assert "not JSON" not in str(failure.value)
