"""Корпорация: отделы, маршрутизация, лимиты на отдел, экономика крипты, совет директоров, сканер, отчёты."""
import numpy as np
import pandas as pd
import pytest

from office import desks, entry, screener, views
from office.board import Board
from office.config import COSTS
from office.journal import Journal

NOW = "2026-10-18T10:00:00+00:00"


def mk():
    j = Journal()
    desks.seed(j, NOW)
    return j


def pos(j, asset, desk, qty=1.0, avg=100.0):
    j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts,desk) VALUES(?,?,?,?,?,?,?,?,?)", (asset, qty, avg, avg * .9, avg * 1.2, 1.0, 0, "t", desk))


def trade(j, desk, pnl, fees=1.0, reason="stop", ts="2026-10-18T12:00:00+00:00"):
    j._x("INSERT INTO trades(asset,entry_ts,exit_ts,entry,exit,qty,fees,pnl,r_mult,run_id,exit_reason,desk) VALUES('X','t',?,1,1,1,?,?,0,0,?,?)", (ts, fees, pnl, reason, desk))


def test_four_desks_seeded_with_20_percent_each_and_20_percent_reserve():
    j = mk()
    ds = desks.active(j)
    assert [d["id"] for d in ds] == ["stocks", "majors", "trend", "reversal"] and all(d["share"] == 0.20 for d in ds)
    assert desks.RESERVE == 0.20 and sum(d["share"] for d in ds) + desks.RESERVE == pytest.approx(1.0)
    desks.seed(j, NOW)
    assert len(j.q("SELECT * FROM desks")) == 4                               # повторный запуск не плодит отделы


def test_routing_priority_and_asset_rules():
    j = mk()
    r = lambda sym, pat, op=True: desks.route(j, sym, pat, 100_000, "2026-10-18", op)[0]  # noqa: E731
    assert r("AAPL", "trend")["id"] == "stocks" and r("AAPL", "revert")["id"] == "stocks"
    assert r("AAPL", "trend", op=False) is None                              # акции вне часов биржи — никто не берёт
    assert r("BTC/USD", "support")["id"] == "majors"                          # крупная крипта — любые шаблоны
    assert r("PEPE/USD", "trend")["id"] == "trend" and r("PEPE/USD", "bounce")["id"] == "trend"
    assert r("PEPE/USD", "support")["id"] == "reversal" and r("PEPE/USD", "revert")["id"] == "reversal"
    j.kv_set("major_crypto", ["PEPE/USD"])
    assert r("PEPE/USD", "trend")["id"] == "majors" and r("BTC/USD", "trend")["id"] == "trend"   # список крупной крипты задаёт сканер по обороту


def test_per_desk_limits_3_positions_5_entries_and_budget_overflow():
    j = mk()
    for a in ("A/USD", "B/USD", "C/USD"):
        pos(j, a, "trend")
    d, why = desks.route(j, "PEPE/USD", "trend", 100_000, "2026-10-18", True)
    assert d is None and "лимит 3 позиций отдела" in why and "«Тренд»" in why
    d2, _ = desks.route(j, "AAPL", "trend", 100_000, "2026-10-18", True)       # у «Акций США» свои лимиты
    assert d2["id"] == "stocks"
    for i in range(5):
        j.add_fill(f"2026-10-18T0{i}:00:00+00:00", f"S{i}", "buy", 1, 1, 0, "entry", None, "stocks")
    d3, _ = desks.route(j, "AAPL", "trend", 100_000, "2026-10-18", True)
    assert d3 is None                                                          # «Акции США» исчерпали 5 входов, «Тренд» полон по позициям → входа нет
    d4, why4 = desks.route(j, "AAPL", "trend", 100_000, "2026-10-19", True)
    assert d4["id"] == "stocks"                                                # новые сутки — лимит входов обнулился


def test_budget_is_share_of_equity_and_position_cap_half_of_budget():
    j = mk()
    d = desks.active(j)[0]
    pos(j, "AAPL", "stocks", qty=100, avg=100.0)                              # 10 000 $ уже в отделе
    cap = desks.capacity(j, d, 100_000, "2026-10-18")
    assert cap["budget"] == 20_000 and cap["free"] == 10_000 and cap["ok"]
    lim = desks.limits_for(d, 100_000)
    assert lim.risk_per_trade == 0.02 and lim.daily_stop == -0.05 and lim.max_notional == 0.10 and lim.max_positions == 3 and lim.max_trades_day == 5   # 2% и −5% не тронуты
    pos(j, "MSFT", "stocks", qty=100, avg=100.0)
    assert not desks.capacity(j, d, 100_000, "2026-10-18")["ok"] and "бюджет" in desks.capacity(j, d, 100_000, "2026-10-18")["why"]


def test_crypto_economics_two_to_one_and_four_times_fees():
    c = COSTS["crypto"]
    p = entry.trade_plan(82_125.8, 231.0, "trend", "crypto", c)
    assert p.ok and p.reward_unit >= 2 * p.risk_unit and (p.take - 82_125.8) >= 4 * p.fees_unit and p.take > 82_125.8 + 7.6 * 231
    tiny = entry.trade_plan(100.0, 0.05, "trend", "crypto", c)               # ATR 0,05% при комиссии 0,25%: цель не окупает издержки
    assert not tiny.ok and "экономика сделки не сходится" in tiny.reason
    s = entry.trade_plan(180.0, 0.8, "trend", "stock", COSTS["stock"])
    assert s.ok and s.reward_unit >= 2 * s.risk_unit


def test_council_closes_desk_after_20_losing_trades_only():
    j, b = mk(), Board()
    for i in range(20):
        trade(j, "trend", -5.0)
    for i in range(20):
        trade(j, "reversal", -5.0 if i < 5 else 5.0)                          # 20 сделок, но итог в плюсе — не закрываем
    for i in range(19):
        trade(j, "stocks", -50.0)                                             # минус, но только 19 сделок
    trade(j, "majors", -1.0, reason="partial")                                # частичные не считаются сделками
    out = desks.council(j, b, 100_000, NOW, "2026-10-11T00:00:00+00:00")
    st = {d["id"]: d["status"] for d in desks.all_desks(j)}
    assert st == {"stocks": "active", "majors": "active", "trend": "closed", "reversal": "active"}
    assert any("«Тренд» закрыт: 20 сделок" in x for x in out)
    assert desks.reserve_free(j) == pytest.approx(0.20)                      # резерв 20% + освободившиеся 20% → свободно 20% сверх обязательного резерва
    assert 1 - sum(d["share"] for d in desks.active(j)) == pytest.approx(0.40)    # 20% резерв + освободившиеся 20%


def test_council_rebalances_by_week_result_step_5pp_with_floor_and_cap():
    j, b = mk(), Board()
    trade(j, "reversal", 400.0)
    trade(j, "trend", -300.0)
    trade(j, "stocks", 10.0)
    out = desks.council(j, b, 100_000, NOW, "2026-10-11T00:00:00+00:00")
    sh = {d["id"]: d["share"] for d in desks.active(j)}
    assert sh["reversal"] == pytest.approx(0.25) and sh["trend"] == pytest.approx(0.15) and sh["stocks"] == sh["majors"] == 0.20
    assert sum(sh.values()) == pytest.approx(0.80) and any("перераспределён" in x for x in out)
    for _ in range(10):                                                       # потолок 40% и пол 5% соблюдаются
        trade(j, "reversal", 400.0, ts="2026-10-19T00:00:00+00:00")
        trade(j, "trend", -300.0, ts="2026-10-19T00:00:00+00:00")
        desks.council(j, b, 100_000, NOW, "2026-10-11T00:00:00+00:00")
    sh = {d["id"]: d["share"] for d in desks.active(j)}
    assert sh["reversal"] <= 0.40 + 1e-9 and sh["trend"] >= 0.05 - 1e-9 and sum(sh.values()) == pytest.approx(0.80)


def test_new_desk_only_for_lab_live_strategy_and_only_from_free_reserve():
    j, b = mk(), Board()
    assert "Изменений в структуре нет" in desks.council(j, b, 100_000, NOW, "2026-10-11")[0]     # лаборатории нет → новых отделов нет
    b.x("INSERT INTO lab_trial(key,symbol,name,tf,params,start,status) VALUES('k1','ETH/USD','donchian','1h','{}','t','trial')")
    assert not [d for d in desks.all_desks(j) if d["kind"] == "lab"]                              # испытательный срок — ещё не отдел
    b.x("UPDATE lab_trial SET status='live' WHERE key='k1'")
    assert desks.reserve_free(j) == 0
    desks.council(j, b, 100_000, NOW, "2026-10-11")
    assert not [d for d in desks.all_desks(j) if d["kind"] == "lab"]                              # свободного бюджета нет (резерв ровно 20%) — отдел не открыт
    for i in range(20):
        trade(j, "trend", -5.0)
    out = desks.council(j, b, 100_000, "2026-10-25T20:00:00+00:00", "2026-10-18")                  # «Тренд» закрыт → 20% освободилось
    lab = [d for d in desks.all_desks(j) if d["kind"] == "lab"]
    assert len(lab) == 1 and lab[0]["share"] == pytest.approx(0.20) and any("Открыт отдел «Лаборатория: donchian" in x for x in out)
    assert desks.eligible(j, lab[0], "ETH/USD", "lab", True) and not desks.eligible(j, lab[0], "BTC/USD", "lab", True) and not desks.eligible(j, lab[0], "ETH/USD", "trend", True)


def test_desk_report_in_russian_per_desk_with_fees():
    j = mk()
    trade(j, "trend", -35.0, fees=21.0)
    trade(j, "trend", 15.0, fees=9.0)
    pos(j, "PEPE/USD", "trend", qty=10, avg=2.0)
    view = {"equity": 100_000.0, "positions": [{"symbol": "PEPEUSD", "pl": 4.5}]}
    rows = desks.stats(j, "2026-10-18T00:00:00+00:00", view)
    txt = "\n".join(views.desk_lines(rows, "🏢 Отделы за день:")).replace("\u00a0", " ")
    line = next(x for x in txt.splitlines() if x.startswith("• Тренд:"))
    assert "бюджет 20% (20 000 $)" in line and "открыто 1 из 3" in line and "закрыто 2 сделки, в плюс 1" in line
    assert "прибыль после комиссий −20 $ (комиссии 31,00 $)" in line and "не закрыто +4 $" in line      # комиссии: закрытые сделки 30 $ + вход в открытую позицию 1 $
    for name in ("Акции США", "Крупная крипта", "Отскок и возврат к средней"):
        assert name in txt
    assert "Итого по отделам" in txt and "резерв 20%" in txt


def test_scanner_returns_10_to_20_candidates_without_any_llm(monkeypatch):
    idx = pd.date_range("2026-06-01", periods=100, freq="D", tz="UTC")
    syms = [f"C{i}/USD" for i in range(25)] + [f"S{i}" for i in range(40)]

    def bars(seed, drift):
        px = 100 * np.exp(np.cumsum(np.random.default_rng(seed).normal(drift, 0.02, 100)))
        return pd.DataFrame({"open": px, "high": px * 1.01, "low": px * .99, "close": px, "volume": 1e7}, index=idx)
    data_ = {s: bars(i, 0.004 if i % 2 else -0.004) for i, s in enumerate(syms)}
    monkeypatch.setattr(screener, "universe", lambda c: {"crypto": [s for s in syms if "/" in s], "stock": [s for s in syms if "/" not in s]})
    monkeypatch.setattr(screener.data, "alpaca_bars", lambda ss, kind, days=100, **k: {s: data_[s] for s in ss if s in data_})
    monkeypatch.setattr(screener, "crypto_volumes", lambda: {f"C{i}": 1e9 - i * 1e6 for i in range(25)})
    monkeypatch.setattr(screener.lab, "live_symbols", lambda b: set())
    b, majors = Board(), []
    top = screener.scan(b, None, True, majors_out=majors)
    assert 10 <= len(top) <= 20 and len({r["symbol"] for r in top}) == len(top)
    assert majors == [f"C{i}/USD" for i in range(5)]                          # крупная крипта = топ по обороту
    assert {r["why"] for r in top} >= {"trend", "reversal"} and len(b.q("SELECT * FROM candidates")) == len(top)
    stocks_closed = screener.scan(Board(), None, False)
    assert all("/" in r["symbol"] for r in stocks_closed)                      # вне часов биржи акций среди кандидатов нет


def test_reversal_score_prefers_oversold_near_low():
    idx = pd.date_range("2026-06-01", periods=60, freq="D", tz="UTC")
    down = pd.Series(np.linspace(120, 90, 60), index=idx)
    up = pd.Series(np.linspace(90, 120, 60), index=idx)
    mk_ = lambda px: pd.DataFrame({"open": px, "high": px * 1.01, "low": px * .99, "close": px, "volume": 1e6}, index=idx)  # noqa: E731
    assert screener.reversal_score(mk_(down)) > 0.5 > screener.reversal_score(mk_(up))


def test_legacy_positions_adopted_by_asset_class():
    j = Journal()
    for a in ("AAPL", "BTC/USD", "ARB/USD"):
        j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts) VALUES(?,?,?,?,?,?,?,?)", (a, 1, 1, .5, 2, 0, 0, "t"))
    desks.seed(j, NOW)
    assert {p["asset"]: p["desk"] for p in j.positions().values()} == {"AAPL": "stocks", "BTC/USD": "majors", "ARB/USD": "trend"}
