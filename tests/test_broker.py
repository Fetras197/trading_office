import pytest

from office.broker import SimBroker
from office.config import Costs
from office.journal import Journal

KINDS = {"BTC": "crypto", "AAPL": "stock"}
COSTS = {"crypto": Costs(fee=0.0025, slippage=0.0005), "stock": Costs(fee=0.0, slippage=0.0005)}


def mk(cash=10_000):
    j = Journal()
    return SimBroker(j, KINDS, COSTS, cash), j


def test_buy_applies_slippage_and_fee():
    b, _ = mk()
    r = b.buy("BTC", 1, 100, 95, 110, "2026-01-01T00:00:00")
    assert r["price"] == pytest.approx(100.05) and r["fee"] == pytest.approx(100.05 * 0.0025)
    assert b.cash == pytest.approx(10_000 - 100.05 - r["fee"])


def test_roundtrip_pnl_matches_cash_and_journal():
    b, j = mk()
    b.buy("BTC", 10, 100, 95, 120, "t0")
    r = b.sell("BTC", 110, "t1")
    t = j.q("SELECT * FROM trades")[0]
    assert t["pnl"] == pytest.approx(r["pnl"])
    assert b.cash == pytest.approx(10_000 + t["pnl"])  # без позиций cash = старт + pnl
    assert not j.positions()


def test_stop_beats_take_when_both_touched():
    b, j = mk()
    b.buy("BTC", 1, 100, 95, 105, "t0")
    ev = b.check_exits("BTC", high=106, low=94, ts="t1")
    assert ev["reason"] == "stop"


def test_take_fills_at_limit_without_slippage():
    b, _ = mk()
    b.buy("AAPL", 10, 100, 95, 105, "t0")
    ev = b.check_exits("AAPL", high=106, low=99, ts="t1")
    assert ev["reason"] == "take" and ev["price"] == 105


def test_no_double_position_and_cash_check():
    b, _ = mk(1_000)
    b.buy("BTC", 1, 100, 95, 110, "t")
    with pytest.raises(ValueError):
        b.buy("BTC", 1, 100, 95, 110, "t")
    with pytest.raises(ValueError):
        b.buy("AAPL", 100, 100, 95, 110, "t")


def test_sell_without_position():
    b, _ = mk()
    with pytest.raises(ValueError):
        b.sell("BTC", 100, "t")


def test_equity_marks_open_positions():
    b, _ = mk()
    b.buy("AAPL", 10, 100, 95, 110, "t")
    assert b.equity({"AAPL": 120}) == pytest.approx(b.cash + 1200)
