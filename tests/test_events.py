import datetime as dt

from office import events
from office.board import Board

UTC = dt.timezone.utc


def mk(board, ts, title, assets, impact="High", until=None, kind="macro"):
    board.x("INSERT INTO events(key,ts,until,title,ccy,impact,kind,assets,src) VALUES(?,?,?,?,?,?,?,?,?)", (title + ts, ts, until, title, "USD", impact, kind, assets, "t"))


def test_blocked_window_30min_around_high_usd_event():
    b = Board()
    t0 = dt.datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
    mk(b, t0.isoformat(), "FOMC Statement", "all")
    f = lambda m: events.blocked(b, "BTC/USD", "crypto", t0 + dt.timedelta(minutes=m))  # noqa: E731
    assert f(-31) == "" and f(-29) == "решение ФРС по ставке" and f(0) and f(29) and f(31) == ""


def test_medium_and_foreign_events_do_not_block_but_earnings_block_only_own_stock():
    b = Board()
    t0 = dt.datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    mk(b, t0.isoformat(), "Core CPI m/m", "", impact="High")           # не USD-связанный (assets пусто) — не блокирует
    mk(b, t0.isoformat(), "ISM", "all", impact="Medium")
    mk(b, (t0 - dt.timedelta(hours=12)).isoformat(), "Отчёт компании AAPL", "AAPL", until=(t0 + dt.timedelta(hours=22)).isoformat(), kind="earnings")
    assert events.blocked(b, "BTC/USD", "crypto", t0) == ""
    assert events.blocked(b, "AAPL", "stock", t0) == "Отчёт компании AAPL"
    assert events.blocked(b, "MSFT", "stock", t0) == ""


def test_title_ru_and_upcoming():
    assert events.title_ru({"kind": "macro", "title": "Core CPI m/m", "ccy": "USD"}) == "базовая инфляция (цены для потребителей)"
    assert events.title_ru({"kind": "macro", "title": "Something odd", "ccy": "EUR"}) == "событие (EUR)"
    b = Board()
    now = dt.datetime(2026, 10, 12, 6, 0, tzinfo=UTC)
    mk(b, "2026-10-12T12:30+00:00", "CPI", "all")
    mk(b, "2026-10-15T12:30+00:00", "PPI", "all")
    assert [e["title"] for e in events.upcoming(b, 24, now)] == ["CPI"]
