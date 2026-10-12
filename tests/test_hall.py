"""Цепочка зала на фейках: правило ссылок, пропуск при невалидном/лимитном ответе, исполнение."""
import json
import re

import numpy as np
import pandas as pd
import pytest

from office import agents, data, hall, portfolio
from office.board import Board
from office.hall import Hall
from office.journal import Journal
from office.llm import LLMUnavailable

PRICE, ATR = 100.0, 1.0


class FakeRouter:
    def __init__(self, stance="buy", unavailable=False):
        self.stance, self.unavailable = stance, unavailable

    def ask(self, role, payload, system=None):
        if self.unavailable:
            raise LLMUnavailable("429")
        return {"stance": self.stance, "confidence": 0.8, "rationale": f"{role} says {self.stance}"}


class FakeClaude:
    def __init__(self, trader_action="buy", bad_role=None, down_role=None):
        self.trader_action, self.bad_role, self.down_role, self.calls = trader_action, bad_role, down_role, []

    def text(self, system, prompt, model=""):  # дебаты
        ids = json.loads(re.search(r"Use only ids from: (\[.*?\])", prompt).group(1))
        must = re.search(r"MUST cite these report ids explicitly: (\[.*?\])", prompt)
        cite = sorted(set(ids) | set(json.loads(must.group(1)) if must else []))
        return "argument\nREFS_JSON: " + json.dumps({"agree": cite[:-1], "dispute": cite[-1:], "confidence": 0.7, "stance": "buy"})

    def ask(self, role, payload, system=None):
        self.calls.append(role)
        if role == self.down_role:
            raise LLMUnavailable("limit")
        ids = [r["id"] for r in payload["reports"]]
        base = {"refs": [] if role == self.bad_role else ids, "stances": {str(i): "agree" for i in ids}}
        if role == "trader":
            return {**base, "action": self.trader_action, "entry": PRICE, "stop": PRICE - 1.5 * ATR, "take_profit": PRICE + 4.5 * ATR, "confidence": 0.7, "rationale": "r"}
        if role == "risk_manager":
            return {**base, "verdict": "approve", "size_mult": 1.0, "rationale": "r"}
        if role == "portfolio_manager":
            return {**base, "decision": "approve", "size_mult": 1.0, "rationale": "r"}
        return {**base, "ticket_ok": True, "rationale": "ok"}


class FakeBroker:
    cash, last_order_id = 100_000.0, None

    def __init__(self):
        self.bought, self.pos = [], {}

    def equity(self, prices=None):
        return 100_000.0

    def position(self, name):
        return self.pos.get(name)

    def buy(self, name, qty, price, stop, take, ts, run_id=None):
        self.bought.append((name, qty, stop, take))
        self.last_order_id = "ord-1"
        return {"price": price, "qty": qty, "fee": 0.0}


@pytest.fixture
def env(monkeypatch):
    idx = pd.date_range("2026-01-01", periods=600, freq="h", tz="UTC")
    px = pd.Series(100 + np.cumsum(np.random.default_rng(1).normal(0, .2, 600)), index=idx)
    df = pd.DataFrame({"open": px, "high": px + .3, "low": px - .3, "close": px, "volume": 1.0})
    ind = {"close": PRICE, "atr14": ATR, "rsi14": 55.0}
    monkeypatch.setattr(data, "candles", lambda *a, **k: df)
    monkeypatch.setattr(data, "bundle", lambda a, d=None: {"asset": a.name, "as_of": "t", "indicators_1h": ind, "indicators_4h": ind, "last_candles_1h": [],
                                                           "tradingview": {}, "news": [{"title": "x"}], "fundamentals": {"pe": 10}})
    monkeypatch.setattr(agents, "quant_stats", lambda a: {"sharpe_90d": 1})
    monkeypatch.setattr(portfolio, "daily_closes", lambda syms: pd.DataFrame({s: 100 + np.cumsum(np.random.default_rng(i).normal(.05, 1, 120)) for i, s in enumerate(syms)}))
    monkeypatch.setattr(hall, "utcnow", lambda: "2026-10-12T10:00:00+00:00")


PIC = {"regime": "neutral", "summary": "тест", "size_mult": 1.0, "main_risks": [], "forbidden": [], "no_new_entries": False, "avoid_assets": []}


def mk(router=None, claude=None, pic=True, **kw):
    b, j, br = Board(), Journal(), FakeBroker()
    if pic:  # «Картина дня» обязательна: без неё зал отклоняет работу
        b.publish("R-pic", "picture", "*", "neutral: тест", 0.8, data={**PIC, **(pic if isinstance(pic, dict) else {})})
    return Hall(b, j, br, router or FakeRouter(), claude or FakeClaude(), **kw), b, br


def test_full_chain_executes_and_every_post_analyst_report_cites(env):
    h, b, br = mk()
    r = h.run_asset("BTC/USD")
    assert r.status == "opened" and r.order["order_id"] == "ord-1" and br.bought
    reps = b.cycle_reports(r.cycle_id, "BTC/USD")
    roles = [x["role"] for x in reps]
    assert roles[-8:] == ["bull", "bear", "bull", "bear", "trader", "risk_manager", "portfolio_manager", "executor"]
    for x in reps:
        if x["role"] not in ("market", "news", "fundamentals", "quant"):
            assert x["refs"] and set(map(str, x["refs"])) <= set(x["stances"])  # ссылки + явная позиция по каждой
    assert reps[-1]["refs"] == [reps[-2]["id"]]  # executor ссылается на PM


def test_risk_gate_caps_size_and_stop_attached(env):
    h, b, br = mk()
    h.run_asset("BTC/USD")
    name, qty, stop, take = br.bought[0]
    assert qty * PRICE <= 0.25 * 100_000 + 1 and stop < PRICE < take
    assert qty * (PRICE - stop) <= 0.02 * 100_000 * 1.01  # <=2% риска


def test_report_without_refs_is_rejected_and_asset_skipped(env):
    h, b, br = mk(claude=FakeClaude(bad_role="risk_manager"))
    r = h.run_asset("BTC/USD")
    assert r.status == "skipped" and "risk_manager" in r.detail and not br.bought
    assert b.q("SELECT COUNT(*) n FROM rejected")[0]["n"] >= 1


def test_llm_limit_on_chain_role_skips_not_crashes(env):
    h, b, br = mk(claude=FakeClaude(down_role="trader"))
    r = h.run_asset("BTC/USD")
    assert r.status == "skipped" and "лимит" in r.detail and not br.bought


def test_analysts_unavailable_abstain_and_skip(env):
    h, b, br = mk(router=FakeRouter(unavailable=True))
    r = h.run_asset("BTC/USD")
    assert r.status == "skipped" and "недостаточно аналитиков" in r.detail
    assert {x["verdict"].split(":")[0] for x in b.cycle_reports(r.cycle_id, "BTC/USD")} == {"abstain"}


def test_trader_hold_no_order(env):
    h, b, br = mk(claude=FakeClaude(trader_action="hold"))
    assert h.run_asset("BTC/USD").status == "hold" and not br.bought


def test_sell_without_position_never_shorts(env):
    h, b, br = mk(claude=FakeClaude(trader_action="sell"))
    assert h.run_asset("BTC/USD").status == "hold" and not br.bought


def test_halt_file_blocks_new_entries(env, monkeypatch, tmp_path):
    f = tmp_path / "STOP"
    f.touch()
    monkeypatch.setattr(hall.config, "STOP_FILE", f)
    h, b, br = mk()
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "STOP" in r.detail and not br.bought


def test_dry_run_sends_no_order(env):
    h, b, br = mk(auto_trade=False)
    assert h.run_asset("BTC/USD").detail.startswith("dry-run") and not br.bought


def test_limits_exhausted_skips_llm_spend(env):
    class Full(FakeBroker):
        pass
    h, b, br = mk()
    h.j.kv_set("day_start:2026-10-12", 100_000.0)
    for i in range(3):
        h.j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts) VALUES(?,?,?,?,?,?,?,?)", (f"X{i}", 1, 1, 0.5, 2, 0, 1, "t"))
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "лимит позиций" in r.detail and not br.bought
    assert b.cycle_reports(r.cycle_id, "BTC/USD") == []  # аналитики не вызывались


# ---------- v3: «Картина дня» и календарь ----------
def test_no_picture_means_skip(env):
    h, b, br = mk(pic=False)
    r = h.run_asset("BTC/USD")
    assert r.status == "skipped" and "Картин" in r.detail and not br.bought


def test_picture_no_new_entries_and_avoid_block_before_llm(env):
    c = FakeClaude()
    h, b, br = mk(claude=c, pic={"no_new_entries": True})
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "картина дня" in r.detail and c.calls == []
    h, b, br = mk(claude=c, pic={"avoid_assets": ["BTC/USD"]})
    assert h.run_asset("BTC/USD").status == "hold" and c.calls == []


def test_event_blackout_blocks_new_entries(env):
    import datetime as dt
    h, b, br = mk()
    now = dt.datetime.now(dt.timezone.utc)
    b.x("INSERT INTO events(key,ts,title,ccy,impact,kind,assets,src) VALUES('k',?,?,?,?,?,?,?)", ((now + dt.timedelta(minutes=10)).isoformat(timespec="minutes"), "FOMC Statement", "USD", "High", "macro", "all", "t"))
    r = h.run_asset("BTC/USD")
    assert r.status == "hold" and "решение ФРС" in r.detail and not br.bought


def test_trader_without_picture_citation_is_rejected(env):
    class NoPic(FakeClaude):
        def ask(self, role, payload, system=None):
            out = super().ask(role, payload, system)
            if role == "trader":
                pid = next(r["id"] for r in payload["reports"] if r["role"] == "picture")
                out["refs"] = [i for i in out["refs"] if i != pid]
            return out
    h, b, br = mk(claude=NoPic())
    r = h.run_asset("BTC/USD")
    assert r.status == "skipped" and not br.bought
    assert any("picture" in x["reason"] for x in b.q("SELECT reason FROM rejected"))


def test_stale_picture_cannot_be_cited(env):
    h, b, br = mk()
    b.x("UPDATE reports SET ts='2020-01-01T00:00:00+00:00' WHERE role='picture'")
    r = h.run_asset("BTC/USD")
    assert r.status == "skipped" and not br.bought


def test_picture_size_mult_only_reduces(env):
    h1, _, br1 = mk()
    h1.run_asset("BTC/USD")
    h2, _, br2 = mk(pic={"size_mult": 0.5})
    h2.run_asset("BTC/USD")
    assert br1.bought and br2.bought and br2.bought[0][1] < br1.bought[0][1]  # размер меньше (PM-кап в первом прогоне мог уже ограничить)
