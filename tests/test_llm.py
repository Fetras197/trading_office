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


# ---- запасные бесплатные модели ----
class FbResp:
    def __init__(self, code, content=None, text="", headers=None):
        self.status_code, self.text, self.headers = code, text or (content or ""), headers or {}
        self._j = {"choices": [{"message": {"content": content}}]}

    def json(self):
        return self._j


class FbSess:
    def __init__(self, *resps):
        self.resps, self.calls = list(resps), []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append((url, dict(json)))     # копия: провайдер потом убирает response_format из того же словаря
        return self.resps.pop(0) if self.resps else FbResp(500, text="no more")


GOOD = '{"stance": "hold", "confidence": 0.6, "rationale": "ok"}'


def _gem_down():
    class G:
        def ask(self, role, p, system=None):
            raise llm.LLMUnavailable("gemini 429")
    return G()


def _chain(monkeypatch, sessions, keys=("groq", "cerebras", "mistral", "openrouter")):
    for n, (_, env, *_r) in llm.OpenAICompatBackend.PROVIDERS.items():
        monkeypatch.delenv(env, raising=False)
    for n in keys:
        monkeypatch.setenv(llm.OpenAICompatBackend.PROVIDERS[n][1], "k-" + n)
    llm.OpenAICompatBackend._pause.clear()
    return [llm.OpenAICompatBackend(n, session=sessions[n], min_interval=0) for n in ("groq", "cerebras", "mistral", "openrouter")]


def test_fallback_switches_to_next_provider_on_429_and_pauses_the_limited_one(monkeypatch):
    s = {"groq": FbSess(FbResp(429, text="rate", headers={"retry-after": "30"})), "cerebras": FbSess(FbResp(200, GOOD)), "mistral": FbSess(), "openrouter": FbSess()}
    r = RoleRouter(gemini=_gem_down(), fallbacks=_chain(monkeypatch, s))
    assert r.ask("market", {"a": 1}, system="x")["stance"] == "hold"
    assert len(s["groq"].calls) == 1 and len(s["cerebras"].calls) == 1 and not s["mistral"].calls
    assert llm.OpenAICompatBackend._pause["groq"] > __import__("time").time() + 20                 # Retry-After учтён: Groq на паузе
    r2 = RoleRouter(gemini=_gem_down(), fallbacks=_chain_keep(s))
    s["cerebras"].resps = [FbResp(200, GOOD)]
    r2.ask("news", {"b": 2}, system="x")
    assert len(s["groq"].calls) == 1                                                              # пока пауза, Groq не дёргаем


def _chain_keep(sessions):
    return [llm.OpenAICompatBackend(n, session=sessions[n], min_interval=0) for n in ("groq", "cerebras", "mistral", "openrouter")]


def test_all_models_down_raises_plain_unavailable_text(monkeypatch):
    s = {n: FbSess(FbResp(429, text="rate")) for n in ("groq", "cerebras", "mistral", "openrouter")}
    r = RoleRouter(gemini=_gem_down(), fallbacks=_chain(monkeypatch, s))
    with pytest.raises(llm.LLMUnavailable, match="аналитики недоступны: лимит моделей"):
        r.ask("market", {}, system="x")


def test_providers_without_keys_are_skipped_and_gemini_answer_wins_when_alive(monkeypatch):
    s = {n: FbSess(FbResp(200, GOOD)) for n in ("groq", "cerebras", "mistral", "openrouter")}
    chain = _chain(monkeypatch, s, keys=("mistral",))
    r = RoleRouter(gemini=_gem_down(), fallbacks=chain)
    assert r.ask("market", {}, system="x")["stance"] == "hold" and not s["groq"].calls and len(s["mistral"].calls) == 1
    live = RoleRouter(gemini=type("G", (), {"ask": lambda self, role, p, system=None: {"stance": "buy", "confidence": 0.9, "rationale": "g"}})(), fallbacks=chain)
    assert live.ask("quant", {"z": 1}, system="x")["stance"] == "buy" and len(s["mistral"].calls) == 1   # Gemini жив — запасные не тратим


def test_unknown_model_goes_to_next_model_and_json_mode_retry(monkeypatch):
    monkeypatch.setenv("GROQ_MODELS", "dead-model,live-model")
    s = {"groq": FbSess(FbResp(404, text="model not found"), FbResp(400, text="no json mode"), FbResp(200, GOOD)), "cerebras": FbSess(), "mistral": FbSess(), "openrouter": FbSess()}
    chain = _chain(monkeypatch, s, keys=("groq",))
    out = chain[0].ask("market", {}, system="x")
    assert out["stance"] == "hold"
    models = [c[1]["model"] for c in s["groq"].calls]
    assert models == ["dead-model", "live-model", "live-model"] and "response_format" in s["groq"].calls[1][1] and "response_format" not in s["groq"].calls[2][1]


def test_garbage_answers_fall_through_to_unavailable(monkeypatch):
    s = {n: FbSess(FbResp(200, "не json"), FbResp(200, "")) for n in ("groq", "cerebras", "mistral", "openrouter")}
    r = RoleRouter(gemini=_gem_down(), fallbacks=_chain(monkeypatch, s))
    with pytest.raises(llm.LLMUnavailable):
        r.ask("market", {}, system="x")


def test_github_models_not_in_chain():
    # GitHub Models выведен из эксплуатации 30.07.2026 (эндпоинт отвечает 200 text/plain «OK») — в цепочке его быть не должно
    assert "github" not in [b.name for b in llm.fallback_chain()]


def test_gemini_disabled_flag_sends_everything_to_fallbacks(monkeypatch):
    monkeypatch.setenv("GEMINI_DISABLED", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    with pytest.raises(llm.LLMUnavailable):
        GeminiBackend(session=object()).ask("market", {})


@pytest.fixture
def or_env(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "OR_SPEND_FILE", tmp_path / "or.json")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k-or")
    monkeypatch.delenv("OPENROUTER_MODELS", raising=False)
    llm.OpenAICompatBackend._pause.clear()
    llm.OpenAICompatBackend._model_pause.clear()


def _or(sess):
    return llm.OpenAICompatBackend("openrouter", session=sess, min_interval=0)


def test_openrouter_is_first_fallback_free_then_paid_order():
    b = llm.fallback_chain()
    assert b[0].name == "openrouter"
    ms = b[0].models()
    assert all(m.endswith(":free") for m in ms[:-1]) and ms[-1] == "meta-llama/llama-3.3-70b-instruct"


def test_openrouter_free_limit_goes_to_paid_and_spend_is_recorded(or_env):
    paid_resp = FbResp(200, GOOD)
    paid_resp._j["usage"] = {"cost": 0.0004}
    sess = FbSess(FbResp(429, text="free limit"), FbResp(429, text="free limit"), FbResp(429, text="free limit"), paid_resp)
    assert _or(sess).ask("market", {}, system="x")["stance"] == "hold"
    models = [c[1]["model"] for c in sess.calls]
    assert models[-1] == "meta-llama/llama-3.3-70b-instruct" and all(m.endswith(":free") for m in models[:-1])
    assert all(c[1]["usage"] == {"include": True} for c in sess.calls)
    b = llm.or_spend_load()
    assert b["usd"] == pytest.approx(0.0004) and b["paid_calls"] == 1
    # бесплатные модели на паузе — следующий вызов сразу идёт на платную
    sess2 = FbSess(FbResp(200, GOOD))
    _or(sess2).ask("news", {}, system="x")
    assert [c[1]["model"] for c in sess2.calls] == ["meta-llama/llama-3.3-70b-instruct"]


def test_openrouter_paid_blocked_at_daily_cap(or_env):
    llm.or_spend_add(0.20, True)
    sess = FbSess(FbResp(429), FbResp(429), FbResp(429), FbResp(200, GOOD))
    with pytest.raises(llm.LLMUnavailable, match="потолок"):
        _or(sess).ask("market", {}, system="x")
    assert len(sess.calls) == 3          # платная не вызывалась
    r = RoleRouter(gemini=_gem_down(), fallbacks=[_or(FbSess())])
    llm.OpenAICompatBackend._pause.clear()
    with pytest.raises(llm.LLMUnavailable, match="аналитики недоступны: лимит моделей"):
        r.ask("market", {"x": 1}, system="x")


def test_openrouter_line_shows_spend_and_balance(or_env):
    llm.or_spend_add(0.0123, True)

    class S:
        def get(self, url, headers=None, timeout=None):
            class R:
                def json(self_):
                    return {"data": {"total_credits": 8, "total_usage": 1.97}}
            return R()
    line = llm.or_line(S())
    assert "0.012 $" in line and "6.03 $" in line and line.startswith("OpenRouter: потрачено за день")
