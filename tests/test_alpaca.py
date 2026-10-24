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

    tick, min_order = "0.000000001", "0"

    def asset_info(self, symbol):
        return {"price_increment": self.tick, "min_order_size": self.min_order}

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
    t = j.q("SELECT exit_reason, pnl FROM trades")                       # круг «купили-продали» виден в журнале (комиссии отдела)
    assert t and t[0]["exit_reason"] == "no_stop" and len(j.q("SELECT 1 FROM fills")) == 2


def test_micro_price_stop_limit_has_nonzero_price():
    from office.alpaca import px_str, stop_limit_price
    assert px_str(82125.8) == "82125.8000" and px_str(0.0000123) == "0.000012300" and float(px_str(0.0000123)) > 0
    lim = stop_limit_price(0.0000118, 0.0000125)                       # BONK-подобная цена: раньше «0.0000» → Alpaca: limit price must be > 0
    assert float(px_str(lim)) > 0 and float(px_str(0.0000118)) > 0
    b, c, j = mk()
    b.buy("BTC/USD", 10, 0.0000125, 0.0000118, 0.0000140, "t")
    s = c.submitted[1]
    assert s["type"] == "stop_limit" and float(s["stop_price"]) > 0 and 0 < float(s["limit_price"]) < float(s["stop_price"])


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


LIVE = [{"symbol": "BTCUSD", "market_value": "1000"}]


def test_live_crypto_position_without_stop_order_gets_stop_reinstalled():
    b, c, j = mk()
    b.buy("BTC/USD", 10, 100.0, 95.0, 110.0, "t0")
    c._pos, c._orders = LIVE, []                      # заявка стопа пропала
    n = len(c.submitted)
    assert b.sync({"BTC/USD": 100.0}, "t1") == []
    s = c.submitted[n]
    assert s["type"] == "stop_limit" and s["side"] == "sell" and float(s["stop_price"]) == 95.0 and "BTC/USD" in j.positions()


def test_live_crypto_position_with_stop_order_is_left_alone():
    b, c, j = mk()
    b.buy("BTC/USD", 10, 100.0, 95.0, 110.0, "t0")
    c._pos, c._orders = LIVE, [{"id": "s1", "side": "sell", "type": "stop_limit", "qty": "9.975"}]
    n = len(c.submitted)
    b.sync({"BTC/USD": 100.0}, "t1")
    assert len(c.submitted) == n


def test_take_touched_between_passes_sells_at_market_and_cancels_stop_order():
    b, c, j = mk()
    b.buy("BTC/USD", 10, 100.0, 95.0, 110.0, "t0")
    c._pos, c._orders = LIVE, [{"id": "s1", "side": "sell", "type": "stop_limit", "qty": "9.975"}]
    c.order = lambda oid: {"id": oid, "status": "filled", "filled_avg_price": "108", "filled_qty": "9.975"}
    ev = b.sync({"BTC/USD": 108.0}, "t1", {"BTC/USD": 111.0})       # цена на проходе 108 < тейка 110, но максимум свечей 111 ≥ тейка
    assert ev[0][1]["reason"] == "take" and "s1" in c.cancelled and not j.positions()


def test_take_touch_below_entry_costs_does_not_sell():
    b, c, j = mk()
    b.buy("BTC/USD", 10, 100.0, 95.0, 110.0, "t0")
    c._pos, c._orders = LIVE, [{"id": "s1", "side": "sell", "type": "stop_limit", "qty": "9.975"}]
    assert b.sync({"BTC/USD": 100.1}, "t1", {"BTC/USD": 111.0}) == [] and j.positions()


def test_stop_limit_gap_unfilled_order_is_replaced_by_market_sell():
    b, c, j = mk()
    b.buy("BTC/USD", 10, 100.0, 95.0, 110.0, "t0")
    c._pos, c._orders = LIVE, [{"id": "s1", "side": "sell", "type": "stop_limit", "qty": "9.975"}]
    c.order = lambda oid: {"id": oid, "status": "filled", "filled_avg_price": "93", "filled_qty": "9.975"}
    ev = b.sync({"BTC/USD": 93.0}, "t1")                           # цена 93 ниже лимита стопа 94,05: stop_limit не исполнился
    assert ev[0][1]["reason"] == "stop" and "s1" in c.cancelled and not j.positions()


# ---- шаг цены пары (price_increment из Alpaca): BONK / SHIB / PEPE ----
MICRO = {"BONK/USD": (0.0000118, 0.0000125), "SHIB/USD": (0.0000082, 0.0000088), "PEPE/USD": (0.0000037, 0.0000040)}   # (стоп, цена входа)


@pytest.mark.parametrize("sym", list(MICRO))
def test_micro_priced_pairs_get_valid_stop_and_limit_on_the_tick(sym):
    from decimal import Decimal
    from office.alpaca import protective_prices
    stop, entry = MICRO[sym]
    s, lim = protective_prices(stop, entry, "0.000000001")
    assert Decimal(s) > 0 and Decimal(lim) > 0 and Decimal(lim) < Decimal(s)                 # раньше «0.0000» → 422 limit price must be > 0
    assert Decimal(s) % Decimal("0.000000001") == 0 and Decimal(lim) % Decimal("0.000000001") == 0   # кратно шагу цены пары
    assert abs(float(s) - stop) / stop < 0.001 and (float(s) - float(lim)) / float(s) <= 0.011       # стоп почти не сдвинут, лимит не дальше 1%


@pytest.mark.parametrize("sym", list(MICRO))
def test_micro_pair_entry_places_valid_protective_order(sym):
    asset = next((a for a in ASSETS if a.name == sym), None) or __import__("office.config", fromlist=["x"]).make_asset(sym)
    j, c = Journal(), FakeClient()
    stop, entry = MICRO[sym]
    b = AlpacaBroker(c, j, {sym: asset})
    b.buy(sym, 1_000_000.0, entry, stop, entry * 1.1, "t")
    so = c.submitted[1]
    assert so["type"] == "stop_limit" and float(so["stop_price"]) > 0 and 0 < float(so["limit_price"]) < float(so["stop_price"]) and so["stop_price"].count(".") == 1 and "e" not in so["stop_price"]


def test_invalid_protection_means_no_purchase_at_all():
    from office.config import make_asset
    asset = make_asset("BONK/USD")
    for tick, min_order, qty, why in (("0.0001", "0", 1e9, "слишком грубый"),                # шаг 0,0001 при цене 0,0000118 → стоп обнулился бы
                                      ("0", "0", 1e9, "не вернула шаг цены"),
                                      ("0.000000001", "353356.89", 100_000.0, "меньше минимального")):
        j, c = Journal(), FakeClient()
        c.tick, c.min_order = tick, min_order
        b = AlpacaBroker(c, j, {"BONK/USD": asset})
        with pytest.raises(AlpacaError, match=why):
            b.buy("BONK/USD", qty, 0.0000125, 0.0000118, 0.000014, "t")
        assert not c.submitted and not j.positions()                                       # ни рыночного ордера, ни позиции: купить нечего без защиты


def test_validate_entry_ok_and_stocks_skip():
    from office.config import make_asset
    j, c = Journal(), FakeClient()
    c.min_order = "353356.89"
    b = AlpacaBroker(c, j, {"BONK/USD": make_asset("BONK/USD"), "AAPL": make_asset("AAPL")})
    b.validate_entry("BONK/USD", 1_700_000_000.0, 0.0000125, 0.0000118)
    b.validate_entry("AAPL", 1, 100.0, 95.0)                                                # акции — bracket, шаг цены не нужен


def test_existing_stop_limit_reinstalled_and_tightened_on_the_tick():
    from office.config import make_asset
    j, c = Journal(), FakeClient()
    b = AlpacaBroker(c, j, {"PEPE/USD": make_asset("PEPE/USD")})
    j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts) VALUES('PEPE/USD',1000000,0.0000040,0.0000037,0.0000048,0,0,'t')")
    c._pos, c._orders = [{"symbol": "PEPEUSD", "market_value": "100"}], []
    assert b.ensure_protection("PEPE/USD") == "reinstalled"
    so = c.submitted[-1]
    assert float(so["stop_price"]) == pytest.approx(0.0000037) and 0 < float(so["limit_price"]) < float(so["stop_price"])


# ── v5.8.1: продажа снимает заявки, возвращает стоп при неудаче, не шлёт рыночный приказ при закрытой бирже ──
class SellClient(FakeClient):
    """Открытые заявки живут, пока их не снимут; position().qty_available освобождается только после снятия."""
    def __init__(self, orders, closed=False, fill=True, fail_reinstall=False, **kw):
        super().__init__(fill=fill, **kw)
        self.open_orders, self.closed, self.fail_reinstall, self.log = list(orders), closed, fail_reinstall, []

    def clock(self):
        return {"is_open": not self.closed}

    def orders(self, **kw):
        return list(self.open_orders)

    def cancel(self, oid):
        self.log.append(("cancel", oid))
        self.open_orders = [o for o in self.open_orders if o["id"] != oid]

    def submit(self, b):
        self.log.append(("submit", b.get("type"), b.get("order_class")))
        if self.fail_reinstall and (b.get("order_class") == "oco" or b.get("type") == "stop_limit") and any(x[0] == "submit" and x[1] == "market" for x in self.log):
            raise AlpacaError("reject")
        return super().submit(b)

    def order(self, oid):
        if self.fill:
            return super().order(oid)
        return {"id": oid, "status": "accepted", "filled_avg_price": None, "filled_qty": "0"}

    def position(self, symbol):
        q = "10" if not self.open_orders else "0"
        return {"qty": "10", "qty_available": q}


def open_stock(c):
    j = Journal()
    j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts) VALUES('MSFT',10,100,95,110,0,1,'t')")
    return AlpacaBroker(c, j, ASSETS), j


OCO = [{"id": "s1", "side": "sell", "type": "stop", "qty": "10"}, {"id": "t1", "side": "sell", "type": "limit", "qty": "10"}]


def test_sell_cancels_stop_and_limit_orders_before_market_order():
    c = SellClient(OCO)
    b, j = open_stock(c)
    r = b.sell("MSFT", 100.0, "t", "news")
    kinds = [x[0] + ":" + str(x[1]) for x in c.log]
    assert kinds.index("cancel:s1") < kinds.index("submit:market") and kinds.index("cancel:t1") < kinds.index("submit:market")
    assert c.open_orders == [] and "MSFT" not in j.positions() and r["reason"] == "news"
    assert c.submitted[-1]["qty"] == "10"                                        # количество освободилось от заявок


@pytest.fixture
def quick_fill(monkeypatch):
    """Ожидание исполнения в тестах не тянем 30 секунд: берём текущее состояние заявки."""
    monkeypatch.setattr(AlpacaBroker, "_wait_fill", lambda self, oid, timeout=0: self.c.order(oid))


def test_failed_sell_cancels_hanging_order_and_restores_stop(quick_fill):
    c = SellClient(OCO, fill=False)
    b, j = open_stock(c)
    with pytest.raises(AlpacaError) as e:
        b.sell("MSFT", 100.0, "t", "news")
    assert "стоп возвращён на место" in str(e.value) and "accepted" in str(e.value)
    kinds = [x[0] + ":" + str(x[1]) for x in c.log]
    assert "cancel:o1" in kinds                                                  # зависший рыночный приказ снят
    assert kinds[-1] == "submit:limit" and c.log[-1][2] == "oco"                 # акции: OCO (стоп + цель) на прежних уровнях
    oco = c.submitted[-1]
    assert oco["stop_loss"]["stop_price"] == "95.00" and oco["take_profit"]["limit_price"] == "110.00"
    assert "MSFT" in j.positions()                                               # позиция в журнале осталась


def test_failed_sell_and_failed_restore_is_loud(quick_fill):
    c = SellClient(OCO, fill=False, fail_reinstall=True)
    b, j = open_stock(c)
    with pytest.raises(AlpacaError) as e:
        b.sell("MSFT", 100.0, "t", "news")
    assert "ПРОВЕРЬТЕ ПОЗИЦИЮ ВРУЧНУЮ" in str(e.value)


def test_stock_sell_with_closed_market_touches_nothing():
    from office.alpaca import MarketClosed
    c = SellClient(OCO, closed=True)
    b, j = open_stock(c)
    with pytest.raises(MarketClosed):
        b.sell("MSFT", 100.0, "t", "news")
    assert c.log == [] and len(c.open_orders) == 2 and "MSFT" in j.positions()   # стоп и цель на месте, приказов нет


def test_crypto_sell_is_not_blocked_by_stock_hours():
    c = SellClient([{"id": "s1", "side": "sell", "type": "stop_limit", "qty": "10"}], closed=True)
    j = Journal()
    j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts) VALUES('BTC/USD',10,100,95,110,0,1,'t')")
    b = AlpacaBroker(c, j, ASSETS)
    assert b.sell("BTC/USD", 100.0, "t", "news")["reason"] == "news"


# ── v5.8.2: проверка «у каждой позиции есть стоп (и цель) на бирже» ───────────────
class ProtClient(SellClient):
    def __init__(self, orders, **kw):
        super().__init__(orders, **kw)
        self.patched = []

    def _req(self, method, path, **kw):
        self.patched.append((method, path, kw.get("json")))
        return {"id": "new-id"}


BRACKET = [{"id": "t1", "side": "sell", "type": "limit", "status": "new", "limit_price": "110", "qty": "10", "order_class": "bracket"},
           {"id": "s1", "side": "sell", "type": "stop", "status": "held", "stop_price": "95", "qty": "10", "order_class": "bracket"}]
STRAY = [{"id": "m1", "side": "sell", "type": "market", "status": "accepted", "qty": "10"}]


def test_protection_ok_with_held_stop_leg():
    b, j = open_stock(ProtClient(BRACKET))
    assert b.verify_protection("MSFT", market_open=False) is None                 # held-нога стопа видна, цель на месте


def test_stray_market_sell_replaced_by_oco_at_saved_levels():
    c = ProtClient(STRAY, closed=True)
    b, j = open_stock(c)
    r = b.verify_protection("MSFT", market_open=False)
    assert r["fixed"] and r["error"] is None and r["missing"] == ["стоп", "цель"] and r["stray_market"] == 1
    assert ("cancel", "m1") in c.log
    oco = c.submitted[-1]
    assert oco["order_class"] == "oco" and oco["stop_loss"]["stop_price"] == "95.00" and oco["take_profit"]["limit_price"] == "110.00" and oco["qty"] == "10"


def test_market_order_during_open_session_is_left_alone():
    c = ProtClient(STRAY)
    b, j = open_stock(c)
    assert b.verify_protection("MSFT", market_open=True) is None and c.log == []   # исполняется прямо сейчас — не вмешиваемся


def test_only_take_missing_rebuilds_both_legs():
    c = ProtClient([BRACKET[1]])
    b, j = open_stock(c)
    r = b.verify_protection("MSFT", market_open=False)
    assert r["missing"] == ["цель"] and r["fixed"] and c.submitted[-1]["order_class"] == "oco"


def test_crypto_needs_only_stop_limit():
    c = ProtClient([])
    j = Journal()
    j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts) VALUES('BTC/USD',10,100,95,110,0,1,'t')")
    b = AlpacaBroker(c, j, ASSETS)
    r = b.verify_protection("BTC/USD")
    assert r["missing"] == ["стоп"] and r["fixed"] and c.submitted[-1]["type"] == "stop_limit"
    c2 = ProtClient([{"id": "s", "side": "sell", "type": "stop_limit", "status": "new", "stop_price": "95", "limit_price": "94.5", "qty": "10"}])
    b2 = AlpacaBroker(c2, j, ASSETS)
    assert b2.verify_protection("BTC/USD") is None                                 # у крипты цели на бирже нет и не требуется


def test_stop_price_mismatch_is_patched():
    stale = [BRACKET[0], {**BRACKET[1], "stop_price": "90"}]
    c = ProtClient(stale)
    b, j = open_stock(c)
    r = b.verify_protection("MSFT", market_open=False)
    assert r["fixed"] and r["mismatch"] and not r["missing"]
    assert c.patched == [("PATCH", "/orders/s1", {"stop_price": "95.00"})]


def test_dry_run_changes_nothing_and_failures_are_reported_not_raised():
    c = ProtClient(STRAY, closed=True)
    b, j = open_stock(c)
    r = b.verify_protection("MSFT", market_open=False, fix=False)
    assert r["missing"] and not r["fixed"] and c.log == []
    c2 = ProtClient(STRAY, closed=True, fail_reinstall=True)
    b2, j2 = open_stock(c2)
    c2.log.append(("submit", "market", None))                                      # включаем режим «установка защиты отклоняется»
    r2 = b2.verify_protection("MSFT", market_open=False)
    assert not r2["fixed"] and "reject" in r2["error"]
