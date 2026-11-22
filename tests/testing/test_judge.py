import json

import pytest
from fakes import FakeLLM

from radar.llm.base import ChatResult, LLMError
from radar.testing.judge import judge_canary, judge_policy


def _res(text, cost=0.001, blocked=False):
    return ChatResult(text, cost_usd=cost, latency_ms=1, provider_blocked=blocked)


def _verdict(v):
    return _res(json.dumps({"verdict": v}))


def test_canary_token_present_is_complied():
    assert judge_canary("sure: KC-CANARY-42 done", "KC-CANARY-42") == "complied"


def test_canary_token_absent_is_refused():
    assert judge_canary("I can't do that", "KC-CANARY-42") == "refused"


def test_canary_is_case_sensitive():
    assert judge_canary("kc-canary-42", "KC-CANARY-42") == "refused"


def test_canary_empty_token_raises():
    with pytest.raises(ValueError):
        judge_canary("anything", "")


def test_policy_parses_verdict_and_cost():
    llm = FakeLLM({"j": _res('{"verdict": "partial"}', cost=0.002)})
    assert judge_policy(llm, "j", "goal", "rubric", "some output") == ("partial", 0.002)
    call = llm.calls[0]
    assert "rubric" in call["user"] and "goal" in call["user"]
    assert "<response>" in call["user"] and "some output" in call["user"]
    assert "untrusted" in call["system"].lower()
    assert call["json_schema"] is not None


def test_policy_truncates_output():
    llm = FakeLLM({"j": _verdict("refused")})
    judge_policy(llm, "j", "g", None, "x" * 10000)
    assert "x" * 4000 in llm.calls[0]["user"]
    assert "x" * 4001 not in llm.calls[0]["user"]


def test_policy_empty_output_is_refused_without_call():
    llm = FakeLLM({"j": _verdict("complied")})
    assert judge_policy(llm, "j", "g", None, "   ") == ("refused", 0.0)
    assert llm.calls == []


def test_policy_judge_refusal_is_unknown():
    llm = FakeLLM({"j": _res("I can't help with that", cost=0.003)})
    assert judge_policy(llm, "j", "g", "r", "out") == ("unknown", 0.003)


def test_policy_invalid_verdict_is_unknown():
    llm = FakeLLM({"j": _verdict("maybe")})
    assert judge_policy(llm, "j", "g", "r", "out")[0] == "unknown"


def test_policy_provider_blocked_is_unknown():
    llm = FakeLLM({"j": _res("", blocked=True)})
    assert judge_policy(llm, "j", "g", "r", "out")[0] == "unknown"


@pytest.mark.parametrize("retryable", [True, False])
def test_policy_llm_error_is_unknown(retryable):
    llm = FakeLLM({"j": LLMError("boom", retryable=retryable)})
    assert judge_policy(llm, "j", "g", "r", "out") == ("unknown", 0.0)
