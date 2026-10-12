import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from office import data, events, research, scheduler, sources
from office.board import Board
from office.journal import Journal
from office.llm import LLMError, LLMUnavailable

TZ = ZoneInfo("Europe/Minsk")


class Client:
    def account_view(self):
        return {"equity": 100000.0, "cash": 85000.0, "last_equity": 100000.0,
                "positions": [{"symbol": "ONDOUSD", "qty": 100, "avg": 0.5, "price": 0.51, "value": 15000.0, "pl": 200.0, "plpc": 0.02}]}

    def day_change(self, s):
        return 0.01


class Router:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def ask(self, role, payload, system=None):
        self.calls.append(role)
        return self.handler(role, payload)


def mk(handler, held=("ONDO/USD",), cands=("SOL/USD",), notify=None):
    sent = []
    r = research.Research(Board(), Journal(), Client(), Router(handler), lambda: {"held": list(held), "candidates": list(cands)}, notify=notify or sent.append)
    return r, sent


PIC_OK = {"regime": "risk_off", "summary": "Осторожность: ждём решение ФРС.", "main_risks": ["решение ФРС"], "priority_assets": ["SOL/USD", "FAKE"], "avoid_assets": ["ONDO/USD"],
          "forbidden": ["не покупать перед ФРС"], "no_new_entries": False, "size_mult": 0.5, "confidence": 0.7, "expectation": "боковик", "lesson_of_previous": ""}


def test_risk_score_rules():
    calm = {"VIX": {"last": 14}, "SPX": {"above_sma50": True}, "DXY": {"chg_20d": 0.0}, "US10Y": {"chg_20d": 0.0}}
    assert research.risk_score(calm)["regime"] == "risk_on"
    bad = {"VIX": {"last": 33}, "SPX": {"above_sma50": False}, "DXY": {"chg_20d": 0.05}, "US10Y": {"chg_20d": 0.1}}
    assert research.risk_score(bad, {"value": 10})["regime"] == "risk_off"
    assert research.risk_score({})["regime"] == "neutral"


def test_picture_validation_clamps_to_known_assets_and_rejects_bad_numbers():
    p = research.v_picture({**PIC_OK, "regime": "Risk-Off"}, known_assets={"SOL/USD", "ONDO/USD"})
    assert p["regime"] == "risk_off" and p["priority_assets"] == ["SOL/USD"]
    with pytest.raises(ValueError):
        research.v_picture({**PIC_OK, "size_mult": 1.5})
    with pytest.raises(ValueError):
        research.v_picture({**PIC_OK, "regime": "bullish"})


def test_picture_published_and_cited_as_actual():
    r, _ = mk(lambda role, p: PIC_OK)
    rid = r.run_picture()
    cur = r.current_picture()
    assert cur["id"] == rid and cur["data"]["size_mult"] == 0.5 and "FAKE" not in cur["data"]["priority_assets"]
    assert r.board.q("SELECT COUNT(*) n FROM memory WHERE kind='picture'")[0]["n"] == 1   # 1.8 память


def test_picture_falls_back_to_mechanical_when_llm_down():
    def boom(role, p):
        raise LLMUnavailable("limit")
    r, _ = mk(boom)
    r.run_picture()
    d = r.current_picture()["data"]
    assert d["mechanical"] and d["size_mult"] == 0.5 and d["confidence"] <= 0.3        # зал не встаёт, но осторожен


def test_picture_retries_once_on_invalid_answer():
    calls = []

    def h(role, p):
        calls.append(1)
        return {"regime": "???"} if len(calls) == 1 else PIC_OK
    r, _ = mk(h)
    r.run_picture()
    assert len(calls) == 2 and not r.current_picture()["data"].get("mechanical")


def test_ensure_picture_reuses_fresh_and_regenerates_stale():
    r, _ = mk(lambda role, p: PIC_OK)
    a = r.ensure_picture()["id"]
    assert r.ensure_picture()["id"] == a           # свежая не пересоздаётся
    r.board.x("UPDATE reports SET ts='2020-01-01T00:00:00+00:00'")
    assert r.ensure_picture()["id"] != a


def test_news_urgent_alert_sent_once(monkeypatch):
    monkeypatch.setattr(data, "news", lambda q, n=8: [{"title": "ONDO exploit drains pool", "published": "now"}])
    monkeypatch.setattr(data, "yf_news", lambda t, n=8: [])
    out = {"assets": {"ONDO/USD": {"sentiment": -0.9, "urgent": True, "note": "взлом пула", "headline_index": 0}, "SOL/USD": {"sentiment": 0.1, "urgent": False, "note": "", "headline_index": -1}}}
    r, sent = mk(lambda role, p: out)
    urgent = []
    r.on_urgent = urgent.append
    r.run_news()
    r.run_news()
    assert len(sent) == 1 and "ONDO" in sent[0] and "взлом" in sent[0] and urgent == ["ONDO/USD"]
    assert r.board.latest("newsdesk")["data"]["assets"]["ONDO/USD"]["sentiment"] == -0.9


def test_events_report_and_refresh(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    ev = [{"ts": (now + dt.timedelta(hours=3)).isoformat(timespec="minutes"), "title": "Core CPI m/m", "ccy": "USD", "impact": "High", "kind": "macro", "assets": "all", "src": "t"}]
    monkeypatch.setattr(sources, "faireconomy_events", lambda: ev)
    monkeypatch.setattr(sources, "deribit_expiry", lambda: [])
    monkeypatch.setattr(sources, "earnings_events", lambda s: [])
    r, _ = mk(lambda role, p: {})
    r.run_events()
    rep = r.board.latest("calendar")
    assert "1 важных" in rep["verdict"] and "базовая инфляция" in rep["verdict"]
    assert events.blocked(r.board, "BTC/USD", "crypto", now + dt.timedelta(hours=3, minutes=10))


def test_macro_falls_back_to_rules_on_invalid_answer(monkeypatch):
    monkeypatch.setattr(research.Research, "macro_data", lambda self: {"levels": {"VIX": {"last": 14}, "SPX": {"above_sma50": True}}, "fear_greed": {"error": "x"}, "fed": {}, "inflation_labor": {}, "prediction_markets": []})

    def bad(role, p):
        raise LLMError("мусор")
    r, _ = mk(bad)
    r.run_macro()
    d = r.board.latest("macro")["data"]
    assert d["mechanical"] and d["regime"] == "risk_on"


# ---------- планировщик ----------
def mkjob(**kw):
    ran = []
    kv = Journal()
    s = scheduler.Scheduler(kv, [scheduler.Job("j", lambda: ran.append(1), **kw)], log=lambda *a: None)
    return s, ran


def at(h, m, d=12):
    return dt.datetime(2026, 10, d, h, m, tzinfo=TZ)


def test_daily_job_runs_once_in_window_and_catches_up_within_limit():
    s, ran = mkjob(at=["07:30"], catchup_h=3)
    s.kv.kv_set("job:j:last", at(7, 30, 11).isoformat())          # вчера отработало
    assert s.run_due(at(7, 29)) == []
    assert s.run_due(at(7, 31)) == ["j"] and s.run_due(at(7, 40)) == []   # раз в сутки
    s, ran = mkjob(at=["07:30"], catchup_h=3)
    s.kv.kv_set("job:j:last", at(7, 30, 11).isoformat())
    assert s.run_due(at(9, 0)) == ["j"]                            # телефон спал — догнали
    s, ran = mkjob(at=["07:30"], catchup_h=3)
    s.kv.kv_set("job:j:last", at(7, 30, 11).isoformat())
    assert s.run_due(at(12, 0)) == []                              # слишком поздно — не запускаем утренний брифинг в обед


def test_job_bootstrap_interval_failure_retry_trigger():
    s, ran = mkjob(at=["07:30"])
    assert s.run_due(at(15, 0)) == ["j"]                           # никогда не запускалось — сразу
    s, ran = mkjob(every=3600)
    assert s.run_due(at(10, 0)) == ["j"] and s.run_due(at(10, 30)) == [] and s.run_due(at(11, 1)) == ["j"]
    fails = []
    s = scheduler.Scheduler(Journal(), [scheduler.Job("f", lambda: 1 / 0, every=3600, retry_s=600)], log=fails.append)
    assert s.run_due(at(10, 0)) == [] and s.run_due(at(10, 5)) == [] and fails           # упало, но планировщик жив
    s.kv.kv_set("job:f:attempt", at(10, 0).isoformat())
    assert s.is_due(s.jobs[0], at(10, 11)) is True                                     # повтор через retry_s
    s, ran = mkjob(every=3600)
    s.run_due(at(10, 0))
    s.trigger("j")
    assert s.run_due(at(10, 1)) == ["j"]                                               # внеочередной запуск (например, после сделки)


def test_weekly_job_only_on_its_weekday():
    s, ran = mkjob(at=["20:00"], weekday=6, catchup_h=3)           # воскресенье
    s.kv.kv_set("job:j:last", at(20, 0, 4).isoformat())
    assert s.run_due(at(20, 5, 12)) == []                          # 12 октября 2026 — понедельник
    assert s.run_due(at(20, 5, 11)) == ["j"]                       # 11-е — воскресенье


def test_supervisor_wiring_without_running_anything():
    """Только сборка (без запуска потоков/циклов): задания отдела на месте, зал получает ensure_picture, срочная новость подключена."""
    from office.supervisor import Supervisor

    class C:
        def account_view(self): return {}
        def price(self, s): return 1.0
    s = Supervisor(board=Board(), journal=Journal(), client=C(), hall=object(), notify=lambda t: None)
    assert [j.name for j in s.sched.jobs] == ["events", "macro", "macro_pre_event", "news", "crypto", "sectors", "portfolio", "shadow", "review", "weights", "picture"]
    assert s.research.on_urgent == s.urgent and s.scope() == {"held": [], "candidates": []}
