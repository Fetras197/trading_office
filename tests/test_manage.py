import datetime as dt

import pandas as pd

from office import manage
from office.alpaca import AlpacaBroker, AlpacaError
from office.journal import Journal

P = {"avg": 100.0, "stop": 97.0, "take": 108.0}   # R = 3


def d(price, **kw):
    a = dict(atr=1.0, init_stop=97.0, partial_done=False, partial_at=None, age_h=5.0, range48=0.05, kind="stock")
    a.update(kw)
    return manage.decide(P, price, a["atr"], a["init_stop"], a["partial_done"], a["partial_at"], a["age_h"], a["range48"], a["kind"])


def test_stop_never_moves_down_and_waits_for_1R():
    assert d(101)["stop"] is None and d(102.9)["stop"] is None          # < +1R: ничего
    r = d(103.2)                                                        # +1R: безубыток
    assert 100 < r["stop"] < 100.2 and "безубыток" in r["why"]


def test_trailing_from_1_5R_and_min_step():
    r = d(105, atr=1.0)                                                 # +1.67R: трейл = 105 − 2 = 103
    assert abs(r["stop"] - 103.0) < 1e-9 and "подтяжка за ценой" in r["why"]
    assert d(104.6, atr=3.0)["stop"] is not None and d(104.6, atr=3.0)["stop"] < 104.6   # трейл 98.6 ниже безубытка — остаётся безубыток
    assert d(104.6, atr=3.0)["stop"] < 101


def test_partial_at_3pct_or_trader_level_then_breakeven():
    r = d(103.0)
    assert r["partial"] and r["stop"] and r["stop"] > 100
    assert not d(102.0)["partial"]
    assert d(104.0, partial_at=104.0)["partial"] and not d(103.5, partial_at=104.0)["partial"]
    assert not d(110.0, partial_done=True)["partial"]


def test_time_exit_only_for_stagnant_position_not_for_winners():
    assert d(100.2, age_h=49, range48=0.01)["close"] == "time"
    assert d(100.2, age_h=47, range48=0.01)["close"] is None
    assert d(100.2, age_h=49, range48=0.05)["close"] is None
    assert d(104.0, age_h=49, range48=0.01)["close"] is None            # прибыль > +1R — не трогаем


# ---------- брокер: порядок вызовов (защита не должна пропадать) ----------
class FakeC:
    def __init__(self, fail_patch=False, fail_install=0):
        self.log, self.fail_patch, self.fail_install, self.n = [], fail_patch, fail_install, 0
        self.pos = {"qty_available": "100", "current_price": "103"}

    def orders(self, **k):
        return [{"id": "stop1", "side": "sell", "type": "stop_limit", "qty": "100", "stop_price": "97"}]

    def _req(self, m, path, **kw):
        self.log.append((m, path, kw.get("json")))
        if self.fail_patch:
            raise AlpacaError("422")
        return {"id": "replaced-1"}

    def cancel(self, oid): self.log.append(("cancel", oid))

    def submit(self, body):
        self.log.append(("submit", body["type"], body.get("order_class"), body["side"], body.get("stop_price") or body.get("stop_loss")))
        if body["type"] in ("stop_limit", "limit") and body["side"] == "sell" and self.fail_install > 0:
            self.fail_install -= 1
            raise AlpacaError("no")
        self.n += 1
        return {"id": f"o{self.n}"}

    def order(self, oid): return {"id": oid, "status": "filled", "filled_avg_price": "103", "filled_qty": "50"}
    def position(self, sym): return self.pos


def mk(c, kind_sym="BTC/USD"):
    j = Journal()
    j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts) VALUES(?,?,?,?,?,?,?,?)", (kind_sym, 100, 100.0, 97.0, 108.0, 2.0, 1, "2026-10-12T00:00:00+00:00"))
    return AlpacaBroker(c, j), j


def test_move_stop_patches_existing_order_and_updates_journal(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    c = FakeC()
    b, j = mk(c)
    assert b.move_stop("BTC/USD", 101.0) and j.positions()["BTC/USD"]["stop"] == 101.0
    assert c.log[0][0] == "PATCH" and c.log[0][2]["stop_price"] == "101.0000"
    assert j.kv_get("stop_oid:BTC/USD") == "replaced-1"                    # новый id ордера сохранён (замена создаёт новый ордер)
    assert not b.move_stop("BTC/USD", 99.0)                              # вниз — нельзя


def test_move_stop_falls_back_to_cancel_and_reinstall_and_restores_on_failure(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    c = FakeC(fail_patch=True)
    b, j = mk(c)
    assert b.move_stop("BTC/USD", 101.0)
    assert [x[0] for x in c.log if x[0] in ("cancel", "submit")] == ["cancel", "submit"]
    c2 = FakeC(fail_patch=True, fail_install=1)                          # новый стоп не встал -> возвращаем старый
    b2, j2 = mk(c2)
    assert b2.move_stop("BTC/USD", 101.0) is False and j2.positions()["BTC/USD"]["stop"] == 97.0
    subs = [x for x in c2.log if x[0] == "submit"]
    assert len(subs) == 2 and subs[1][4] == "97.0000"


def test_sell_part_cancels_sells_half_and_reprotects_remainder(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    c = FakeC()
    c.pos = {"qty_available": "100", "current_price": "103"}
    b, j = mk(c)
    seq = iter([{"qty_available": "100", "current_price": "103"}, {"qty_available": "50", "current_price": "103"}])
    c.position = lambda sym: next(seq)
    r = b.sell_part("BTC/USD", 0.5, "2026-10-12T10:00:00+00:00", 100.5)
    assert r and r["qty"] == 50 and r["pnl"] > 0
    kinds = [x[0] + ":" + str(x[1]) for x in c.log if x[0] in ("cancel", "submit")]
    assert kinds == ["cancel:stop1", "submit:market", "submit:stop_limit"]       # защита ставится сразу после продажи
    pos = j.positions()["BTC/USD"]
    assert pos["qty"] == 50 and pos["stop"] == 100.5
    t = j.q("SELECT exit_reason, qty FROM trades")
    assert t == [{"exit_reason": "partial", "qty": 50.0}]


def test_manager_run_moves_stop_and_notifies_once(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    c = FakeC()
    b, j = mk(c)
    j.kv_set("partial_done:BTC/USD", 1)      # половина уже продана: остаётся переставить стоп в безубыток
    idx = pd.date_range("2026-10-10", periods=80, freq="h", tz="UTC")
    df = pd.DataFrame({"open": 104.0, "high": 105.0, "low": 103.0, "close": 104.0, "volume": 1.0}, index=idx)
    now = dt.datetime(2026, 10, 12, 8, 0, tzinfo=dt.timezone.utc)
    sent = []

    class Cl:
        def price(self, s): return 104.2
    m = manage.Manager(b, j, Cl(), sent.append, candles_fn=lambda a: df, now_fn=lambda: now)
    out = m.run()
    assert out and "стоп" in out[0] and len(sent) == 1 and "поднят" in sent[0]
    m.run()
    assert len(sent) == 1                                                # повторного спама нет
