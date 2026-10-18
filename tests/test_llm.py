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


def test_router_all_roles_gemini_and_cached():
    class B:
        n = 0

        def ask(self, role, p, system=None):
            B.n += 1
            return {"role": role}
    r = RoleRouter(gemini=B())
    assert [r.ask(x, {})["role"] for x in ("market", "picture", "postmortem", "weekly")] == ["market", "picture", "postmortem", "weekly"]
    r.ask("market", {})
    assert B.n == 4                                   # повтор — из кэша


def test_no_claude_anywhere():
    assert not hasattr(llm, "ClaudeBackend") and "subprocess" not in vars(llm)


def test_gemini_429_all_models_is_unavailable_not_crash(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    from office.llm import LLMUnavailable

    class S:
        def post(self, *a, **k):
            return type("R", (), {"status_code": 429, "text": "RESOURCE_EXHAUSTED PerDay", "json": lambda s: {}})()
    with pytest.raises(LLMUnavailable):
        GeminiBackend(session=S(), min_interval=0).ask("market", {}, system="x")


class _B:
    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def ask(self, role, p, system=None, model=""):
        self.calls.append((role, system, model))
        return self.fn()


def _unavail():
    raise llm.LLMUnavailable("429")


def test_router_limit_propagates_as_unavailable_for_every_role():
    r = RoleRouter(gemini=_B(_unavail))
    for role in ("market", "newsdesk", "picture"):
        with pytest.raises(llm.LLMUnavailable):
            r.ask(role, {}, system="x")


def test_gemini_pause_after_429_stops_requests_and_budget_shares(monkeypatch):
    class S:
        n = 0

        def post(self, *a, **k):
            S.n += 1
            return type("R", (), {"status_code": 429, "text": "PerDay", "json": lambda s: {}})()
    g = GeminiBackend(session=S(), min_interval=0)
    with pytest.raises(llm.LLMUnavailable):
        g.ask("market", {}, system="x")
    n = S.n
    assert llm.status()["gemini_down"]
    with pytest.raises(llm.LLMUnavailable, match="пауз"):
        g.ask("market", {}, system="x")
    assert S.n == n                               # на паузе запросы не уходят
    llm.reset_state()
    monkeypatch.setattr(llm, "DAILY_BUDGET", 10)
    for _ in range(3):
        llm.budget_spend("newsdesk")
    assert not llm.budget_allow("newsdesk") and llm.budget_allow("market")   # фону доля 25% (2 из 10), залу можно
    for _ in range(7):
        llm.budget_spend("market")
    assert not llm.budget_allow("market")
