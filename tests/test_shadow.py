import datetime as dt

from office import shadow
from office.board import Board

T0 = dt.datetime(2026, 10, 12, 10, 0, tzinfo=dt.timezone.utc)


def mk(prices, equity=100_000.0):
    b = Board()
    clock = {"t": T0}
    s = shadow.Shadow(b, lambda a: prices[a], lambda: equity, lambda: clock["t"])
    return s, b, clock


def sig(b, role, asset, stance, conf, price, t):
    b.x("INSERT INTO reports(cycle_id,role,asset,ts,verdict,confidence,refs,stances,data) VALUES('c',?,?,?,?,?,'[]','{}',?)",
        (role, asset, t.isoformat(timespec="seconds"), stance, conf, f'{{"stance": "{stance}", "price": {price}}}'))


def test_hold_portfolios_track_price_with_costs_and_office_is_broker_equity():
    px = {"SPY": 500.0, "BTC/USD": 100_000.0}
    s, b, clk = mk(px)
    s.step()
    px["SPY"], px["BTC/USD"] = 505.0, 90_000.0
    clk["t"] = T0 + dt.timedelta(hours=1)
    s.step()
    r = {x["name"]: x for x in s.summary()}
    assert abs(r["spy"]["ret"] - (505 / 500 * (1 - 0.0005) - 1)) < 1e-9          # проскальзывание на входе учтено
    assert r["btc"]["ret"] < -0.10 and r["btc"]["max_dd"] < -0.10
    assert r["office"]["start"] == 100_000.0 and r["tech"]["ret"] == 0 and r["lab"]["ret"] == 0


def test_signal_portfolios_enter_on_buy_exit_when_signal_goes():
    px = {"SPY": 500.0, "BTC/USD": 100_000.0, "ONDO/USD": 1.0}
    s, b, clk = mk(px)
    s.step()
    sig(b, "market", "ONDO/USD", "buy", 0.8, 1.0, clk["t"])
    sig(b, "market", "SPY", "buy", 0.4, 500, clk["t"])        # уверенность < 0,5 — не берём
    clk["t"] += dt.timedelta(minutes=30)
    s.step()
    assert list(s._positions("tech")) == ["ONDO/USD"] and not s._positions("lab")
    px["ONDO/USD"] = 1.1
    clk["t"] += dt.timedelta(minutes=30)
    s.step()
    tech = {x["name"]: x for x in s.summary()}["tech"]
    assert 0.02 < tech["ret"] < 0.03                                      # 25% капитала * +10% минус издержки
    sig(b, "market", "ONDO/USD", "hold", 0.6, 1.1, clk["t"] + dt.timedelta(minutes=1))
    clk["t"] += dt.timedelta(hours=1)
    s.step()
    assert not s._positions("tech")


def test_stale_signal_ignored():
    s, b, clk = mk({"SPY": 500.0, "BTC/USD": 1.0, "X/USD": 1.0})
    sig(b, "market", "X/USD", "buy", 0.9, 1.0, T0 - dt.timedelta(hours=5))
    s.step()
    assert not s._positions("tech")


def test_office_baseline_is_start_equity_from_journal():
    b = Board()
    s = shadow.Shadow(b, lambda a: 100.0, lambda: 99_509.64, lambda: T0, start_fn=lambda: 100_000.0)
    s.step()
    r = {x["name"]: x for x in s.summary()}
    assert abs(r["office"]["ret"] - (99_509.64 / 100_000 - 1)) < 1e-12 and r["spy"]["start"] == 100_000.0
