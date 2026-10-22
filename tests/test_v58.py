"""v5.8: отчёт крупного конкурента (KLAC/LRCX) и «за день» от последнего вечернего отчёта."""
import datetime as dt

import pytest

from office import daybase, earnings, report
from office.alpaca import AlpacaClient
from office.board import Board
from office.journal import Journal
from tests.test_earnings import Cal, mk_office

D = dt.date
TUE = D(2026, 10, 20)       # вторник


@pytest.fixture(autouse=True)
def _clear():
    earnings._CAL.clear()


def reports(monkeypatch, table):
    """next_report: {тикер: дата}; нет в таблице — данные есть, ближайшего отчёта нет."""
    monkeypatch.setattr(earnings, "next_report", lambda symbol, board=None, today=None: (table.get(symbol), True))


def test_klac_blocked_and_closed_when_lrcx_reports_within_two_trading_days(monkeypatch):
    cal = Cal()
    for lrcx, n in ((TUE, 0), (D(2026, 10, 21), 1), (D(2026, 10, 22), 2)):
        reports(monkeypatch, {"LRCX": lrcx})
        b = earnings.entry_block(cal, Board(), "KLAC", TUE)
        assert "LRCX" in b and "полупроводники" in b and "не входим" in b, (n, b)
        x = earnings.must_exit(cal, Board(), "KLAC", TUE)
        assert "LRCX" in x and "позиция закрыта заранее" in x
    reports(monkeypatch, {"LRCX": D(2026, 10, 23)})                         # через 3 торговых дня — рано
    assert earnings.entry_block(cal, Board(), "KLAC", TUE) == "" and earnings.must_exit(cal, Board(), "KLAC", TUE) == ""
    reports(monkeypatch, {"LRCX": D(2026, 10, 19)})                         # отчёт уже прошёл
    assert earnings.entry_block(cal, Board(), "KLAC", TUE) == ""


def test_symmetric_and_weekend_calendar(monkeypatch):
    cal = Cal()
    reports(monkeypatch, {"KLAC": D(2026, 10, 21)})
    assert "KLAC" in earnings.entry_block(cal, Board(), "LRCX", TUE)         # и наоборот
    fri = D(2026, 10, 23)
    reports(monkeypatch, {"AMAT": D(2026, 10, 26)})                          # понедельник = 1 торговый день от пятницы
    assert "AMAT" in earnings.entry_block(cal, Board(), "KLAC", fri)
    reports(monkeypatch, {"AMAT": D(2026, 10, 28)})                          # среда = 3 торговых дня
    assert earnings.entry_block(cal, Board(), "KLAC", fri) == ""


@pytest.mark.parametrize("sym,peer,key", [("BAC", "JPM", "банки"), ("TGT", "WMT", "ритейл"), ("CVX", "XOM", "нефть")])
def test_other_industries(monkeypatch, sym, peer, key):
    reports(monkeypatch, {peer: D(2026, 10, 21)})
    r = earnings.entry_block(Cal(), Board(), sym, TUE)
    assert peer in r and key in r


def test_other_groups_and_non_stocks_are_not_blocked(monkeypatch):
    reports(monkeypatch, {"JPM": D(2026, 10, 21), "LRCX": D(2026, 10, 21), "XOM": D(2026, 10, 21)})
    for sym in ("AAPL", "SPY", "BTC/USD", "XLF"):
        assert earnings.entry_block(Cal(), Board(), sym, TUE) == "", sym
    assert earnings.entry_block(Cal(), Board(), "KLAC", TUE).count("JPM") == 0                 # банк не конкурент полупроводников
    assert earnings.group_of("AAPL") is None and earnings.group_of("KLAC") == "semis"


def test_own_report_rules_unchanged_and_peer_without_data_ignored(monkeypatch):
    cal = Cal()
    monkeypatch.setattr(earnings, "next_report", lambda s, board=None, today=None: ((D(2026, 10, 21), True) if s == "KLAC" else (None, False)))
    assert "отчёт компании KLAC" in earnings.entry_block(cal, Board(), "KLAC", TUE)           # собственный отчёт — прежний текст
    monkeypatch.setattr(earnings, "next_report", lambda s, board=None, today=None: (None, False))
    assert "нет данных о дате отчёта" in earnings.entry_block(cal, Board(), "KLAC", TUE)      # нет данных о своей дате — как раньше
    monkeypatch.setattr(earnings, "next_report", lambda s, board=None, today=None: ((None, True) if s == "KLAC" else (_ for _ in ()).throw(RuntimeError("yf"))))
    assert earnings.entry_block(cal, Board(), "KLAC", TUE) == ""                                # сбой у конкурента не блокирует


def test_guard_sells_klac_with_peer_reason(monkeypatch):
    from tests.test_earnings import next_weekday
    reports(monkeypatch, {"LRCX": next_weekday(earnings.today_ny())})
    o, sent = mk_office(monkeypatch)
    o.j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts,desk) VALUES('KLAC',10,100,95,110,0,0,'t','stocks')")
    done = o.earnings_guard()
    assert done == ["KLAC"] and o.broker.sold == [("KLAC", "peer")]
    assert "перед отчётом крупного конкурента" in sent[0] and "LRCX" in sent[0] and "закрыта заранее" in sent[0] and "Отдел: Акции США" in sent[0]
    assert o.j.positions().keys() == {"UAL", "AAPL", "BTC/USD"}                      # остальные позиции не тронуты (у них нет конкурентов с отчётом)


def test_guard_waits_for_open_market(monkeypatch):
    from tests.test_earnings import next_weekday
    reports(monkeypatch, {"LRCX": next_weekday(earnings.today_ny())})
    o, sent = mk_office(monkeypatch, open_=False)
    o.j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts,desk) VALUES('KLAC',10,100,95,110,0,0,'t','stocks')")
    assert o.earnings_guard() == [] and not o.broker.sold


def test_close_message_peer():
    m = report.close_message("KLAC", -280.0, -0.028, "peer", desk="Акции США")
    assert m.startswith("🔴") and "перед отчётом крупного конкурента" in m
    assert report.close_message("KLAC", 50.0, 0.01, "peer").startswith("🟢")


# ── «за день» от последнего вечернего отчёта ───────────────────────────────────
def mk_client(equity=99_630.0, broker_last=100_554.0):
    c = AlpacaClient("k", "s")
    c.account = lambda: {"equity": str(equity), "cash": "50000", "last_equity": str(broker_last)}
    c.positions = lambda: []
    return c


def test_account_view_uses_base_and_keeps_broker_value():
    c = mk_client()
    v = c.account_view()
    assert v["last_equity"] == 100_554.0 and v["broker_last_equity"] == 100_554.0               # базы нет — цифры брокера
    c.base_fn = lambda: 99_312.0
    v = c.account_view()
    assert v["last_equity"] == 99_312.0 and v["broker_last_equity"] == 100_554.0
    assert abs((v["equity"] / v["last_equity"] - 1) - 0.0032) < 5e-5                              # +0,32% вместо −0,92%
    c.base_fn = lambda: (_ for _ in ()).throw(RuntimeError("kv"))
    assert c.account_view()["last_equity"] == 100_554.0                                          # сбой базы не ломает отчёты


def test_daybase_seed_get_commit_and_rerun():
    j = Journal()
    assert daybase.get(j, "2026-10-22") is None
    j.q("SELECT 1")
    j._x("INSERT INTO equity(ts,equity) VALUES('2026-10-21T20:29:20+00:00', 99312.0)")
    j._x("INSERT INTO equity(ts,equity) VALUES('2026-10-22T05:48:56+00:00', 98515.0)")
    j.kv_set("job:evening:last", "2026-10-21T23:29:21+03:00")
    assert daybase.get(j, "2026-10-22") == 99312.0                                               # посев: эквити журнала у вечернего отчёта
    assert j.kv_get("day_base")["day"] == "2026-10-21"
    assert daybase.get(j, "2026-10-22", evening=True) == 99312.0                                  # вечером 22-го сравниваем с вечером 21-го
    daybase.commit(j, 99700.0, "2026-10-22")
    assert daybase.get(j, "2026-10-23") == 99700.0 and daybase.get(j, "2026-10-22", evening=True) == 99312.0
    daybase.commit(j, 12345.0, "2026-10-22")                                                      # повтор в те же сутки базу не двигает
    assert daybase.get(j, "2026-10-23") == 99700.0


def test_daybase_no_seed_without_evening_or_far_equity():
    j = Journal()
    j.kv_set("job:evening:last", "2026-10-21T23:29:21+03:00")
    j._x("INSERT INTO equity(ts,equity) VALUES('2026-10-21T10:00:00+00:00', 1.0)")                # снимок далеко от отчёта
    assert daybase.get(j, "2026-10-22") is None


def test_office_wires_base_into_client_and_evening_commits(monkeypatch):
    from office import config
    today = config.localnow().date().isoformat()
    o, sent = mk_office(monkeypatch)
    o.j.kv_set("day_base", {"day": "2026-10-21", "equity": 99_312.0, "prev": None})
    assert o.client.base_fn() == 99_312.0
    o._evening = lambda: None                                                      # сам отчёт проверен в других тестах; здесь — смена базы
    o.client.account_view = lambda: {"equity": 99_630.0, "cash": 1.0, "last_equity": 1.0, "positions": []}
    seen = []
    o._evening = lambda: seen.append(o.client.base_fn())                           # во время отчёта база — прошлый вечер
    o.evening()
    assert seen == [99_312.0]
    assert o.j.kv_get("day_base") == {"day": today, "equity": 99_630.0, "prev": 99_312.0}
    assert o.client.base_fn() == 99_630.0                                          # после отчёта «за день» считается от сегодняшнего вечера
    o.evening()                                                                    # повторный запуск в те же сутки базу не двигает
    assert o.j.kv_get("day_base")["equity"] == 99_630.0
