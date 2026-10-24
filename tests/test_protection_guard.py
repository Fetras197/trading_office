"""Проверка защиты на каждом проходе: восстановление, Telegram, перенос закрытия по новости на открытие биржи."""
import threading

import pytest

from office import config, newsguard, report
from office.alpaca import AlpacaError
from office.board import Board
from office.cloud import Office
from office.journal import Journal


class C:
    def account_view(self): return {"equity": 100_000.0, "last_equity": 100_000.0, "cash": 50_000.0, "positions": []}
    def price(self, s): return 100.0
    def clock(self): return {"is_open": False}


class A:
    def __init__(self, alpaca): self.alpaca = alpaca


class Broker:
    def __init__(self):
        self.assets = {"MSFT": A("MSFT"), "ISRG": A("ISRG"), "BTC/USD": A("BTC/USD")}
        self.live, self.results, self.calls = {"MSFT": 1, "ISRG": 1, "BTCUSD": 1}, {}, []

    def live_positions(self): return self.live

    def verify_protection(self, name, market_open=True, fix=True):
        self.calls.append((name, market_open))
        r = self.results.get(name)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def o(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STOP_FILE", tmp_path / "STOP")
    sent = []
    off = Office(board=Board(), journal=Journal(), client=C(), hall=type("H", (), {"exec_lock": threading.RLock()})(), notify=sent.append, router=object())
    off.broker, off.sent, off.stock_open = Broker(), sent, False
    for a in ("MSFT", "ISRG", "BTC/USD"):
        off.j._x("INSERT INTO positions(asset, qty, avg, stop, take, fees, run_id, opened_ts) VALUES(?,10,100,95,110,0,1,'t')", (a,))
    return off


def res(name, **kw):
    return {"name": name, "missing": ["стоп", "цель"], "stray_market": 0, "mismatch": None, "stop": 95.0, "take": 110.0, "fixed": True, "error": None, **kw}


def test_nothing_to_report_when_everything_is_in_place(o):
    assert o.protection_guard() == [] and o.sent == []
    assert [c[0] for c in o.broker.calls] == ["MSFT", "ISRG", "BTC/USD"]                  # проверены все позиции
    assert dict(o.broker.calls)["BTC/USD"] is True and dict(o.broker.calls)["MSFT"] is False   # для акций учтено, что биржа закрыта


def test_restored_protection_is_reported_with_levels(o):
    o.broker.results["MSFT"] = res("MSFT", missing=["цель"])
    out = o.protection_guard()
    assert len(out) == 1 and "🛡 Проверка защиты" in o.sent[0] and "не было: цель" in o.sent[0] and "Восстановил по сохранённым уровням: стоп 95, цель 110" in o.sent[0]


def test_stray_market_order_moves_close_to_market_open(o):
    o.broker.results["ISRG"] = res("ISRG", stray_market=1)
    o.protection_guard()
    q = o.j.kv_get(newsguard.KEY + "ISRG")
    assert q and "при открытии" in q["why"]
    assert "закрою при открытии биржи: " in o.sent[0] and "по Минску" in o.sent[0] and "висел рыночный приказ" in o.sent[0]
    o.stock_open = True                                                                   # открытие: очередь исполняется как обычно
    o.broker.sell = lambda name, price, ts, reason: {"pnl": -50.0}
    assert newsguard.run_queue(o) == ["ISRG"]


def test_existing_queue_entry_not_overwritten(o):
    o.j.kv_set(newsguard.KEY + "MSFT", {"why": "своё", "head": "h", "ts": "t"})
    o.broker.results["MSFT"] = res("MSFT", stray_market=1)
    o.protection_guard()
    assert o.j.kv_get(newsguard.KEY + "MSFT")["why"] == "своё"


def test_failure_is_reported_at_most_every_two_hours(o):
    o.broker.results["MSFT"] = res("MSFT", fixed=False, error="insufficient qty")
    o.protection_guard()
    o.protection_guard()
    assert sum("Восстановить не удалось" in m for m in o.sent) == 1 and "вручную" in o.sent[0]


def test_one_broken_position_does_not_stop_the_others(o):
    o.broker.results["MSFT"] = AlpacaError("api down")
    o.broker.results["ISRG"] = res("ISRG")
    out = o.protection_guard()
    assert len(out) == 1 and out[0]["name"] == "ISRG"
    assert any("Не удалось проверить защитные заявки" in m for m in o.sent) and any("Проверка защиты" in m for m in o.sent)


def test_positions_not_live_at_broker_are_skipped(o):
    o.broker.live = {"BTCUSD": 1}
    o.protection_guard()
    assert [c[0] for c in o.broker.calls] == ["BTC/USD"]


def test_guard_runs_every_pass(o, monkeypatch):
    seen = []
    o.protection_guard = lambda: seen.append(1) or []
    o.watch = lambda: None
    o.sched.run_due = lambda: []
    o.screen = lambda: None
    o.cycle = lambda: []
    o.earnings_guard = lambda: []
    o.commands = type("Cmd", (), {"poll": lambda s: None, "run": lambda s: None})()
    monkeypatch.setattr(newsguard, "run_queue", lambda off: [])
    o.run_once()
    assert seen


def test_message_shapes():
    m = report.protection_message("BTC/USD", {"missing": ["стоп"], "stray_market": 0, "mismatch": None, "stop": 79419.19, "take": 86090.2, "fixed": True, "error": None})
    assert "цель" not in m.split("\n")[1] and "стоп 79419.2" in m                               # у крипты цели на бирже нет — в сообщении не обещаем
    m2 = report.protection_message("MSFT", {"missing": [], "stray_market": 0, "mismatch": "стоп на бирже 90, в журнале 95", "stop": 95.0, "take": 110.0, "fixed": True, "error": None})
    assert "расхождение" in m2
