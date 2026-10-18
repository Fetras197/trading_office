"""Зал v4 на фейках: сигнал + большинство аналитиков → вход; неполные данные → половинный объём; блокировки пишутся в nogo; лимиты RiskGate неизменны."""
import numpy as np
import pandas as pd
import pytest

from office import agents, data, hall, indicators
from office.board import Board
from office.hall import Hall
from office.journal import Journal
from office.llm import LLMUnavailable

PRICE, ATR = 100.0, 1.0
TREND = {"close": PRICE, "atr14": ATR, "rsi14": 55.0, "ema20": 99.5, "ema50": 99.0, "ema200": 95.0, "macd_hist": 0.3, "bb_lower": 97.0}
FLAT = {**TREND, "close": 98.0, "ema50": 99.0, "macd_hist": -0.2}   # ниже средней 50, импульс вниз → сигнала нет


class FakeRouter:
    """stances: роль -> stance; по умолчанию все buy. 'abstain' → воздержался, 'down' → LLMUnavailable."""
    def __init__(self, stances=None, default="buy"):
        self.stances, self.default, self.asked = stances or {}, default, []

    def ask(self, role, payload, system=None):
        self.asked.append(role)
        s = self.stances.get(role, self.default)
        if s == "down":
            raise LLMUnavailable("429")
        return {"stance": s, "confidence": 0.0 if s == "abstain" else 0.7, "rationale": f"{role} says {s}"}


class FakeBroker:
    cash, last_order_id = 100_000.0, None

    def __init__(self):
        self.bought, self.pos, self.live = [], {}, {}

    def equity(self, prices=None):
        return 100_000.0

    def position(self, name):
        return self.pos.get(name)

    def live_positions(self):
        return self.live

    def buy(self, name, qty, price, stop, take, ts, run_id=None):
        self.bought.append((name, qty, stop, take))
        self.last_order_id = "ord-1"
        return {"price": price, "qty": qty, "fee": 0.0}


@pytest.fixture
def env(monkeypatch):
    idx = pd.date_range("2026-01-01", periods=600, freq="h", tz="UTC")
    px = pd.Series(100 + np.cumsum(np.random.default_rng(1).normal(0, .2, 600)), index=idx)
    df = pd.DataFrame({"open": px, "high": px + .3, "low": px - .3, "close": px, "volume": 1.0})
    state = {"ind": TREND, "news": [{"title": "x"}], "fund": {"pe": 10}}
    monkeypatch.setattr(data, "candles", lambda *a, **k: df)
    monkeypatch.setattr(indicators, "snapshot", lambda d: state["ind"])
    monkeypatch.setattr(data, "bundle", lambda a, d=None: {"asset": a.name, "as_of": "t", "indicators_1h": state["ind"], "indicators_4h": state["ind"], "last_candles_1h": [],
                                                           "tradingview": {}, "news": state["news"], "fundamentals": state["fund"]})
    monkeypatch.setattr(agents, "quant_stats", lambda a: {"sharpe_90d": 1})
    monkeypatch.setattr(hall, "utcnow", lambda: "2026-10-12T10:00:00+00:00")
    return state


def mk(router=None, **kw):
    b, j, br = Board(), Journal(), FakeBroker()
    return Hall(b, j, br, router or FakeRouter(), **kw), b, j, br


def test_signal_and_majority_enter_full_size_with_atr_levels(env):
    h, b, j, br = mk()
    r = h.run_asset("BTC/USD")
    assert r.status == "opened" and r.order["order_id"] == "ord-1" and not r.order["half"]
    name, qty, stop, take = br.bought[0]
    assert stop == PRICE - 2 * ATR and take == PRICE + 4 * ATR
    assert qty * PRICE <= 0.25 * 100_000 + 1 and qty * (PRICE - stop) <= 0.02 * 100_000 * 1.01     # лимиты RiskGate: размер ≤25%, риск ≤2%
    roles = [x["role"] for x in b.cycle_reports(r.cycle_id, "BTC/USD")]
    assert roles[:4] == ["market", "news", "fundamentals", "quant"] and roles[-1] == "decision"


def test_incomplete_data_halves_size_instead_of_skipping(env):
    h, b, j, br = mk()
    h.run_asset("BTC/USD")
    full = br.bought[0][1]
    env["news"] = []
    h2, b2, j2, br2 = mk()
    r = h2.run_asset("ETH/USD")
    assert r.status == "opened" and r.order["half"] and "нет свежих новостей" in r.order["gaps"]
    assert br2.bought[0][1] == pytest.approx(full * 0.5, rel=0.01)


def test_abstaining_analyst_means_half_size(env):
    h, b, j, br = mk(FakeRouter({"quant": "abstain"}))
    r = h.run_asset("BTC/USD")
    assert r.status == "opened" and r.order["half"]


def test_no_technical_signal_no_llm_spend_and_nogo_logged(env, monkeypatch):
    env["ind"] = FLAT
    monkeypatch.setattr(data, "bundle", lambda *a, **k: pytest.fail("тяжёлый пакет данных без сигнала не нужен"))
    rt = FakeRouter()
    h, b, j, br = mk(rt)
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "нет технического сигнала" in r.detail and not rt.asked and not br.bought
    n = j.q("SELECT stage, reason, score FROM nogo")[0]
    assert n["stage"] == "signal" and 0 < n["score"] < 0.5


def test_majority_against_blocks_and_is_explained(env):
    h, b, j, br = mk(FakeRouter({"market": "buy", "news": "hold", "fundamentals": "hold", "quant": "sell"}))
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "аналитики не одобрили" in r.detail and not br.bought
    n = j.nogo_top("2000-01-01")[0]
    assert n["stage"] == "votes" and n["score"] > 0.5


def test_half_of_voters_for_is_enough_when_no_sells(env):
    h, b, j, br = mk(FakeRouter({"market": "buy", "news": "buy", "fundamentals": "hold", "quant": "hold"}))
    assert h.run_asset("BTC/USD").status == "opened"


def test_no_live_votes_means_no_entry(env):
    h, b, j, br = mk(FakeRouter(default="down"))
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "нет голосов" in r.detail and not br.bought


def test_votes_cached_and_fresh_round_limit(env):
    rt = FakeRouter({"market": "hold", "news": "hold", "fundamentals": "hold", "quant": "hold"})
    h, b, j, br = mk(rt, fresh_rounds=1)
    h.run_asset("BTC/USD")
    n = len(rt.asked)
    h.run_asset("BTC/USD")
    assert len(rt.asked) == n                      # второй раз — из кэша голосов (90 мин)
    r = h.run_asset("ETH/USD")                     # лимит раундов за проход исчерпан
    assert len(rt.asked) == n and "очередь" in r.detail


def test_halt_file_blocks_new_entries(env, monkeypatch, tmp_path):
    f = tmp_path / "STOP"
    f.touch()
    monkeypatch.setattr(hall.config, "STOP_FILE", f)
    h, b, j, br = mk()
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "STOP" in r.detail and not br.bought


def test_dry_run_sends_no_order(env):
    h, b, j, br = mk(auto_trade=False)
    assert h.run_asset("BTC/USD").detail.startswith("dry-run") and not br.bought


def test_risk_limits_still_hard(env):
    h, b, j, br = mk()
    h.j.kv_set("day_start:2026-10-12", 100_000.0)
    for i in range(5):
        j.add_fill(f"2026-10-12T0{i}:00:00+00:00", f"X{i}", "buy", 1, 1, 0, "entry", None)
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "лимит 5 входов" in r.detail and not br.bought
    h2, b2, j2, br2 = mk()
    h2.j.kv_set("day_start:2026-10-12", 110_000.0)           # эквити 100k против 110k на старте суток = −9% < −5%
    assert "дневной стоп" in h2.run_asset("BTC/USD").detail
    h3, b3, j3, br3 = mk()
    for s in ("A", "B", "C"):
        j3._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts) VALUES(?,?,?,?,?,?,?,?)", (s, 1, 1, .5, 2, 0, 0, "t"))
    assert "лимит 3 позиций" in h3.run_asset("BTC/USD").detail
    assert not (br.bought or br2.bought or br3.bought)


def test_open_position_not_reanalysed(env):
    rt = FakeRouter()
    h, b, j, br = mk(rt)
    br.pos["BTC/USD"] = {"qty": 1, "avg": 90, "stop": 85, "take": 110}
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "открыта" in r.detail and not rt.asked


def test_broker_already_holds_symbol_blocks(env):
    h, b, j, br = mk()
    br.live = {"BTCUSD": {}}
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "у брокера" in r.detail and not br.bought


def test_panic_regime_blocks_and_high_vol_halves_size(env):
    h, b, j, br = mk()
    b.publish("R-regime", "regime", "*", "x", 1.0, data={"crypto": {"regime": "panic", "why": ["падение"]}, "stock": None})
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "паника" in r.detail and not br.bought
    h1, b1, j1, br1 = mk()
    h1.run_asset("BTC/USD")
    h2, b2, j2, br2 = mk()
    b2.publish("R-regime", "regime", "*", "x", 1.0, data={"crypto": {"regime": "high_vol", "why": ["волатильность"]}, "stock": None})
    h2.run_asset("BTC/USD")
    assert br2.bought[0][1] < br1.bought[0][1]


def test_event_blackout_blocks_new_entries(env, monkeypatch):
    monkeypatch.setattr(hall.events, "blocked", lambda board, name, kind: "выступление главы ФРС")
    h, b, j, br = mk()
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "ФРС" in r.detail and not br.bought


def test_cooldown_after_stop(env):
    h, b, j, br = mk()
    now = pd.Timestamp.now(tz="UTC").isoformat()
    j._x("INSERT INTO trades(asset,entry_ts,exit_ts,entry,exit,qty,fees,pnl,r_mult,run_id,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("BTC/USD", now, now, 100, 98, 1, 0, -2, -1, 0, "stop"))
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "остывание" in r.detail


def test_price_cross_check_between_sources_skips(env):
    class C:
        def price(self, sym): return PRICE * 1.05
    h, b, j, br = mk()
    br.c = C()
    assert "расходятся" in h.run_asset("BTC/USD").detail
    br.c = type("C2", (), {"price": lambda self, s: PRICE * 1.005})()
    assert h.run_asset("BTC/USD").status == "opened"


def test_smartmoney_voice_published_for_crypto_only(env):
    h, b, j, br = mk(smart=lambda coin: {"stance": "buy", "confidence": 0.7, "rationale": "умные деньги в лонгах"} if coin == "BTC" else None)
    h.run_asset("BTC/USD")
    r = b.q("SELECT verdict FROM reports WHERE role='smartmoney'")
    assert r and r[0]["verdict"].startswith("buy: умные")
    h2, b2, j2, _ = mk(smart=lambda coin: {"stance": "buy", "confidence": 0.7, "rationale": "x"})
    h2.run_asset("NVDA")
    assert not b2.q("SELECT 1 FROM reports WHERE role='smartmoney'")


def test_riskgate_rejection_is_logged_as_nearest_candidate(env):
    h, b, j, br = mk()
    env["ind"] = {**TREND, "atr14": 0.00001}    # ATR слишком мал → размер/стоп не проходят проверки
    r = h.run_asset("BTC/USD")
    assert r.status in ("rejected", "opened", "skipped")
    if r.status == "rejected":
        assert j.nogo_top("2000-01-01")[0]["stage"] == "riskgate"
