import datetime as dt
import json

import numpy as np
import pandas as pd

from office import lab, labtrial
from office.board import Board

UTC = dt.timezone.utc


def series(n, seed=0, drift=0.0004, vol=0.004):
    r = np.random.default_rng(seed).normal(drift, vol, n)
    c = 100 * np.cumprod(1 + r)
    idx = pd.date_range("2026-06-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame({"open": c, "high": c * 1.002, "low": c * 0.998, "close": c, "volume": 1.0}, index=idx)


def test_p_value_and_multitest_bh_blocks_lucky_winners():
    rng = np.random.default_rng(0)
    idx = pd.date_range("2026-01-01", periods=180, freq="D", tz="UTC")
    strong = pd.Series(rng.normal(0.01, 0.01, 180), index=idx)
    noise = pd.Series(rng.normal(0.0, 0.01, 180), index=idx)
    assert lab.p_value(strong) < 1e-6 and 0.01 < lab.p_value(noise) < 0.99 and lab.p_value(strong.iloc[:10]) == 1.0
    b = Board()
    # 200 стратегий со случайными p (равномерно 0..1; при перебое часть из них «лучше buy&hold» — все approved по критериям) + одна действительно сильная
    for i in range(200):
        b.x("INSERT INTO strategies(ts,symbol,name,params,approved,oos_sharpe,bh_sharpe,tf,kind,p) VALUES(datetime('now'),?, 'macd','{}',1,1,0.5,'1h','stock',?)", (f"S{i}", (i + 1) / 200))
    b.x("INSERT INTO strategies(ts,symbol,name,params,approved,oos_sharpe,bh_sharpe,tf,kind,p) VALUES(datetime('now'),'REAL','macd','{}',1,2,0.5,'1h','stock',1e-6)")
    assert lab.multitest(b) == 1
    assert b.q("SELECT symbol FROM strategies WHERE mt=1") == [{"symbol": "REAL"}]
    # старые строки без p-значения поправку не проходят
    b.x("INSERT INTO strategies(ts,symbol,name,params,approved,oos_sharpe,bh_sharpe,tf,kind) VALUES(datetime('now'),'OLD','macd','{}',1,2,0.5,'1h','stock')")
    lab.multitest(b)
    assert not b.q("SELECT 1 FROM strategies WHERE symbol='OLD' AND mt=1")


def test_forward_and_verdict_rules():
    df = series(600, seed=3, drift=0.001)
    _, _, ps = lab.grid(df["close"], df["high"], df["low"], "roc_momentum")
    r = labtrial.forward(df, "roc_momentum", {**ps[0], "sl": 0.05}, df.index[300], 0.0005)
    assert r["bars"] > 200 and -1 < r["ret"] < 5 and r["dd"] <= 0 and "bh_ret" in r
    assert labtrial.verdict({"trades": 2, "ret": 0.1, "dd": -0.01, "bh_dd": -0.05})[0] is False        # мало сделок
    assert labtrial.verdict({"trades": 5, "ret": -0.01, "dd": -0.01, "bh_dd": -0.05})[0] is False       # убыток
    assert labtrial.verdict({"trades": 5, "ret": 0.02, "dd": -0.08, "bh_dd": -0.05})[0] is False        # просадка хуже удержания
    assert labtrial.verdict({"trades": 5, "ret": 0.02, "dd": -0.03, "bh_dd": -0.05}) == (True, "подтверждена вживую")


def test_trial_lifecycle_voice_only_after_live(monkeypatch):
    b = Board()
    df = series(1500, seed=2)
    _, _, ps = lab.grid(df["close"], df["high"], df["low"], "sma_cross")
    b.x("INSERT INTO strategies(ts,symbol,name,params,approved,oos_sharpe,bh_sharpe,tf,kind,p,data) VALUES(datetime('now'),'X','sma_cross',?,1,1.5,0.5,'1h','stock',1e-9,'{}')", (json.dumps(ps[0]),))
    monkeypatch.setattr(lab, "history", lambda s, tf="1h", d=None: df)
    monkeypatch.setattr(lab, "symbol_regime", lambda s: None)
    t0 = dt.datetime(2026, 8, 1, tzinfo=UTC)
    assert labtrial.promote(b, now=t0)["new"] == 1
    assert lab.signal_for(b, "X") is None                                  # на испытании голоса в зале нет
    assert lab.signal_for(b, "X", include_trial=True) is not None          # а в теневом портфеле торгуется
    assert [x[0] for x in lab.shadow_signals(b)] in ([], ["X"])
    assert labtrial.promote(b, fetch=lambda *a: df, now=t0 + dt.timedelta(days=5))["live"] == 0   # срок не вышел
    forced = {"ret": 0.03, "trades": 4, "dd": -0.01, "bh_ret": 0.0, "bh_dd": -0.04, "bars": 100}
    monkeypatch.setattr(labtrial, "forward", lambda *a, **k: forced)
    assert labtrial.promote(b, fetch=lambda *a: df, now=t0 + dt.timedelta(days=15))["live"] == 1
    assert lab.signal_for(b, "X") is not None and lab.live_symbols(b) == {"X"}   # голос получен после подтверждения
    s = lab.stats(b)
    assert s["live"] == 1 and s["trial"] == 0


def test_failed_trial_can_retry_only_after_30_days(monkeypatch):
    b = Board()
    b.x("INSERT INTO strategies(ts,symbol,name,params,approved,oos_sharpe,bh_sharpe,tf,kind,p) VALUES(datetime('now'),'X','sma_cross','{}',1,1.5,0.5,'1h','stock',1e-9)")
    t0 = dt.datetime(2026, 8, 1, tzinfo=UTC)
    labtrial.promote(b, now=t0)
    monkeypatch.setattr(labtrial, "forward", lambda *a, **k: {"ret": -0.02, "trades": 5, "dd": -0.05, "bh_ret": 0, "bh_dd": -0.04, "bars": 9})
    assert labtrial.promote(b, fetch=lambda *a: series(100), now=t0 + dt.timedelta(days=15))["failed"] == 1
    assert labtrial.promote(b, now=t0 + dt.timedelta(days=20))["new"] == 0
    b.x("UPDATE strategies SET ts=datetime('now')")
    assert labtrial.promote(b, now=t0 + dt.timedelta(days=50))["new"] == 1
