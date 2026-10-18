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
    assert r["office"]["start"] == 100_000.0 and r["tech2"]["ret"] == 0 and r["lab"]["ret"] == 0


import pandas as pd

from office.config import COSTS
from office.journal import Journal
from office.risk import RiskGate


def mk2(prices, bars=None, equity=100_000.0):
    b, j = Board(), Journal()
    clock = {"t": T0}
    s = shadow.Shadow(b, lambda a: prices[a], lambda: equity, lambda: clock["t"], journal=j, candles_fn=lambda a: bars[a])
    return s, b, j, clock


def candles(path):
    idx = pd.date_range(T0 + dt.timedelta(minutes=30), periods=len(path), freq="h", tz="UTC")
    return pd.DataFrame({"open": [p[0] for p in path], "high": [p[1] for p in path], "low": [p[2] for p in path], "close": [p[0] for p in path], "volume": 1.0}, index=idx)


def test_tech2_uses_same_stop_size_and_fees_as_office_and_loses_on_stop():
    px = {"SPY": 500.0, "BTC/USD": 82_000.0}
    bars = {"BTC/USD": candles([(82_000, 82_100, 81_900), (81_500, 81_600, 81_000)])}       # вторая свеча пробивает стоп 81 247
    s, b, j, clk = mk2(px, bars)
    s.step()                                                                                    # старт: сигналы до него не берём
    j.add_signal((T0 + dt.timedelta(minutes=30)).isoformat(), "BTC/USD", "trend", 82_125.8, 231.0, 81_247.5, 83_880.0)
    clk["t"] += dt.timedelta(hours=1)
    s.step()
    pos = s._positions("tech2")["BTC/USD"]
    gate = RiskGate().check_entry(kind="crypto", costs=COSTS["crypto"], entry=82_125.8, stop=81_247.5, atr=231.0, equity=100_000.0, day_start_equity=100_000.0,
                                  entries_today=0, open_positions=0, cash=100_000.0, has_position=False, size_mult=0.5)
    assert abs(pos["qty"] - gate.qty) < 1e-9 and pos["stop"] == 81_247.5                       # размер ровно как у офиса (RiskGate, ×0,5)
    clk["t"] += dt.timedelta(hours=2)
    s.step()
    assert not s._positions("tech2")
    t = b.q("SELECT * FROM shadow_trades")[0]
    planned = RiskGate.planned_risk(COSTS["crypto"], 82_125.8, 81_247.5, gate.qty)
    assert t["reason"] == "stop" and t["pnl"] < 0 and -t["pnl"] <= planned * 1.05            # убыток с комиссиями в пределах заложенного
    r = {x["name"]: x for x in s.summary()}["tech2"]
    assert r["ret"] < 0                                                                        # портфель в минусе, как и реальная сделка


def test_tech2_take_profit_and_limits_and_cooldown():
    px = {"SPY": 500.0, "BTC/USD": 100.0, "ETH/USD": 100.0, "SOL/USD": 100.0, "XRP/USD": 100.0}
    up = candles([(100, 104, 99.5)])
    bars = {k: up for k in px}
    s, b, j, clk = mk2(px, bars)
    s.step()
    for a in ("BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD"):
        j.add_signal((T0 + dt.timedelta(minutes=10)).isoformat(), a, "trend", 100.0, 1.0, 96.2, 103.0)
    clk["t"] += dt.timedelta(minutes=30)
    s.step()
    assert len(s._positions("tech2")) == 3                                                     # не больше 3 позиций, как у офиса
    clk["t"] += dt.timedelta(hours=2)
    s.step()
    tr = b.q("SELECT reason, pnl FROM shadow_trades")
    assert len(tr) == 3 and all(x["reason"] == "take" and x["pnl"] > 0 for x in tr)


def test_tech2_ignores_signals_from_before_its_start():
    px = {"SPY": 500.0, "BTC/USD": 100.0}
    s, b, j, clk = mk2(px, {"BTC/USD": candles([(100, 101, 99)])})
    j.add_signal(T0.isoformat(), "BTC/USD", "trend", 100.0, 1.0, 96.2, 103.0)
    s.step()
    clk["t"] += dt.timedelta(hours=1)
    s.step()
    assert not s._positions("tech2")


def test_office_baseline_is_start_equity_from_journal():
    b = Board()
    s = shadow.Shadow(b, lambda a: 100.0, lambda: 99_509.64, lambda: T0, start_fn=lambda: 100_000.0)
    s.step()
    r = {x["name"]: x for x in s.summary()}
    assert abs(r["office"]["ret"] - (99_509.64 / 100_000 - 1)) < 1e-12 and r["spy"]["start"] == 100_000.0
