"""Защита позиции от срочной негативной новости: совет (3 роли) — закрыть по рынку или подтянуть стоп до −1% цены; отдельное сообщение."""
import threading

import pytest

from office import config, llm, newsguard
from office.board import Board
from office.cloud import Office
from office.journal import Journal


class C:
    def account_view(self): return {"equity": 100_000.0, "last_equity": 100_000.0, "cash": 50_000.0, "positions": []}
    def price(self, s): return 96.2
    def clock(self): return {"is_open": True}


class Broker:
    def __init__(self): self.sold, self.moved, self.move_ok = [], [], True
    def move_stop(self, name, stop):
        self.moved.append((name, stop))
        return self.move_ok
    def sell(self, name, price, ts, reason):
        self.sold.append((name, reason))
        return {"pnl": -380.0}


class Router:
    def __init__(self, answers): self.answers, self.calls = answers, []
    def ask(self, role, payload, system=None):
        self.calls.append((role, payload))
        a = self.answers[role]
        if isinstance(a, Exception):
            raise a
        return a


INFO = {"note": "опасения по долгам", "headline": "Debt concerns", "sentiment": -0.7}


@pytest.fixture
def o(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STOP_FILE", tmp_path / "STOP")
    sent = []
    off = Office(board=Board(), journal=Journal(), client=C(), hall=type("H", (), {"exec_lock": threading.RLock()})(), notify=sent.append, router=Router({}))
    off.broker, off.sent, off.stock_open = Broker(), sent, True
    off.j._x("INSERT INTO positions(asset, qty, avg, stop, take, fees, run_id, opened_ts) VALUES('CRWV',10,100,92,120,0,1,'2026-10-20T00:00:00+00:00')")
    return off


def answers(*acts):
    return {r: {"action": a, "reason": f"причина {r}"} for r, a in zip(newsguard.ROLES, acts)}


def test_one_close_vote_only_tightens_stop_to_minus_1pct(o):
    o.router = Router(answers("close", "tighten", "tighten"))
    assert newsguard.handle(o, "CRWV", INFO) == "tighten"
    assert o.broker.moved == [("CRWV", pytest.approx(96.2 * 0.99))] and not o.broker.sold
    m = o.sent[-1]
    assert "подтянуть стоп" in m and "Причина:" in m and "опасения по долгам" in m


def test_two_close_votes_sell_at_market_with_reason(o):
    o.router = Router(answers("close", "close", "tighten"))
    assert newsguard.handle(o, "CRWV", INFO) == "close"
    assert o.broker.sold == [("CRWV", "news")] and not o.broker.moved
    assert "закрыть по рынку" in o.sent[-1] and "−380" in o.sent[-1].replace("-", "−")


def test_council_unavailable_defaults_to_tighten_and_says_so(o):
    o.router = Router({r: llm.LLMUnavailable("лимит") for r in newsguard.ROLES})
    assert newsguard.handle(o, "CRWV", INFO) == "tighten"
    assert "совет не ответил" in o.sent[-1] and o.broker.moved


def test_stop_already_tight_is_left_alone(o):
    o.j._x("UPDATE positions SET stop=96.0 WHERE asset='CRWV'")
    o.router = Router(answers("tighten", "tighten", "tighten"))
    assert newsguard.handle(o, "CRWV", INFO) == "noop"
    assert not o.broker.moved and not o.broker.sold and "оставить как есть" in o.sent[-1]


def test_failed_stop_move_falls_back_to_close(o):
    o.broker.move_ok = False
    o.router = Router(answers("tighten", "tighten", "tighten"))
    assert newsguard.handle(o, "CRWV", INFO) == "close" and o.broker.sold


def test_positive_or_neutral_news_does_nothing(o):
    o.router = Router(answers("close", "close", "close"))
    assert newsguard.handle(o, "CRWV", {**INFO, "sentiment": 0.6}) is None
    assert not o.router.calls and not o.sent


def test_office_urgent_routes_held_asset_to_guard_even_on_pause(o):
    o.router = Router(answers("close", "close", "close"))
    config.STOP_FILE.touch()
    o.j.kv_set("urgent_info:CRWV", INFO)
    o.hall.run_asset = lambda s: pytest.fail("зал не должен разбирать актив в позиции")
    o.urgent("CRWV")
    assert o.broker.sold == [("CRWV", "news")]


def test_research_stores_urgent_info(o, monkeypatch):
    # run_news кладёт note/headline/sentiment до вызова on_urgent
    from office import research
    r = research.Research(o.board, o.j, o.client, o.router, o.scope, notify=o.notify)
    seen = []
    r.on_urgent = lambda k: seen.append(o.j.kv_get(f"urgent_info:{k}"))
    heads = {"CRWV": [{"title": "Debt concerns", "published": None}]}
    import office.research as rs
    monkeypatch.setattr(rs.data, "headlines", lambda sym, *a, **k: [{"title": "Debt concerns", "published": None}])
    r.symbols = lambda: ["CRWV"]
    r._ask = lambda role, payload, sysm, v: v({"assets": {"CRWV": {"sentiment": -0.7, "urgent": True, "note": "опасения по долгам", "headline_index": 0}}})
    r.run_news()
    assert seen and seen[0]["sentiment"] == -0.7 and seen[0]["headline"] == "Debt concerns" and heads


# ── v5.8.1: биржа акций закрыта — действие ждёт открытия ─────────────────────
def votes_close():
    return answers("close", "close", "tighten")


def test_closed_market_queues_close_and_tightens_stop(o):
    o.stock_open = False
    o.router = Router(votes_close())
    assert newsguard.handle(o, "CRWV", INFO) == "queued"
    assert not o.broker.sold                                                     # рыночный приказ не отправлен
    assert o.j.kv_get("news_close:CRWV")["why"]
    assert o.broker.moved == [("CRWV", pytest.approx(96.2 * 0.99))]              # стоп пока подтянут
    m = o.sent[-1]
    assert "закрою при открытии биржи" in m and "16:30 по Минску" in m and "стоп подтянут" in m


def test_queue_waits_while_closed_then_executes_once_at_open(o):
    o.stock_open = False
    o.router = Router(votes_close())
    newsguard.handle(o, "CRWV", INFO)
    assert newsguard.run_queue(o) == [] and not o.broker.sold                    # всё ещё закрыта
    o.stock_open = True
    assert newsguard.run_queue(o) == ["CRWV"] and o.broker.sold == [("CRWV", "news")]
    assert o.j.kv_get("news_close:CRWV") is None and "закрыть по рынку" in o.sent[-1]
    assert newsguard.run_queue(o) == [] and len(o.broker.sold) == 1              # повторно не продаём


def test_queue_dropped_if_position_already_gone(o):
    o.stock_open = False
    o.router = Router(votes_close())
    newsguard.handle(o, "CRWV", INFO)
    o.j._x("DELETE FROM positions WHERE asset='CRWV'")                           # закрылась по стопу
    o.stock_open = True
    assert newsguard.run_queue(o) == [] and not o.broker.sold and o.j.kv_get("news_close:CRWV") is None


def test_queue_failure_keeps_item_and_notifies_once(o):
    o.stock_open = False
    o.router = Router(votes_close())
    newsguard.handle(o, "CRWV", INFO)
    o.stock_open = True
    def boom(*a, **k):
        raise RuntimeError("alpaca down")
    o.broker.sell = boom
    assert newsguard.run_queue(o) == [] and o.j.kv_get("news_close:CRWV") is not None
    newsguard.run_queue(o)
    assert sum("Отложенное закрытие" in m for m in o.sent) == 1


def test_market_closes_between_check_and_order(o):
    from office.alpaca import MarketClosed
    o.router = Router(votes_close())
    def closed(*a, **k):
        raise MarketClosed("закрыта")
    o.broker.sell = closed
    assert newsguard.handle(o, "CRWV", INFO) == "queued" and o.j.kv_get("news_close:CRWV")


def test_crypto_is_never_queued(o):
    o.j._x("INSERT INTO positions(asset, qty, avg, stop, take, fees, run_id, opened_ts) VALUES('BTC/USD',1,100,92,120,0,2,'2026-10-20T00:00:00+00:00')")
    o.stock_open = False
    o.router = Router(votes_close())
    assert newsguard.handle(o, "BTC/USD", INFO) == "close" and o.broker.sold == [("BTC/USD", "news")]


def test_run_queue_is_wired_into_pass(o, monkeypatch):
    seen = []
    monkeypatch.setattr(newsguard, "run_queue", lambda off: seen.append(off.stock_open) or [])
    o.watch = lambda: None
    o.sched.run_due = lambda: []
    o.screen = lambda: None
    o.cycle = lambda: []
    o.earnings_guard = lambda: []
    o.commands = type("Cmd", (), {"poll": lambda s: None, "run": lambda s: None})()
    o.run_once()
    assert seen
