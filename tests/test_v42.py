"""Правки 18.10 (вечер): причина перерасхода риска BTC, заморозка правил, время по Минску."""
import pytest

from office import alpaca, proofread, report, rules, views
from office.config import COSTS
from office.journal import Journal
from office.risk import RiskGate

# Реальная сделка BTC 18.10: вход 82 125,81, стоп 81 247,54, qty 0,148637, исполнение стопа 81 232,4 (по заявке stop_limit на бирже), результат −193,57 $ с комиссиями.
ENTRY, STOP, QTY, FILL, PNL = 82_125.813, 81_247.535, 0.148637, 81_232.4, -193.57


def test_btc_loss_explained_by_fees_not_by_stop_slippage():
    price_loss = (ENTRY - FILL) * QTY                                    # 132,8 $ — потеря на цене
    slippage_vs_stop = (STOP - FILL) * QTY                               # 2,2 $ — проскальзывание заявки stop_limit
    assert price_loss == pytest.approx(132.8, abs=0.2) and slippage_vs_stop == pytest.approx(2.2, abs=0.2)
    planned_old = (ENTRY - STOP) * QTY                                   # то, что показывали в сообщении: 130,5 $ — без комиссий
    planned_all_in = RiskGate.planned_risk(COSTS["crypto"], ENTRY, STOP, QTY)
    assert planned_old == pytest.approx(130.5, abs=0.2) and planned_all_in > 195      # с комиссиями 0,25%×2 и проскальзыванием 0,05%×2
    assert -PNL <= planned_all_in * 1.10                                              # факт в пределах заложенного +10%


def test_stop_limit_offset_never_adds_more_than_10_percent_of_risk():
    for entry, stop in ((ENTRY, STOP), (100.0, 98.0), (100.0, 90.0), (3000.0, 2990.0)):
        lim = alpaca.stop_limit_price(stop, entry)
        extra = (stop - lim) / (entry * (1 + COSTS["crypto"].fee) - stop * (1 - COSTS["crypto"].fee))     # худшее проскальзывание лимита / заложенная дистанция
        assert 0 < extra <= 0.105 or (stop - lim) / stop <= 0.0005 + 1e-9, (entry, stop, extra)
    assert alpaca.stop_limit_price(STOP, ENTRY) == pytest.approx(STOP * (1 - 0.001567), rel=1e-3)
    assert alpaca.stop_limit_price(95.0, 100.0) > 95.0 * 0.99                         # раньше было фиксированные −1%


def test_trade_message_shows_all_in_risk():
    from types import SimpleNamespace
    from office.board import Board
    res = SimpleNamespace(status="opened", asset="BTC/USD", cycle_id="c", order={"price": ENTRY, "qty": QTY, "stop": STOP, "take": 85_000.0, "equity": 98_000})
    m = report.trade_message(Board(), res)
    assert "риск с комиссиями: 203" in m.replace(" ", "")


def test_close_message_compares_loss_with_planned_risk():
    planned = RiskGate.planned_risk(COSTS["crypto"], ENTRY, STOP, QTY)
    ok = report.close_message("BTC/USD", PNL, -0.016, "stop", planned_risk=planned)
    assert "в пределах заложенного" in ok and "Заложенный риск с комиссиями" in ok
    bad = report.close_message("BTC/USD", -(planned * 1.4), -0.02, "stop", planned_risk=planned)
    assert "больше заложенного" in bad
    assert "Заложенный" not in report.close_message("BTC/USD", 50.0, 0.01, "take", planned_risk=planned)


def test_rules_fingerprint_pinned_and_freeze_counter():
    # Изменить правила входа/размера/блокировок можно только осознанно: обновить PINNED и сбросить счёт сделок (rules.register сделает это сам и сообщит владельцу).
    PINNED = "14498830b459"
    assert rules.fingerprint() == PINNED, f"правила изменились: {rules.fingerprint()} — счёт {rules.FREEZE_MIN_TRADES} сделок начнётся заново"
    j, sent = Journal(), []
    assert rules.register(j, sent.append, "2026-10-18T12:00:00+00:00") is False and not sent
    for i in range(3):
        j._x("INSERT INTO trades(asset,entry_ts,exit_ts,entry,exit,qty,fees,pnl,r_mult,run_id,exit_reason) VALUES('X','t',?,1,1,1,0,1,1,0,?)", (f"2026-10-19T0{i}:00:00+00:00", "stop"))
    j._x("INSERT INTO trades(asset,entry_ts,exit_ts,entry,exit,qty,fees,pnl,r_mult,run_id,exit_reason) VALUES('X','t','2026-10-19T09:00:00+00:00',1,1,1,0,1,1,0,'partial')")
    j._x("INSERT INTO trades(asset,entry_ts,exit_ts,entry,exit,qty,fees,pnl,r_mult,run_id,exit_reason) VALUES('X','t','2026-10-01T09:00:00+00:00',1,1,1,0,1,1,0,'stop')")   # до начала счёта
    st = rules.status(j)
    assert st["closed"] == 3 and st["frozen"] and "3 из 20" in rules.line(st) and "только записываются" in rules.line(st)
    j.kv_set("rules_fp", "old")                                                        # «правки в коде»: отпечаток другой → счёт заново и сообщение
    assert rules.register(j, sent.append, "2026-10-20T00:00:00+00:00") is True and "изменились" in sent[0] and rules.status(j)["closed"] == 0


def test_lessons_never_feed_back_into_rules():
    """Автоподстройки нет: задания облака не содержат weights/labtrial, зал не импортирует review/weights, уроки только в memory."""
    import inspect
    from office import hall
    from office.cloud import Office
    from office.board import Board
    o = Office(board=Board(), journal=Journal(), client=type("C", (), {"price": lambda s, x: 1.0, "account_view": lambda s: {}})(), notify=lambda t: None, router=object())
    assert not {"weights", "labtrial", "lab"} & {j.name for j in o.sched.jobs}
    src = inspect.getsource(hall)
    assert "review" not in src and "weights" not in src and "lessons" not in src


def test_utc_times_in_outgoing_text_become_minsk():
    assert proofread.sanitize("CPI в 12:30 UTC, речь в 21:45 GMT, а в 23:30 (UTC) тихо") == "CPI в 15:30 по Минску, речь в 00:45 по Минску, а в 02:30 по Минску тихо"


def test_hall_ban_reason_is_in_minsk_time():
    import datetime as dt
    now = dt.datetime(2026, 10, 18, 7, 21, tzinfo=dt.timezone.utc)
    assert views.when_text("2026-10-19T07:21+00:00", now) == "завтра 10:21"


def test_existing_wide_stop_limit_is_tightened_to_10_percent_rule():
    from tests.test_alpaca import FakeClient, ASSETS
    c = FakeClient()
    patched = []
    c._req = lambda method, path, **kw: patched.append((method, path, kw["json"])) or {"id": "new1"}
    j = Journal()
    b = alpaca.AlpacaBroker(c, j, ASSETS)
    j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts) VALUES('BTC/USD',1,100,95,110,0,0,'t')")
    c._orders = [{"id": "s1", "side": "sell", "type": "stop_limit", "qty": "1", "stop_price": "95", "limit_price": "94.05"}]   # старый лимит −1%
    assert b.ensure_protection("BTC/USD") == "tightened"
    want = alpaca.stop_limit_price(95.0, 100.0)
    assert patched[0][0] == "PATCH" and float(patched[0][2]["limit_price"]) == pytest.approx(want, abs=1e-4) and want > 94.05
    c._orders[0]["limit_price"] = f"{want:.4f}"
    assert b.ensure_protection("BTC/USD") == "ok"
