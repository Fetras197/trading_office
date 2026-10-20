"""Правило риска «отчёты компаний»: не входить за 2 торговых дня до отчёта, закрывать накануне; сообщение о стопе между проверками."""
import datetime as dt

import pytest

from office import earnings, report
from office.board import Board
from office.cloud import Office
from office.journal import Journal

D = dt.date


class Cal:
    """Календарь Alpaca: будни, кроме праздников."""
    def __init__(self, holidays=()):
        self.h = set(holidays)

    def _req(self, method, path, params=None):
        a, b = D.fromisoformat(params["start"]), D.fromisoformat(params["end"])
        return [{"date": (a + dt.timedelta(days=i)).isoformat()} for i in range((b - a).days + 1)
                if (a + dt.timedelta(days=i)).weekday() < 5 and (a + dt.timedelta(days=i)) not in self.h]


@pytest.fixture(autouse=True)
def _clear():
    earnings._CAL.clear()


def test_trading_days_count_skips_weekends_and_holidays():
    c = Cal()
    assert earnings.days_until(c, D(2026, 10, 20), D(2026, 10, 20)) == 0 and earnings.days_until(c, D(2026, 10, 20), D(2026, 10, 19)) == 1      # сегодня=пн 19.10 → вт 20.10 = 1
    assert earnings.days_until(c, D(2026, 10, 21), D(2026, 10, 19)) == 2 and earnings.days_until(c, D(2026, 10, 22), D(2026, 10, 19)) == 3
    assert earnings.days_until(c, D(2026, 10, 26), D(2026, 10, 23)) == 1                                                                       # пятница → понедельник = 1 торговый день
    assert earnings.days_until(c, D(2026, 10, 19), D(2026, 10, 23)) is None                                                                    # отчёт уже прошёл
    earnings._CAL.clear()
    assert earnings.days_until(Cal(holidays=[D(2026, 10, 26)]), D(2026, 10, 27), D(2026, 10, 23)) == 1                                       # праздник в понедельник: вторник — первый торговый день


def test_calendar_failure_falls_back_to_weekdays():
    class Broken:
        def _req(self, *a, **k):
            raise RuntimeError("down")
    assert earnings.days_until(Broken(), D(2026, 10, 26), D(2026, 10, 23)) == 1
    assert earnings.days_until(None, D(2026, 10, 21), D(2026, 10, 19)) == 2


def rep(monkeypatch, date, ok=True):
    monkeypatch.setattr(earnings, "next_report", lambda symbol, board=None, today=None: (date, ok))


def test_no_entry_within_two_trading_days_before_report(monkeypatch):
    c, today = Cal(), D(2026, 10, 19)
    for report_date, blocked in ((D(2026, 10, 19), True), (D(2026, 10, 20), True), (D(2026, 10, 21), True), (D(2026, 10, 22), False), (D(2026, 11, 3), False)):
        rep(monkeypatch, report_date)
        why = earnings.entry_block(c, None, "UAL", today)
        assert bool(why) == blocked, report_date
    rep(monkeypatch, D(2026, 10, 20))
    assert "UAL 20.10" in earnings.entry_block(c, None, "UAL", today) and "через 1 торг. дн." in earnings.entry_block(c, None, "UAL", today)
    assert earnings.entry_block(c, None, "BTC/USD", today) == ""                          # крипта отчётов не имеет
    rep(monkeypatch, None)
    assert earnings.entry_block(c, None, "SPY", today) == ""                              # ETF: отчёта нет — можно
    rep(monkeypatch, None, ok=False)
    assert "нет данных о дате отчёта" in earnings.entry_block(c, None, "XYZ", today)      # нет данных — не входим


def test_friday_before_monday_report_blocks_and_exits(monkeypatch):
    c, fri = Cal(), D(2026, 10, 23)
    rep(monkeypatch, D(2026, 10, 26))
    assert earnings.entry_block(c, None, "UAL", fri) and earnings.must_exit(c, None, "UAL", fri)


def test_must_exit_today_and_next_trading_day_only(monkeypatch):
    c, today = Cal(), D(2026, 10, 19)
    for report_date, exit_ in ((D(2026, 10, 19), True), (D(2026, 10, 20), True), (D(2026, 10, 21), False), (None, False)):
        rep(monkeypatch, report_date)
        assert bool(earnings.must_exit(c, None, "UAL", today)) == exit_, report_date
    rep(monkeypatch, D(2026, 10, 20))
    assert "закрыта заранее" in earnings.must_exit(c, None, "UAL", today) and earnings.must_exit(c, None, "ETH/USD", today) == ""
    rep(monkeypatch, None, ok=False)
    assert earnings.must_exit(c, None, "UAL", today) == ""                                # нет данных — не закрываем вслепую


def test_next_report_falls_back_to_saved_events_when_source_fails(monkeypatch):
    monkeypatch.undo()
    from office import sources
    b = Board()
    b.x("INSERT INTO events(key,ts,until,title,ccy,impact,kind,assets,src) VALUES('k','2026-10-20T04:00+00:00',NULL,'Отчёт компании UAL','USD','High','earnings','UAL','yfinance')")
    monkeypatch.setattr(sources, "cached", lambda key, ttl, fn: {"error": "unavailable"})
    assert earnings.next_report("UAL", b, D(2026, 10, 19)) == (D(2026, 10, 20), True)
    assert earnings.next_report("XYZ", b, D(2026, 10, 19)) == (None, False)
    monkeypatch.setattr(sources, "cached", lambda key, ttl, fn: {"Earnings Date": [D(2026, 10, 18), D(2026, 11, 3)]})
    assert earnings.next_report("UAL", b, D(2026, 10, 19)) == (D(2026, 11, 3), True)     # прошедшая дата игнорируется
    monkeypatch.setattr(sources, "cached", lambda key, ttl, fn: {})
    assert earnings.next_report("SPY", b, D(2026, 10, 19)) == (None, True)


class C:
    def account_view(self): return {"equity": 100_000.0, "last_equity": 100_000.0, "cash": 50_000.0, "positions": []}
    def price(self, s): return 101.0
    def clock(self): return {"is_open": True}
    def day_change(self, s): return 0.0
    def _req(self, method, path, params=None): return Cal()._req(method, path, params)


class FakeBroker:
    def __init__(self, j): self.j, self.sold = j, []

    def sell(self, name, px, ts, reason):
        self.sold.append((name, reason))
        self.j._x("DELETE FROM positions WHERE asset=?", (name,))
        self.j._x("INSERT INTO trades(asset,entry_ts,exit_ts,entry,exit,qty,fees,pnl,r_mult,run_id,exit_reason,desk) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (name, "t", ts, 100, px, 10, 0, 10.0, 0, 0, reason, "stocks"))
        return {"price": px, "pnl": 10.0, "reason": reason}


def mk_office(monkeypatch, open_=True):
    sent = []
    j = Journal()
    __import__("office.desks", fromlist=["x"]).seed(j, "2026-10-19T00:00:00+00:00")
    for a, d in (("UAL", "stocks"), ("AAPL", "stocks"), ("BTC/USD", "majors")):
        j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts,desk) VALUES(?,?,?,?,?,?,?,?,?)", (a, 10, 100, 95, 110, 0, 0, "t", d))
    o = Office(board=Board(), journal=j, client=C(), hall=type("H", (), {"exec_lock": __import__("threading").Lock(), "stock_open": True, "fresh_left": 5})(), notify=sent.append, router=object())
    o.broker = FakeBroker(j)
    o.stock_open = open_
    return o, sent


def next_weekday(d):
    d += dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def test_open_position_closed_on_the_eve_of_report_during_market_hours(monkeypatch):
    nxt = next_weekday(earnings.today_ny())                                   # следующий торговый день (в тестовом календаре — следующий будний)
    monkeypatch.setattr(earnings, "next_report", lambda symbol, board=None, today=None: ((nxt, True) if symbol == "UAL" else (None, True)))
    o, sent = mk_office(monkeypatch)
    done = o.earnings_guard()
    assert done == ["UAL"] and o.broker.sold == [("UAL", "earnings")]
    assert "перед отчётом компании" in sent[0] and "Отдел: Акции США" in sent[0] and "закрыта заранее" in sent[0]
    assert o.j.positions().keys() == {"AAPL", "BTC/USD"}                      # остальные позиции не тронуты


def test_guard_does_nothing_when_market_closed_or_report_is_far(monkeypatch):
    nxt = next_weekday(earnings.today_ny())
    monkeypatch.setattr(earnings, "next_report", lambda symbol, board=None, today=None: (nxt, True))
    o, sent = mk_office(monkeypatch, open_=False)
    assert o.earnings_guard() == [] and not o.broker.sold                                              # биржа закрыта — продать нельзя, ждём открытия
    monkeypatch.setattr(earnings, "next_report", lambda symbol, board=None, today=None: (earnings.today_ny() + dt.timedelta(days=30), True))
    o2, _ = mk_office(monkeypatch)
    assert o2.earnings_guard() == []


def test_sell_failure_is_reported_once_not_crash(monkeypatch):
    monkeypatch.setattr(earnings, "next_report", lambda symbol, board=None, today=None: (earnings.today_ny(), True))
    o, sent = mk_office(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("alpaca down")
    o.broker.sell = boom
    assert o.earnings_guard() == []
    assert sum("Не удалось закрыть" in m for m in sent) == 2                                          # UAL и AAPL — по одному сообщению (BTC отчётов не имеет)
    o.earnings_guard()
    assert sum("Не удалось закрыть" in m for m in sent) == 2                                          # повтор не спамит


def test_stop_message_wording_between_checks():
    m = report.close_message("UAL", -120.0, -0.02, "stop", offline=True)
    assert "между проверками офиса, заявка на бирже" in m and "выключен" not in m
    assert "между проверками офиса, заявка на бирже" in report.close_message("UAL", 80.0, 0.02, "take", offline=True)
    assert "выключен" not in report.close_message("UAL", -1.0, 0.0, "stop") and "перед отчётом" in report.close_message("UAL", 5.0, 0.0, "earnings")
