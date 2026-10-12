import pytest

from office.alpaca import AlpacaBroker, AlpacaClient, AlpacaError
from office.config import ASSETS
from office.journal import Journal


class FakeClient:
    def __init__(self, cash=100_000, fail_stop=False, fill=True, avail="9.975"):
        self.cash_v, self.fail_stop, self.fill, self.avail = cash, fail_stop, fill, avail  # avail: остаток после комиссии в активе
        self.submitted, self.cancelled, self._pos, self._orders = [], [], [], []

    def account(self):
        return {"cash": str(self.cash_v), "equity": str(self.cash_v)}

    def submit(self, b):
        self.submitted.append(b)
        if b.get("type") == "stop_limit" and self.fail_stop:
            raise AlpacaError("stop rejected")
        return {"id": f"o{len(self.submitted)}", "status": "new"}

    def order(self, oid):
        return {"id": oid, "status": "filled" if self.fill else "new", "filled_avg_price": "100.5", "filled_qty": "10" if self.fill else "0"}

    def cancel(self, oid):
        self.cancelled.append(oid)

    def orders(self, **kw):
        return self._orders

    def positions(self):
        return self._pos

    def position(self, symbol):
        return {"qty_available": self.avail} if self.avail is not None else None


def mk(**kw):
    j = Journal()
    c = FakeClient(**kw)
    return AlpacaBroker(c, j, ASSETS), c, j


def test_client_refuses_live_host():
    with pytest.raises(AlpacaError):
        AlpacaClient("k", "s", base="https://api.alpaca.markets/v2")


def test_client_needs_keys(monkeypatch):
    for k in ("ALPACA_KEY_ID", "ALPACA_SECRET", "ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(AlpacaError):
        AlpacaClient()


def test_stock_entry_is_real_bracket_with_whole_shares():
    b, c, j = mk()
    b.buy("AAPL", 10, 100.0, 95.0, 110.0, "t")
    o = c.submitted[0]
    assert o["order_class"] == "bracket" and o["side"] == "buy" and o["qty"] == "10"
    assert o["stop_loss"]["stop_price"] == "95.00" and o["take_profit"]["limit_price"] == "110.00"
    assert "AAPL" in j.positions()


def test_crypto_entry_places_protective_stop_limit():
    b, c, j = mk()
    b.buy("BTC/USD", 10, 100.0, 95.0, 110.0, "t")
    assert c.submitted[0]["type"] == "market" and "order_class" not in c.submitted[0]
    s = c.submitted[1]
    assert s["type"] == "stop_limit" and s["side"] == "sell" and float(s["limit_price"]) < float(s["stop_price"])
    assert s["qty"] == "9.975000"  # реальный остаток после комиссии, а не filled_qty=10
    pos = j.positions()["BTC/USD"]
    assert pos["qty"] == 9.975 and pos["fees"] == pytest.approx(0.025 * 100.5)


def test_crypto_pnl_includes_fees():
    b, c, j = mk()
    b.buy("BTC/USD", 10, 100.0, 95.0, 110.0, "t0")
    c.order = lambda oid: {"id": oid, "status": "filled", "filled_avg_price": "100.5", "filled_qty": "9.975"}
    ev = b.sell("BTC/USD", 100.5, "t1")
    assert ev["pnl"] < 0  # цена не изменилась → убыток = комиссии


def test_crypto_stop_failure_flattens_position():
    b, c, j = mk(fail_stop=True)
    with pytest.raises(AlpacaError, match="немедленно закрыта"):
        b.buy("BTC/USD", 10, 100.0, 95.0, 110.0, "t")
    assert c.submitted[-1]["side"] == "sell" and c.submitted[-1]["type"] == "market"
    assert not j.positions()


def test_crypto_stop_failure_and_unconfirmed_close_keeps_position_tracked(monkeypatch):
    monkeypatch.setattr("office.alpaca.time.sleep", lambda s: None)
    b, c, j = mk(fail_stop=True)
    orig = c.order
    c.order = lambda oid: orig(oid) if oid == "o1" else {"id": oid, "status": "new"}  # вход исполнен, закрытие — нет
    monkeypatch.setattr("office.alpaca.time.time", iter(range(0, 100_000, 40)).__next__)
    with pytest.raises(AlpacaError, match="СТОП НЕ ВСТАЛ"):
        b.buy("BTC/USD", 10, 100.0, 95.0, 110.0, "t")
    assert "BTC/USD" in j.positions()  # позиция не «потеряна»


def test_no_leverage_cash_check():
    b, c, _ = mk(cash=500)
    with pytest.raises(AlpacaError, match="cash"):
        b.buy("AAPL", 10, 100.0, 95.0, 110.0, "t")
    assert not c.submitted


def test_unfilled_entry_cancelled_and_not_recorded(monkeypatch):
    monkeypatch.setattr("office.alpaca.time.sleep", lambda s: None)
    monkeypatch.setattr("office.alpaca.time.time", iter(range(0, 10_000, 40)).__next__)
    b, c, j = mk(fill=False)
    with pytest.raises(AlpacaError, match="не исполнен"):
        b.buy("AAPL", 10, 100.0, 95.0, 110.0, "t")
    assert c.cancelled and not j.positions()


def test_only_buy_entries_never_short():
    b, c, _ = mk()
    b.buy("AAPL", 10, 100.0, 95.0, 110.0, "t")
    assert all(o["side"] == "buy" for o in c.submitted)
    with pytest.raises(AlpacaError):
        b.sell("MSFT", 100.0, "t")  # нет позиции → шорт невозможен


def test_sync_books_stop_hit():
    b, c, j = mk()
    b.buy("AAPL", 10, 100.0, 95.0, 110.0, "t0")
    c._pos = []  # на брокере позиции нет — сработал стоп
    c._orders = [{"side": "sell", "status": "filled", "filled_avg_price": "94.9"}]
    ev = b.sync({"AAPL": 94.9}, "t1")
    assert ev[0][1]["reason"] == "stop" and not j.positions()
    assert j.q("SELECT pnl FROM trades")[0]["pnl"] < 0


def test_sync_crypto_take_profit_sells():
    b, c, j = mk()
    b.buy("BTC/USD", 10, 100.0, 95.0, 110.0, "t0")
    c._pos = [{"symbol": "BTCUSD"}]
    c.order = lambda oid: {"id": oid, "status": "filled", "filled_avg_price": "111", "filled_qty": "10"}
    ev = b.sync({"BTC/USD": 111.0}, "t1")
    assert ev[0][1]["reason"] == "take" and not j.positions()


def test_fill_during_cancel_is_flagged_not_silently_lost(monkeypatch):
    monkeypatch.setattr("office.alpaca.time.sleep", lambda s: None)
    monkeypatch.setattr("office.alpaca.time.time", iter(range(0, 10_000, 40)).__next__)
    b, c, j = mk(fill=False)
    c.cancel = lambda oid: setattr(c, "fill", True)  # ордер исполнился в момент отмены
    with pytest.raises(AlpacaError, match="в момент отмены"):
        b.buy("AAPL", 10, 100.0, 95.0, 110.0, "t")


def test_sync_ignores_dust_and_books_offline_close_with_broker_time():
    b, c, j = mk()
    b.buy("AAPL", 10, 100.0, 95.0, 110.0, "2026-10-12T07:00:00+00:00")
    c._pos = [{"symbol": "AAPL", "market_value": "0.0000009"}]   # пыль после продажи — не позиция
    c._orders = [{"side": "sell", "status": "filled", "filled_avg_price": "94.9", "filled_at": "2026-10-12T09:00:00Z"}]
    ev = b.sync({}, "2026-10-12T12:00:00+00:00")
    assert ev[0][1]["offline"] and ev[0][1]["reason"] == "stop" and not j.positions()
    assert j.q("SELECT exit_ts, exit FROM trades")[0] == {"exit_ts": "2026-10-12T09:00:00+00:00", "exit": 94.9}


def test_orphans_skip_dust():
    b, c, j = mk()
    c._pos = [{"symbol": "ONDOUSD", "market_value": "0.0000009"}, {"symbol": "NVDA", "market_value": "500"}]
    assert b.orphans() == ["NVDA"]
