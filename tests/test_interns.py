import datetime as dt
import json

import numpy as np
import pandas as pd

from office import interns, rules
from office.board import Board
from office.journal import Journal

T0 = dt.datetime(2026, 10, 21, 22, 0, tzinfo=dt.timezone.utc)


def bars(start=100.0, drift=0.004, n=140, vol=0.004, seed=1, end="2026-10-21"):
    rng = np.random.default_rng(seed)
    r = drift + vol * rng.standard_normal(n)
    c = start * np.cumprod(1 + r)
    idx = pd.date_range(end=end, periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e7}, index=idx)


def world(regime="neutral", now=T0, prices=None):
    b, j = Board(), Journal()
    px = prices if prices is not None else {}
    data = {}
    it = interns.Interns(b, j, lambda: [s for s in data if s not in interns.ETF_SKIP], lambda syms, days: {s: data[s] for s in syms if s in data}, lambda a: data["__h__"][a] if "__h__" in data else None,
                         lambda a: px.get(a) or float(data[a]["close"].iloc[-1]), regime_fn=lambda: regime, now_fn=lambda: now)
    return it, b, j, data, px


def fill(data):
    for i, (s, d) in enumerate({"AAA": 0.006, "BBB": 0.005, "CCC": 0.004, "DDD": 0.0035, "EEE": 0.003, "FFF": 0.0025, "GGG": -0.004}.items()):
        data[s] = bars(drift=d, seed=i + 2, vol=0.0008)
    for i, s in enumerate(interns.SECTORS):
        data[s] = bars(drift=0.004 - i * 0.0007, seed=20 + i, vol=0.0005)
    data["GLD"], data["TLT"], data["SPY"] = bars(drift=0.001, seed=40), bars(drift=0.0005, seed=41), bars(drift=0.001, seed=42)


def test_scores_and_picks():
    up, down = bars(drift=0.006), bars(drift=-0.004)
    assert interns.score_momentum(up["close"]) > 0 > interns.score_momentum(down["close"])
    assert interns.score_momentum(up["close"].iloc[:30]) is None
    assert interns.above_sma(up["close"]) and not interns.above_sma(down["close"])
    data = {}
    fill(data)
    top = interns.pick_top(data)
    assert len(top) == 5 and [t[0] for t in top][0] == "AAA" and "GGG" not in [t[0] for t in top] and not any(t[0] in interns.ETF_SKIP for t in top)
    assert top == sorted(top, key=lambda x: -x[1])
    sec = interns.pick_sectors(data)
    assert [s for s, _ in sec] == ["XLK", "XLE", "XLF"]
    assert interns.pick_sectors({}) == []


def test_momentum_enters_top5_with_gate_sizing_and_costs():
    it, b, j, data, px = world()
    fill(data)
    out = it.step()
    assert out["daily"] == "2026-10-21"
    pos = it.positions("mom")
    assert 1 <= len(pos) <= 5 and "GGG" not in pos
    for a, p in pos.items():
        eq = interns.START
        assert p["qty"] * p["entry"] <= 0.25 * eq + 1                                    # лимит позиции 25% как у боевых
        assert p["stop"] < p["entry"] and p["take"] is None
        c = float(data[a]["close"].iloc[-1])
        assert abs(p["entry"] - c * 1.0005) < 1e-6                                       # проскальзывание из config.COSTS
        assert (p["entry"] - p["stop"]) * p["qty"] <= 0.02 * eq * 1.05                   # риск на сделку ≤ 2%
    assert it.cash("mom") < interns.START and it.equity("mom") > interns.START * 0.99


def test_each_intern_starts_with_20000_and_is_independent():
    it, b, j, data, px = world()
    fill(data)
    it.step()
    r = {x["name"]: x for x in it.rows()}
    assert set(r) == set(interns.NAMES) and all(x["start"] == 20_000.0 for x in r.values())
    assert it.cash("def") == 20_000.0 and it.positions("def") == {}


def test_hold_until_below_sma_then_exit():
    it, b, j, data, px = world()
    fill(data)
    it.step()
    held = list(it.positions("mom"))[0]
    d = data[held].copy()
    d.iloc[-1, d.columns.get_loc("close")] = float(d["close"].tail(50).mean()) * 0.9
    d.iloc[-1, d.columns.get_loc("low")] = d["close"].iloc[-1]
    new = pd.concat([d, d.iloc[[-1]].set_axis([d.index[-1] + pd.Timedelta(days=1)])])
    data[held] = new
    data["SPY"] = pd.concat([data["SPY"], data["SPY"].iloc[[-1]].set_axis([data["SPY"].index[-1] + pd.Timedelta(days=1)])])
    it.now = lambda: T0 + dt.timedelta(days=1)
    it.step()
    assert held not in it.positions("mom")
    tr = b.q("SELECT * FROM intern_trades WHERE name='mom' AND asset=?", (held,))
    assert tr and tr[0]["reason"] in ("stop", "ниже 50-дневной средней")


def test_stop_by_daily_low():
    it, b, j, data, px = world()
    fill(data)
    it.step()
    held = list(it.positions("mom"))[0]
    stop = it.positions("mom")[held]["stop"]
    d = data[held]
    nxt = d.iloc[[-1]].copy()
    nxt.index = [d.index[-1] + pd.Timedelta(days=1)]
    nxt["low"] = stop * 0.97
    nxt["close"] = stop * 0.99
    data[held] = pd.concat([d, nxt])
    data["SPY"] = pd.concat([data["SPY"], data["SPY"].iloc[[-1]].set_axis([data["SPY"].index[-1] + pd.Timedelta(days=1)])])
    it.now = lambda: T0 + dt.timedelta(days=1)
    it.step()
    t = b.q("SELECT * FROM intern_trades WHERE name='mom' AND asset=?", (held,))[0]
    assert t["reason"] == "stop" and t["pnl"] < 0 and abs(t["exit"] - stop * 0.9995) < 1e-6
    assert abs(t["pnl"]) <= 0.02 * 20_000 * 1.1                                         # потеря ограничена риском на сделку


def test_no_double_processing_same_bar():
    it, b, j, data, px = world()
    fill(data)
    it.step()
    n = b.q("SELECT COUNT(*) n FROM intern_eq")[0]["n"]
    assert "daily" not in it.step()
    assert b.q("SELECT COUNT(*) n FROM intern_pos")[0]["n"] == sum(len(it.positions(k)) for k in interns.NAMES)
    assert b.q("SELECT COUNT(*) n FROM intern_eq")[0]["n"] == n + 1                      # только снимок «Аналитиков с вето»


def test_sector_rotation_weekly():
    it, b, j, data, px = world()
    fill(data)
    it.step()
    assert set(it.positions("sect")) == {"XLK", "XLE", "XLF"}
    assert j.kv_get("interns_sect_week") == "2026-43"
    for s in ("XLK", "XLE", "XLF"):                                                     # в ту же неделю состав не трогаем, даже если лидеры сменились
        data[s] = bars(drift=-0.003, seed=3)
    for s in data:
        data[s] = pd.concat([data[s], data[s].iloc[[-1]].set_axis([data[s].index[-1] + pd.Timedelta(days=1)])])
    it.now = lambda: T0 + dt.timedelta(days=1)
    it.step()
    assert set(it.positions("sect")) == {"XLK", "XLE", "XLF"} or j.kv_get("interns_sect_week") == "2026-43"
    for s in data:
        data[s] = pd.concat([data[s], data[s].iloc[[-1]].set_axis([data[s].index[-1] + pd.Timedelta(days=6)])])
    it.now = lambda: T0 + dt.timedelta(days=7)
    it.step()
    assert j.kv_get("interns_sect_week") == "2026-44" and not ({"XLK", "XLE", "XLF"} & set(it.positions("sect")))


def test_defender_regimes():
    it, b, j, data, px = world(regime="risk_off")
    fill(data)
    it.step()
    assert set(it.positions("def")) == {"GLD", "TLT"} and it.cash("def") < 20_000
    assert all(p["qty"] * p["entry"] <= 0.25 * 20_000 + 1 for p in it.positions("def").values())
    it.regime_fn = lambda: "neutral"
    for s in data:
        data[s] = pd.concat([data[s], data[s].iloc[[-1]].set_axis([data[s].index[-1] + pd.Timedelta(days=1)])])
    it.now = lambda: T0 + dt.timedelta(days=1)
    it.step()
    assert it.positions("def") == {} and abs(it.cash("def") - 20_000) < 400
    assert {t["reason"].split("—")[-1].strip() for t in b.q("SELECT reason FROM intern_trades WHERE name='def'")} == {"уходим в кэш"}
    it2, *_ = world(regime="risk_on")
    assert it2.regime_fn() == "risk_on"


def test_defender_regime_from_board_picture():
    b, j = Board(), Journal()
    it = interns.Interns(b, j, lambda: [], lambda s, d: {}, lambda a: None, lambda a: 1.0, now_fn=lambda: T0)
    assert it._regime_from_board() == "neutral"
    b.x("INSERT INTO reports(cycle_id,role,asset,ts,verdict,confidence,refs,stances,data) VALUES('c','picture','*',?,'x',0.7,'[]','{}',?)",
        (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), json.dumps({"regime": "risk_off"})))
    assert it._regime_from_board() == "risk_off"


# ── «Аналитики с вето» ───────────────────────────────────────────────────────
def mk_vet(analyst_acc, sell_by=(), buy_by=("market", "quant", "news"), asset="NVDA"):
    it, b, j, data, px = world()
    fill(data)
    j.add_signal((T0 - dt.timedelta(minutes=40)).isoformat(timespec="seconds"), asset, "trend", 100.0, 2.0, 96.0, 108.0)   # старый сигнал (до старта) — пропускается
    it.step()                                                                                                                 # старт: указатель на последний сигнал
    sid = j.q("SELECT MAX(id) m FROM tech_signals")[0]["m"]
    assert sid == 1 and j.kv_get("interns_sig_id") == 1
    t_sig = T0 - dt.timedelta(minutes=30)
    j.add_signal(t_sig.isoformat(timespec="seconds"), asset, "trend", 100.0, 2.0, 96.0, 108.0)
    for role in set(buy_by) | set(sell_by):
        st = "sell" if role in sell_by else "buy"
        b.x("INSERT INTO reports(cycle_id,role,asset,ts,verdict,confidence,refs,stances,data) VALUES('c',?,?,?,?,0.7,'[]','{}',?)", (role, asset, t_sig.isoformat(timespec="seconds"), st, json.dumps({"stance": st})))
    for role, (n, k) in analyst_acc.items():                                    # история оценённых мнений для точности
        for i in range(n):
            rid = b.x("INSERT INTO reports(cycle_id,role,asset,ts,verdict,confidence,refs,stances,data) VALUES('h',?,'X','2026-10-01T00:00:00','buy',0.6,'[]','{}','{}')", (role,)).lastrowid
            b.x("INSERT INTO outcomes(report_id,label,correct) VALUES(?,?,?)", (rid, 1, 1 if i < k else 0))
    return it, b, j


def run_later(it):
    it.now = lambda: T0 + dt.timedelta(minutes=5)
    return it.run_vet((T0 + dt.timedelta(minutes=5)).isoformat(timespec="seconds"))


ACC = {"market": (12, 11), "quant": (12, 10), "news": (12, 5)}


def test_top2_most_accurate_analysts():
    it, b, j = mk_vet(ACC)
    assert it.vetoers() == ["market", "quant"]
    assert j.kv_get("interns_vetoers") == ["market", "quant"]
    b.x("DELETE FROM outcomes")
    assert it.vetoers() == ["market", "quant"]                                  # оценки обрезаны хранением — берём последний известный список


def test_veto_by_top_analyst_blocks_entry():
    it, b, j = mk_vet(ACC, sell_by=("market",))
    out = run_later(it)
    assert out["vetoed"] == 1 and out["entered"] == 0 and it.positions("vet") == {}


def test_sell_by_weak_analyst_does_not_veto_and_majority_gives_full_size():
    it, b, j = mk_vet(ACC, sell_by=("news",), buy_by=("market", "quant"))
    out = run_later(it)
    assert out["entered"] == 1 and out["vetoed"] == 0
    p = it.positions("vet")["NVDA"]
    assert p["qty"] > 0 and p["stop"] < 100 < p["take"]
    full = p["qty"]
    it2, *_ = mk_vet(ACC, sell_by=("news", "quant"), buy_by=("market",))       # большинство против, вето нет (quant — в топ-2, значит вето!)
    out2 = run_later(it2)
    assert out2["vetoed"] == 1
    it3, b3, j3 = mk_vet({"market": (12, 11), "quant": (12, 10), "news": (12, 5)}, sell_by=("news",), buy_by=())   # без «за» — половинный объём
    out3 = run_later(it3)
    assert out3["entered"] == 1 and it3.positions("vet")["NVDA"]["qty"] <= full * 0.5 + 1


def test_vet_waits_for_votes_and_pointer_moves():
    it, b, j = mk_vet(ACC)
    out = it.run_vet(T0.isoformat(timespec="seconds"))                         # сигналу 30 мин — созрел; но проверим, что свежий (5 мин) ждёт
    j.add_signal((T0 - dt.timedelta(minutes=5)).isoformat(timespec="seconds"), "AAPL", "trend", 100.0, 2.0, 96.0, 108.0)
    before = j.kv_get("interns_sig_id")
    it.run_vet(T0.isoformat(timespec="seconds"))
    assert j.kv_get("interns_sig_id") == before and out is not None


# ── отчёты и совет ───────────────────────────────────────────────────────────
def test_rows_lines_and_russian():
    it, b, j, data, px = world()
    fill(data)
    it.step()
    t = it.lines()
    assert t.startswith("🎓 Стажёры") and all(n in t for n in interns.NAMES.values())
    import re
    assert not re.search(r"[A-Za-z]", re.sub(r"\$|S&P", "", t))
    assert it.rows(since="2099-01-01") == []
    assert "ещё не начали" in world()[0].lines()


def test_verdicts_need_data_then_promote_and_fire(monkeypatch):
    it, b, j, data, px = world()
    fill(data)
    it.step()
    desk = [{"name": "Основной", "pnl": 100.0, "budget": 10000.0}]
    now = T0 + dt.timedelta(days=60)
    v = it.verdicts(j, desk, T0 + dt.timedelta(days=2))
    assert all("наблюдаем" in x for x in v[1:]) and "Боевые отделы" in v[0]
    # искусственно: сильный стажёр и слабый стажёр, достаточно срока и сделок
    for k, eq in (("mom", 23_000.0), ("sect", 18_000.0)):
        b.x("INSERT INTO intern_eq(ts,name,equity,cash) VALUES(?,?,?,?)", ((T0 + dt.timedelta(days=50)).isoformat(timespec="seconds"), k, eq, eq))
        for i in range(7):
            b.x("INSERT INTO intern_trades(name,asset,opened,closed,entry,exit,qty,pnl,reason) VALUES(?,?,?,?,1,1,1,1,'x')", (k, "A", "2026-10-21", "2026-11-01", ))
    b.x("INSERT INTO shadow_eq(ts,name,equity,cash) VALUES(?,?,?,?)", (T0.isoformat(timespec="seconds"), "spy", 100.0, 0))
    b.x("INSERT INTO shadow_eq(ts,name,equity,cash) VALUES(?,?,?,?)", ((T0 + dt.timedelta(days=50)).isoformat(timespec="seconds"), "spy", 101.0, 0))
    out = "\n".join(it.verdicts(j, desk, now))
    assert "ПЕРЕВЕСТИ В БОЕВЫЕ" in out and "УВОЛИТЬ" in out and "отпечаток" in out
    assert "решаете вы" in out


def test_does_not_touch_rules_fingerprint_or_trade_count():
    j = Journal()
    fp = rules.fingerprint()
    it, b, j2, data, px = world()
    fill(data)
    it.step()
    assert rules.fingerprint() == fp
    assert "interns" not in json.dumps(rules.snapshot(), default=str)
    assert j2.q("SELECT COUNT(*) n FROM trades")[0]["n"] == 0 and j2.q("SELECT COUNT(*) n FROM fills")[0]["n"] == 0
    assert rules.status(j)["closed"] == 0


# ── подключение к офису: вечерний/недельный отчёты и совет ─────────────────────
def _office(monkeypatch, tmp_path):
    from office import config
    from office.cloud import Office
    from tests.helpers_cloud import C, FakeHall
    sent = []
    monkeypatch.setattr(config, "STOP_FILE", tmp_path / "STOP")
    o = Office(board=Board(), journal=Journal(), client=C(), hall=FakeHall(), notify=sent.append, router=object())
    o.sched.run_due = lambda: []
    return o, sent


def test_evening_and_weekly_have_interns_and_council_proposes(monkeypatch, tmp_path):
    o, sent = _office(monkeypatch, tmp_path)
    data = {}
    fill(data)
    o.interns.universe_fn = lambda: [s for s in data if s not in interns.ETF_SKIP]
    o.interns.bars_fn = lambda syms, days: {s: data[s] for s in syms if s in data}
    o.interns.now = lambda: T0
    o.interns.step()
    monkeypatch.setattr(o, "_local_midnight_utc", lambda: "2026-10-20T21:00:00+00:00")
    monkeypatch.setattr(o.shadow, "summary", lambda since=None: [])
    o.evening()
    assert "🎓 Стажёры" in sent[-1] and "Импульс акций США" in sent[-1] and "Защитник" in sent[-1]
    o.weekly_with_council()
    joined = "\n".join(sent)
    assert "🎓 Стажёры за неделю" in joined and "🎓 Стажёры и совет" in joined and "наблюдаем" in joined


def test_interns_failure_does_not_break_reports(monkeypatch, tmp_path):
    o, sent = _office(monkeypatch, tmp_path)
    def boom(*a, **k):
        raise RuntimeError("сломались")
    o.interns.lines = boom
    o.interns.council_text = boom
    monkeypatch.setattr(o.shadow, "summary", lambda since=None: [])
    monkeypatch.setattr(o, "_local_midnight_utc", lambda: "2026-10-20T21:00:00+00:00")
    o.evening()
    assert sent and "Вечерний отчёт" in sent[-1]
    o.weekly_with_council()
    assert any("Совет директоров" in m for m in sent)


def test_job_registered_hourly(monkeypatch, tmp_path):
    o, _ = _office(monkeypatch, tmp_path)
    j = {x.name: x for x in o.sched.jobs}
    assert j["interns"].every == 3600 and j["interns"].fn == o.interns.step
