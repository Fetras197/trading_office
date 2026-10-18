import importlib.util

import numpy as np
import pandas as pd
import pytest

from office import lab, screener
from office.board import Board


def series(n=3000, seed=0, drift=0.0):
    idx = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    px = 100 * np.exp(np.cumsum(np.random.default_rng(seed).normal(drift, 0.004, n)))
    return pd.DataFrame({"open": px, "high": px * 1.002, "low": px * .998, "close": px, "volume": 1000.0}, index=idx)


def test_signals_are_causal():
    df = series(800)
    for fam in lab.FAMILIES:
        e1, x1, _ = lab.grid(df["close"], df["high"], df["low"], fam)
        e2, x2, _ = lab.grid(df["close"][:600], df["high"][:600], df["low"][:600], fam)
        assert e1.iloc[:600].equals(e2) and x1.iloc[:600].equals(x2), fam  # будущее не меняет прошлые сигналы


def test_approval_rule_all_conditions():
    ok = {"oos_sharpe": 1.0, "bh_sharpe": 0.5, "oos_dd": -0.1, "bh_dd": -0.3, "trades": 30, "oos_return": 0.1}
    assert lab.is_approved(ok)
    assert not lab.is_approved({**ok, "trades": 29})
    assert not lab.is_approved({**ok, "oos_sharpe": 0.4})
    assert not lab.is_approved({**ok, "oos_dd": -0.4})
    assert not lab.is_approved({**ok, "oos_return": -0.01})


@pytest.mark.skipif(importlib.util.find_spec("vectorbt") is None, reason="vectorbt нужен только лаборатории (телефон), в облаке не ставится")
def test_walk_forward_runs_and_random_walk_rarely_approved():
    r = lab.walk_forward(series(9500, 1), "sma_cross", 0.003)
    assert r and r["folds"] >= 3 and set(r) >= {"oos_sharpe", "bh_sharpe", "oos_dd", "bh_dd", "trades", "params", "approved"}


def test_walk_forward_too_short_returns_none():
    assert lab.walk_forward(series(500), "macd", 0.003) is None


def test_approved_strategies_vote_as_lab_analyst(monkeypatch):
    b = Board()
    assert lab.signal_for(b, "X") is None  # нет approved -> лаборатория не голосует
    df = series(1500, 3)
    _, _, ps = lab.grid(df["close"], df["high"], df["low"], "sma_cross")
    b.x("INSERT INTO strategies(ts,symbol,name,params,approved,oos_sharpe,bh_sharpe,tf,mt,p) VALUES(datetime('now'),'X','sma_cross',?,1,1.5,0.5,'1h',1,1e-9)", (__import__("json").dumps(ps[0]),))
    assert lab.signal_for(b, "X") is None  # approved без испытательного срока голоса не имеет
    b.x("INSERT INTO lab_trial(key,symbol,name,tf,params,start,status,updated) VALUES('X|sma_cross|1h','X','sma_cross','1h','{}','2026-01-01','live','2026-01-20')")
    monkeypatch.setattr(lab, "symbol_regime", lambda s: None)
    monkeypatch.setattr(lab, "history", lambda s, tf="1h", d=None: df)
    sg = lab.signal_for(b, "X")
    assert sg["stance"] in ("buy", "hold") and 0 <= sg["confidence"] <= 1


def test_screener_metrics_and_score():
    up = series(100, 5, drift=0.01)
    m = screener.metrics(up)
    assert m["roc20"] > 0 and m["breakout"] in (0, 1)
    flat = {"roc20": 0, "vol_ratio": 1, "breakout": 0, "rsi14": 50}
    assert screener.score({**flat, "roc20": 20, "breakout": 1, "vol_ratio": 2}) > screener.score(flat) > screener.score({**flat, "breakout": -1})
    assert screener.score(flat, lab_long=True) > screener.score(flat)
    assert screener.metrics(series(10)) is None


def test_universe_excludes_ondo_and_stablecoins(tmp_path, monkeypatch):
    import json
    c = tmp_path / "universe.json"
    c.write_text(json.dumps({"crypto": ["BTC/USD", "ONDO/USD", "USDC/USD", "USDT/USD", "USDG/USD", "ETH/USD"], "stock": ["AAPL"]}))
    monkeypatch.setattr(screener, "CACHE", c)
    assert screener.universe(None)["crypto"] == ["BTC/USD", "ETH/USD"]


def test_turnover_filter_crypto_by_market_volume_stock_by_bars():
    cv = {"BTC": 30e9, "WIF": 2e6, "ETH": 15e9}
    assert screener.liquid("BTC/USD", None, cv) and not screener.liquid("WIF/USD", None, cv) and not screener.liquid("NEW/USD", None, cv)
    assert screener.liquid("BTC/USD", None, {}) and not screener.liquid("DOGE/USD", None, {})   # нет данных об обороте: только ядро
    idx = pd.date_range("2026-01-01", periods=30, freq="D", tz="UTC")
    rich = pd.DataFrame({"close": 100.0, "volume": 1e6}, index=idx)      # 100 млн $/день
    thin = pd.DataFrame({"close": 10.0, "volume": 1e4}, index=idx)       # 0,1 млн $/день
    assert screener.liquid("AAPL", rich, {}) and not screener.liquid("XYZ", thin, {}) and not screener.liquid("NEW", rich.head(5), {})


def test_scan_skips_excluded_and_illiquid(monkeypatch):
    idx = pd.date_range("2026-01-01", periods=100, freq="D", tz="UTC")
    up = pd.Series(np.linspace(100, 160, 100), index=idx)
    mk = lambda v: pd.DataFrame({"open": up, "high": up * 1.01, "low": up * .99, "close": up, "volume": v}, index=idx)  # noqa: E731
    bars = {"BTC/USD": mk(1.0), "ONDO/USD": mk(1.0), "USDC/USD": mk(1.0), "WIF/USD": mk(1.0), "AAPL": mk(1e6)}
    monkeypatch.setattr(screener, "universe", lambda c: {"crypto": ["BTC/USD", "ONDO/USD", "USDC/USD", "WIF/USD"], "stock": ["AAPL"]})
    monkeypatch.setattr(screener.data, "alpaca_bars", lambda syms, kind, days=100, **k: {s: bars[s] for s in syms if s in bars})
    monkeypatch.setattr(screener, "crypto_volumes", lambda: {"BTC": 1e10, "ONDO": 1e9, "USDC": 1e10, "WIF": 1e6})
    monkeypatch.setattr(screener.lab, "live_symbols", lambda b: set())
    top = screener.scan(Board(), None, True, top_n=10)
    assert {r["symbol"] for r in top} == {"BTC/USD", "AAPL"}
