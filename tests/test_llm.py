import pytest

from office import llm
from office.llm import GeminiBackend, LLMError, RoleRouter


class Resp:
    def __init__(self, code, js=None, text=""):
        self.status_code, self._js, self.text = code, js, text or str(js)

    def json(self):
        return self._js


def ok(txt='{"stance":"hold","confidence":0.5,"rationale":"r"}'):
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": txt}]}}]})


class Sess:
    def __init__(self, *resps):
        self.resps, self.n = list(resps), 0

    def post(self, *a, **k):
        self.n += 1
        return self.resps.pop(0)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setenv("GEMINI_API_KEY", "k")


def test_gemini_ok():
    g = GeminiBackend(session=Sess(ok()), min_interval=0)
    assert g.ask("tech_analyst", {"a": 1})["stance"] == "hold"


def test_gemini_retries_429_then_succeeds():
    s = Sess(Resp(429), Resp(503), ok())
    assert GeminiBackend(session=s, min_interval=0).ask("tech_analyst", {})["stance"] == "hold" and s.n == 3


def test_gemini_gives_up():
    with pytest.raises(LLMError, match="429"):
        GeminiBackend(session=Sess(*[Resp(429)] * 9), min_interval=0).ask("tech_analyst", {})


def test_gemini_daily_quota_falls_back_to_next_model():
    s = Sess(Resp(429, text="GenerateRequestsPerDayPerProjectPerModel-FreeTier"), ok())
    assert GeminiBackend(session=s, min_interval=0).ask("tech_analyst", {})["stance"] == "hold" and s.n == 2


def test_gemini_garbage_and_missing_key(monkeypatch):
    with pytest.raises(LLMError):
        GeminiBackend(session=Sess(Resp(200, {"candidates": []})), min_interval=0).ask("tech_analyst", {})
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiBackend(session=Sess(), min_interval=0).ask("tech_analyst", {})


def test_router_analysts_gemini_rest_claude():
    class B:
        def __init__(self, n):
            self.n = n

        def ask(self, role, p, system=None):
            return self.n
    r = RoleRouter(claude=B("claude"), gemini=B("gemini"))
    assert [r.ask(x, {}) for x in ("market", "news", "fundamentals", "quant", "buffett", "munger", "druckenmiller")] == ["gemini"] * 7
    assert [r.ask(x, {}) for x in ("trader", "risk_manager", "portfolio_manager", "executor")] == ["claude"] * 4


def test_gemini_429_all_models_is_unavailable_not_crash(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    from office.llm import LLMUnavailable

    class S:
        def post(self, *a, **k):
            return type("R", (), {"status_code": 429, "text": "RESOURCE_EXHAUSTED PerDay", "json": lambda s: {}})()
    with pytest.raises(LLMUnavailable):
        GeminiBackend(session=S(), min_interval=0).ask("market", {}, system="x")
