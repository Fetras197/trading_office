import datetime as dt

from office import events
from office.board import Board

UTC = dt.timezone.utc


def mk(board, ts, title, assets, impact="High", until=None, kind="macro"):
    board.x("INSERT INTO events(key,ts,until,title,ccy,impact,kind,assets,src) VALUES(?,?,?,?,?,?,?,?,?)", (title + ts, ts, until, title, "USD", impact, kind, assets, "t"))


def test_blocked_window_60min_around_high_usd_event():
    b = Board()
    t0 = dt.datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
    mk(b, t0.isoformat(), "FOMC Statement", "all")
    f = lambda m: events.blocked(b, "BTC/USD", "crypto", t0 + dt.timedelta(minutes=m))  # noqa: E731
    assert f(-61) == "" and f(-59) == "решение ФРС по ставке" and f(0) and f(59) and f(61) == ""      # 60 минут до и 60 минут после


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
    assert events.title_ru({"kind": "macro", "title": "Something odd", "ccy": "EUR"}) == "Something odd (EUR)"
    b = Board()
    now = dt.datetime(2026, 10, 12, 6, 0, tzinfo=UTC)
    mk(b, "2026-10-12T12:30+00:00", "CPI", "all")
    mk(b, "2026-10-15T12:30+00:00", "PPI", "all")
    assert [e["title"] for e in events.upcoming(b, 24, now)] == ["CPI"]


def test_duplicates_collapsed_same_minute_same_label():
    b = Board()
    now = dt.datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
    for t, imp in (("CPI m/m", "High"), ("Median CPI y/y", "High"), ("Trimmed CPI y/y", "High"), ("Common CPI y/y", "Medium")):
        b.x("INSERT INTO events(key,ts,title,ccy,impact,kind,assets,src) VALUES(?,?,?,?,?,?,?,?)", (t, "2026-10-19T12:30+00:00", t, "CAD", imp, "macro", "", "t"))
    for t in ("Flash Manufacturing PMI", "Flash Services PMI"):
        b.x("INSERT INTO events(key,ts,title,ccy,impact,kind,assets,src) VALUES(?,?,?,?,?,?,?,?)", (t, "2026-10-19T13:45+00:00", t, "USD", "Medium", "macro", "", "t"))
    ups = events.upcoming(b, 24, now)
    assert len(ups) == 3 and sum(1 for e in ups if e["ccy"] == "CAD") == 1 and next(e for e in ups if e["ccy"] == "CAD")["impact"] == "High"


def test_refresh_drops_moved_earnings_and_stale_rows(monkeypatch):
    b = Board()
    fut = lambda d: (dt.datetime.now(UTC) + dt.timedelta(days=d)).isoformat(timespec="minutes")  # noqa: E731
    mkev = lambda d: [{"ts": fut(d), "until": fut(d + 1), "title": "Отчёт компании MRNA", "ccy": "USD", "impact": "High", "kind": "earnings", "assets": "MRNA", "src": "yfinance"}]  # noqa: E731
    monkeypatch.setattr(events.sources, "faireconomy_events", lambda: [])
    monkeypatch.setattr(events.sources, "deribit_expiry", lambda: [])
    monkeypatch.setattr(events.sources, "earnings_events", lambda syms: mkev(3))
    events.refresh(b, ["MRNA"])
    monkeypatch.setattr(events.sources, "earnings_events", lambda syms: mkev(4))        # дату отчёта перенесли
    events.refresh(b, ["MRNA"])
    assert len(b.q("SELECT 1 FROM events WHERE assets='MRNA'")) == 1


def test_key_us_events_are_always_high_and_block_all_assets():
    from office import sources
    for title in ("CPI m/m", "Core CPI m/m", "Federal Funds Rate", "FOMC Press Conference", "Non-Farm Employment Change", "Unemployment Rate", "Core PCE Price Index m/m", "PPI m/m"):
        assert sources.KEY_US_EVENT.search(title), title
    assert not sources.KEY_US_EVENT.search("Unemployment Claims") and not sources.KEY_US_EVENT.search("Flash Services PMI")


def test_foreign_events_keep_their_name_and_currency():
    t = lambda title, ccy: events.title_ru({"kind": "macro", "title": title, "ccy": ccy})  # noqa: E731
    assert t("BOE Gov Bailey Speaks", "GBP") == "Банк Англии: выступление представителя центробанка (GBP)"
    assert t("Flash Services PMI", "GBP") == "деловая активность в услугах (PMI) (GBP)"
    assert t("CPI y/y", "GBP") == "инфляция (цены для потребителей) (GBP)" and t("Core CPI m/m", "USD") == "базовая инфляция (цены для потребителей)"
    assert t("Something odd", "GBP") == "Something odd (GBP)" and "событие (" not in t("Something odd", "GBP")


def test_calendar_titles_fully_russian():
    t = lambda title, ccy="USD": events.title_ru({"kind": "macro", "title": title, "ccy": ccy})  # noqa: E731
    assert t("CB Consumer Confidence") == "Индекс доверия потребителей (Conference Board)"
    assert t("ECB Press Conference", "EUR") == "ЕЦБ: пресс-конференция центробанка (EUR)"
    for title in ("Crude Oil Inventories", "Natural Gas Storage", "Advance GDP q/q", "Chicago PMI", "New Home Sales", "Durable Goods Orders m/m", "Personal Spending m/m", "Richmond Manufacturing Index"):
        assert not __import__("re").search("[A-Za-z]{3}", t(title)), t(title)
