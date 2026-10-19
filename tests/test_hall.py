"""Зал v4 на фейках: сигнал + большинство аналитиков → вход; неполные данные → половинный объём; блокировки пишутся в nogo; лимиты RiskGate неизменны."""
import numpy as np
import pandas as pd
import pytest

from office import agents, data, desks, entry, hall, indicators
from office.board import Board
from office.config import COSTS
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

    def buy(self, name, qty, price, stop, take, ts, run_id=None, desk=None):
        self.bought.append((name, qty, stop, take))
        self.desks = getattr(self, "desks", []) + [desk]
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
    desks.seed(j, "2026-10-12T00:00:00+00:00")
    return Hall(b, j, br, router or FakeRouter(), **kw), b, j, br


def test_signal_and_majority_enter_full_size_with_atr_levels(env):
    h, b, j, br = mk()
    r = h.run_asset("BTC/USD")
    assert r.status == "opened" and r.order["order_id"] == "ord-1" and not r.order["half"]
    name, qty, stop, take = br.bought[0]
    assert stop == pytest.approx(PRICE - 3.8 * ATR) and take >= PRICE + 7.6 * ATR     # крипта: широкие уровни, тейк подобран экономикой
    pl = entry.trade_plan(PRICE, ATR, "trend", "crypto", COSTS["crypto"])
    assert take == pytest.approx(pl.take) and pl.reward_unit >= 2 * pl.risk_unit and (pl.take - PRICE) >= 4 * pl.fees_unit      # цель с комиссиями ≥ 2× риска и ≥ 4× комиссии
    assert qty * PRICE <= 0.5 * 0.20 * 100_000 + 1 and qty * (PRICE - stop) <= 0.02 * 100_000 * 1.01     # потолок позиции = половина бюджета отдела (10% счёта), риск ≤2%
    assert br.desks == ["majors"]                                                        # BTC → «Крупная крипта»
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


def ban(board, asset, reason="крупные ликвидации лонгов", hours=6):
    import datetime as dt
    until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)).isoformat(timespec="minutes")
    board.publish("R-pic", "picture", "*", "neutral: тест", 0.8, data={"regime": "neutral", "summary": "т", "avoid_assets": [{"asset": asset, "reason": reason, "until": until}]})


def test_council_against_but_signal_enters_half_size_tech_only(env):
    h, b, j, br = mk(FakeRouter({"market": "buy", "news": "hold", "fundamentals": "hold", "quant": "sell"}))
    r = h.run_asset("BTC/USD")
    assert r.status == "opened" and r.order["mode"] == "tech_only" and r.order["half"] and "аналитики не одобрили" in r.order["gaps"][0]
    h2, b2, j2, br2 = mk()
    h2.run_asset("BTC/USD")
    assert br.bought[0][1] == pytest.approx(br2.bought[0][1] * 0.5, rel=0.01)
    assert b.q("SELECT data FROM reports WHERE role='decision'")


def test_concrete_picture_ban_blocks_tech_only_but_not_council(env):
    h, b, j, br = mk(FakeRouter({"market": "hold", "news": "hold", "fundamentals": "hold", "quant": "hold"}))
    ban(b, "BTC/USD")
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "запрет совета" in r.detail and "ликвидации" in r.detail and not br.bought
    n = j.nogo_top("2000-01-01")[0]
    assert n["stage"] == "ban" and n["score"] == 0.9
    h2, b2, j2, br2 = mk()                          # аналитики «за» — полноценный вход, запрет совета не мешает одобренному входу
    ban(b2, "BTC/USD")
    assert h2.run_asset("BTC/USD").status == "opened"
    h3, b3, j3, br3 = mk(FakeRouter({"market": "hold", "news": "hold", "fundamentals": "hold", "quant": "hold"}))
    ban(b3, "BTC/USD", hours=-1)                    # запрет истёк — не действует
    assert h3.run_asset("BTC/USD").status == "opened"
    h4, b4, j4, br4 = mk(FakeRouter({"market": "hold", "news": "hold", "fundamentals": "hold", "quant": "hold"}))
    ban(b4, "ETH/USD")                              # запрет по другому активу
    assert h4.run_asset("BTC/USD").status == "opened"


def test_tech_only_switch_restores_strict_council(env, monkeypatch):
    monkeypatch.setattr(entry, "TECH_ONLY", False)
    h, b, j, br = mk(FakeRouter({"market": "hold", "news": "hold", "fundamentals": "hold", "quant": "sell"}))
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "аналитики не одобрили" in r.detail and not br.bought


def test_half_of_voters_for_is_enough_when_no_sells(env):
    h, b, j, br = mk(FakeRouter({"market": "buy", "news": "buy", "fundamentals": "hold", "quant": "hold"}))
    assert h.run_asset("BTC/USD").status == "opened"


def test_no_live_votes_enters_half_size_without_ban_and_blocked_with_ban(env):
    h, b, j, br = mk(FakeRouter(default="down"))
    r = h.run_asset("BTC/USD")
    assert r.status == "opened" and r.order["half"] and "нет голосов аналитиков" in r.order["gaps"][0]
    h2, b2, j2, br2 = mk(FakeRouter(default="down"))
    ban(b2, "BTC/USD")
    assert h2.run_asset("BTC/USD").status == "hold"


def test_votes_cached_and_fresh_round_limit(env, monkeypatch):
    monkeypatch.setattr(entry, "TECH_ONLY", False)
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


def test_risk_limits_still_hard_and_now_per_desk(env):
    h, b, j, br = mk()
    h.j.kv_set("day_start:2026-10-12", 100_000.0)
    for i in range(5):                                                                     # 5 входов за сутки в обоих подходящих отделах → вход невозможен
        for d in ("majors", "trend"):
            j.add_fill(f"2026-10-12T0{i}:00:00+00:00", f"X{i}/USD", "buy", 1, 1, 0, "entry", None, d)
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "лимит 5 входов в сутки отдела" in r.detail and not br.bought
    h1, b1, j1, br1 = mk()                                                                 # а пять входов в ЧУЖОМ отделе не мешают
    for i in range(5):
        j1.add_fill(f"2026-10-12T0{i}:00:00+00:00", f"X{i}/USD", "buy", 1, 1, 0, "entry", None, "trend")
    assert h1.run_asset("BTC/USD").status == "opened"
    h2, b2, j2, br2 = mk()
    h2.j.kv_set("day_start:2026-10-12", 110_000.0)                                         # дневной стоп −5% — по счёту, для всех отделов сразу
    assert "дневной стоп" in h2.run_asset("BTC/USD").detail
    h3, b3, j3, br3 = mk()
    for s_ in ("A/USD", "B/USD", "C/USD"):
        j3._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts,desk) VALUES(?,?,?,?,?,?,?,?,?)", (s_, 1, 1, .5, 2, 0, 0, "t", "majors"))
    r3 = h3.run_asset("BTC/USD")                                                           # «Крупная крипта» полна → сигнал уходит в «Тренд» (перелив)
    assert r3.status == "opened" and br3.desks == ["trend"]
    assert not (br.bought or br2.bought)


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


def test_economics_rejection_logged_when_costs_eat_the_target(env):
    h, b, j, br = mk()
    env["ind"] = {**TREND, "atr14": 0.00001}    # ATR ничтожно мал относительно цены → цель с комиссиями не окупает риск, тейк дальше 16 ATR не ищем
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "экономика сделки не сходится" in r.detail and not br.bought
    assert j.nogo_top("2000-01-01")[0]["stage"] == "economics"


def test_wide_spread_blocks_entry_and_real_spread_counts_as_slippage(env):
    class C:
        def __init__(self, sp): self.sp = sp
        def price(self, s): return PRICE
        def spread(self, s): return self.sp
    h, b, j, br = mk()
    br.c = C(0.027)                                   # BONK 18.10: круг стоил 2,7%
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "спред 2.70% шире" in r.detail and not br.bought and j.nogo_top("2000-01-01")[0]["stage"] == "spread"
    h2, b2, j2, br2 = mk()
    br2.c = C(0.0)
    h2.run_asset("BTC/USD")
    h3, b3, j3, br3 = mk()
    br3.c = C(0.0024)                                  # допустимый, но заметный спред: проскальзывание в расчёте = 0,12% (> 0,05%) → тейк дальше, размер меньше
    r3 = h3.run_asset("BTC/USD")
    assert r3.status == "opened" and br3.bought[0][3] > br2.bought[0][3] and br3.bought[0][1] <= br2.bought[0][1]


def test_alt_cap_two_open_positions_across_all_desks_btc_eth_exempt(env):
    def with_alts(*names):
        h, b, j, br = mk()
        for n, d in zip(names, ("trend", "reversal", "majors")):
            j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts,desk) VALUES(?,?,?,?,?,?,?,?,?)", (n, 1, 1, .5, 2, 0, 0, "t", d))
        return h, b, j, br
    h, b, j, br = with_alts("RENDER/USD", "ARB/USD")                                   # 2 альта в РАЗНЫХ отделах
    r = h.run_asset("SOL/USD")
    assert r.status == "hold" and "общий лимит на все отделы — 2" in r.detail and not br.bought and j.nogo_top("2000-01-01")[0]["stage"] == "alts"
    assert h.run_asset("BTC/USD").status == "opened"                                    # BTC и ETH альткоинами не считаются
    h2, b2, j2, br2 = with_alts("RENDER/USD", "ARB/USD")
    assert h2.run_asset("ETH/USD").status == "opened"
    h3, b3, j3, br3 = with_alts("RENDER/USD", "BTC/USD", "ETH/USD")                     # BTC/ETH в позициях лимит не занимают
    assert h3.run_asset("SOL/USD").status == "opened"
    h4, b4, j4, br4 = with_alts("RENDER/USD", "ARB/USD", "AAVE/USD")                    # уже больше лимита: новые альты нельзя, старые остаются
    assert h4.run_asset("DOT/USD").status == "hold" and len(j4.positions()) == 3


def test_alt_cap_rechecked_under_lock_when_parallel_thread_opened_one(env):
    h, b, j, br = mk()
    j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts,desk) VALUES('RENDER/USD',1,1,.5,2,0,0,'t','trend')")
    orig = h._votes

    def votes_then_other_thread_opens(asset, bundle, now=None):
        j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts,desk) VALUES('ARB/USD',1,1,.5,2,0,0,'t','reversal')")   # пока шли аналитики, открылась вторая
        return orig(asset, bundle, now)
    h._votes = votes_then_other_thread_opens
    r = h.run_asset("SOL/USD")
    assert r.status == "hold" and "общий лимит 2" in r.detail and not br.bought


def test_pair_without_valid_protection_is_not_bought_and_logged(env):
    from office.alpaca import AlpacaError
    h, b, j, br = mk()

    def bad(name, qty, price, stop):
        raise AlpacaError("шаг цены 0.0001 слишком грубый для цены 1.18e-05")
    br.validate_entry = bad
    r = h.run_asset("BTC/USD")
    assert r.status == "skipped" and "не покупаем" in r.detail and not br.bought
    assert j.nogo_top("2000-01-01")[0]["stage"] == "protection"
