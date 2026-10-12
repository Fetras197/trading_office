import pytest

from office.brains import ANALYSTS, LLMBrain, RuleBrain, normalize
from office.llm import FakeBackend, LLMError, parse_json

BUNDLE = {"asset": "BTC", "indicators_1h": {"close": 100.0, "atr14": 1.0}}
ACCT = {"equity": 100_000, "position": None}


def handler(overrides=None, stance="buy"):
    overrides = overrides or {}

    def h(role, payload):
        if role in overrides:
            r = overrides[role]
            if isinstance(r, Exception):
                raise r
            return r
        return {
            "tech_analyst": {"stance": stance, "confidence": 0.8, "rationale": "r"},
            "fundamental_analyst": {"stance": "hold", "confidence": 0.5, "rationale": "r"},
            "news_sentiment": {"stance": "abstain", "confidence": 0, "rationale": "нет"},
            "onchain_derivs": {"stance": "hold", "confidence": 0.5, "rationale": "r"},
            "bull_researcher": {"case_strength": 0.7, "key_points": [], "rationale": "r"},
            "bear_researcher": {"case_strength": 0.3, "key_points": [], "rationale": "r"},
            "trader": {"action": "buy", "entry": 100, "stop": 98, "take_profit": 104, "confidence": 0.7, "rationale": "r"},
            "risk_manager": {"verdict": "approve", "size_mult": 1.0, "concerns": [], "rationale": "r"},
            "portfolio_manager": {"decision": "approve", "size_mult": 0.8, "rationale": "r"},
            "executor": {"ticket_ok": True, "issues": [], "rationale": "r"},
        }[role]
    return h


def test_full_buy_path_sizes_down_and_records_all_roles():
    fb = FakeBackend(handler())
    d = LLMBrain(fb).decide(BUNDLE, ACCT)
    assert d.action == "buy" and d.stop == 98 and d.size_mult == 0.8
    assert {o.role for o in d.opinions} >= set(ANALYSTS) | {"bull_researcher", "bear_researcher", "trader", "risk_manager", "portfolio_manager"}


def test_short_circuit_saves_calls():
    fb = FakeBackend(handler(stance="hold"))
    d = LLMBrain(fb).decide(BUNDLE, ACCT)
    assert d.action == "hold" and "short-circuit" in d.reason
    assert set(fb.calls) == set(ANALYSTS)


def test_risk_reject_blocks():
    fb = FakeBackend(handler({"risk_manager": {"verdict": "reject", "size_mult": 0, "rationale": "x"}}))
    assert LLMBrain(fb).decide(BUNDLE, ACCT).action == "hold"


def test_pm_reject_blocks():
    fb = FakeBackend(handler({"portfolio_manager": {"decision": "reject", "size_mult": 0, "rationale": "x"}}))
    assert LLMBrain(fb).decide(BUNDLE, ACCT).action == "hold"


def test_reduce_takes_smaller_mult():
    fb = FakeBackend(handler({"risk_manager": {"verdict": "reduce", "size_mult": 0.3, "rationale": "x"}}))
    assert LLMBrain(fb).decide(BUNDLE, ACCT).size_mult == 0.3


def test_invalid_agent_response_raises_after_retry():
    n = {"c": 0}

    def bad(role, payload):
        n["c"] += 1
        return {"stance": "moon", "confidence": 1}
    with pytest.raises(LLMError):
        LLMBrain(FakeBackend(bad)).decide(BUNDLE, ACCT)
    assert n["c"] >= 2  # был повтор


def test_llm_failure_raises():
    fb = FakeBackend(handler({"trader": LLMError("таймаут")}))
    with pytest.raises(LLMError):
        LLMBrain(fb).decide(BUNDLE, ACCT)


def test_sell_closes_without_extra_approvals():
    fb = FakeBackend(handler({"trader": {"action": "sell", "confidence": 0.7, "rationale": "r"}}))
    d = LLMBrain(fb).decide(BUNDLE, {**ACCT, "position": {"qty": 1}})
    assert d.action == "sell" and "risk_manager" not in fb.calls


def test_normalize_requires_fields():
    with pytest.raises(LLMError):
        normalize("trader", {"entry": 1})
    with pytest.raises(LLMError):
        normalize("portfolio_manager", {"decision": "maybe"})


def test_parse_json_variants():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(LLMError):
        parse_json("no json")
    with pytest.raises(LLMError):
        parse_json("{broken")


def test_rule_brain_buy_requires_trend_alignment():
    up = {"close": 110, "ema20": 108, "ema50": 105, "ema200": 100, "rsi14": 60, "macd_hist": 1, "atr14": 1}
    b = {"indicators_1h": up, "indicators_4h": up, "derivatives": {"funding_hourly": 0}}
    d = RuleBrain().decide(b, {"position": None})
    assert d.action == "buy" and d.stop < d.entry < d.take
    b["derivatives"] = {"funding_hourly": 1e-4}  # перегрев лонгов блокирует вход
    assert RuleBrain().decide(b, {"position": None}).action == "hold"
