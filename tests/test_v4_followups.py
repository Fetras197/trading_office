"""Правки 18.10: пыль в отчётах, срок запретов с датой, источники цифр, keepalive-журнал."""
import datetime as dt
import subprocess
import sys

import pytest

from office import research, sources, views
from office.alpaca import AlpacaClient
from office.board import Board
from office.journal import Journal


class _S:
    def __init__(self, positions):
        self.p = positions

    def request(self, method, url, **kw):
        body = {"equity": "98000", "cash": "98000", "last_equity": "98000"} if url.endswith("/account") else self.p
        return type("R", (), {"status_code": 200, "text": "x", "json": lambda s: body})()


def test_dust_positions_hidden_from_account_view():
    pos = [{"symbol": "ONDOUSD", "qty": "8.7e-07", "avg_entry_price": "0.5", "current_price": "0.5", "market_value": "0.00000044", "unrealized_pl": "0", "unrealized_plpc": "0"},
           {"symbol": "BTCUSD", "qty": "0.01", "avg_entry_price": "60000", "current_price": "61000", "market_value": "610", "unrealized_pl": "10", "unrealized_plpc": "0.016"}]
    v = AlpacaClient(key="k", secret="s", session=_S(pos)).account_view()
    assert [p["symbol"] for p in v["positions"]] == ["BTCUSD"]


def test_ban_time_shows_date_when_not_today(monkeypatch):
    monkeypatch.setattr(views.config, "TZ_NAME", "Europe/Minsk")
    now = dt.datetime(2026, 10, 18, 7, 21, tzinfo=dt.timezone.utc)           # 10:21 по Минску
    assert views.when_text("2026-10-18T15:00+00:00", now) == "18:00"
    assert views.when_text("2026-10-19T07:21+00:00", now) == "завтра 10:21"   # раньше читалось как «до 10:21» при обновлении в 10:21
    assert views.when_text("2026-10-21T07:21+00:00", now) == "21.10 10:21"


def test_ban_hours_must_be_at_least_one_and_until_in_future():
    now = dt.datetime(2026, 10, 18, 7, 21, tzinfo=dt.timezone.utc)
    got = research._avoid([{"asset": "FIL/USD", "reason": "ликвидации лонгов", "hours": 24}, {"asset": "AAVE/USD", "reason": "ликвидации лонгов", "hours": 0.01}], set(), now)
    assert [e["asset"] for e in got] == ["FIL/USD"] and got[0]["until"] == "2026-10-19T07:21+00:00"
    from office import events
    assert events.picture_bans({"avoid_assets": got}, now + dt.timedelta(hours=23, minutes=59)) and not events.picture_bans({"avoid_assets": got}, now + dt.timedelta(hours=24, minutes=1))


LEVELS = {"US10Y": {"last": 5.098, "chg_1d": 0.0018, "chg_5d": -0.0278, "chg_20d": 0.02, "src": "Yahoo Finance", "asof": "2026-10-16"},
          "VIX": {"last": 19.99, "chg_1d": 0.2145, "chg_5d": 0.347, "chg_20d": 0.3498, "src": "FRED", "asof": "2026-10-15"}, "DXY": {"error": "X"}}
D = {"levels": LEVELS, "fed": {"effr": 3.88, "target_from": 3.75, "target_to": 4.0, "date": "2026-10-15", "src": "NY Fed"},
     "inflation_labor": {"cpi_yoy": 0.0374, "cpi_period": "2026-09", "unemployment": 4.2, "unemployment_period": "2026-09", "src": "FRED"}, "fear_greed": {"value": 53, "prev": 55, "label": "Neutral", "src": "alternative.me"}}


def test_every_macro_figure_carries_source_and_date():
    figs = sources.figures(D)
    by = {f["label"]: f for f in figs}
    y = by["доходность 10-летних облигаций США, %"]
    assert y["value"] == 5.098 and y["src"] == "Yahoo Finance" and y["asof"] == "2026-10-16"
    assert by["VIX (индекс страха)"]["src"] == "FRED" and by["инфляция CPI за год, %"]["value"] == 3.74 and all(f["src"] for f in figs)
    t = sources.figures_text(figs)
    assert "доходность 10-летних облигаций США, %: 5.098 (Yahoo Finance, 2026-10-16)" in t and "ставка ФРС (EFFR), %: 3.88 (NY Fed, 2026-10-15)" in t


def test_invented_numbers_detected_real_ones_pass():
    figs = sources.figures(D)
    assert sources.unsourced_numbers("Доходность 10 лет 5,1% (Yahoo Finance), VIX 19,99, CPI 3,74%", figs) == []
    assert sources.unsourced_numbers("Доходности выросли до 5,8%, ставка 3,88", figs) == ["5,8%"]
    assert sources.unsourced_numbers("через 3 дня и 50-дневной средней", figs) == []                    # целые без % не проверяем
    assert sources.unsourced_numbers("прогноз 2,7% по календарю", figs, extra='{"forecast": "2.7%"}') == []   # число есть во входных данных


def test_macro_with_invented_number_is_retried_then_mechanical(monkeypatch):
    class R:
        n = 0

        def ask(self, role, payload, system=None):
            R.n += 1
            return {"regime": "risk_off", "confidence": 0.7, "summary": "Доходности 10 лет выросли до 7,77%, это давит на рынок", "key_risks": []}
    b = Board()
    monkeypatch.setattr(sources, "cached", lambda key, ttl, fn: {"levels": LEVELS, "fear_greed": D["fear_greed"], "effr": D["fed"], "bls": D["inflation_labor"], "polymarket": []}.get(key) or fn())
    r = research.Research(b, Journal(), None, R(), lambda: {"held": [], "candidates": []})
    monkeypatch.setattr(r, "macro_data", lambda: {**D, "prediction_markets": []})
    rid = r.run_macro()
    rep = b.get(rid)
    assert R.n == 2 and rep["data"].get("mechanical") and "7,77" not in rep["verdict"] and rep["data"]["figures"]


def test_picture_text_has_sources_line():
    pic = {"ts": "2026-10-18T07:00:00+00:00", "data": {"regime": "risk_off", "summary": "s", "data_sources": "VIX (индекс страха): 19.99 (FRED, 2026-10-15)"}}
    assert "Данные и источники: VIX (индекс страха): 19.99 (FRED, 2026-10-15)" in views.picture_text(pic)


def test_weekly_journal_script(tmp_path):
    st = tmp_path / "st"
    (st / "logs").mkdir(parents=True)
    j = Journal(st / "office.db")
    j.snap_equity(dt.datetime.now(dt.timezone.utc).isoformat(), 100_000.0)
    j.add_nogo(dt.datetime.now(dt.timezone.utc).isoformat(), "BTC/USD", "signal", "x", 0.2)
    j.db.close()
    out = tmp_path / "journal"
    subprocess.run([sys.executable, "scripts/weekly_journal.py", str(st), str(out)], check=True, capture_output=True)
    f = next(out.glob("*.md"))
    assert "100,000.00" in f.read_text() and "signal 1" in f.read_text()
