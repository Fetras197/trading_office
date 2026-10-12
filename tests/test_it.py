import datetime as dt

from office import it, llm, realmoney
from office.board import Board
from office.journal import Journal


def mk(**kw):
    sent, local, clock = [], [], {"t": 1_000_000.0}
    d = dict(battery_fn=lambda: {"pct": 50, "plugged": False}, net_fn=lambda: True, lab_alive_fn=lambda: True, lab_restart_fn=lambda: sent.append("RESTART"),
             pressure_fn=lambda: {"claude": False, "gemini": False})
    d.update(kw)
    dept = it.ITDept(Journal(), sent.append, now_fn=lambda: clock["t"], local_fn=local.append, **d)
    return dept, sent, local, clock


def test_battery_alert_only_below_20_unplugged_with_antispam():
    bat = {"v": {"pct": 18, "plugged": False}}
    d, sent, local, clk = mk(battery_fn=lambda: bat["v"])
    d.check_battery()
    d.check_battery()
    assert len(sent) == 1 and "18%" in sent[0] and local                    # второй раз подряд — тишина
    clk["t"] += 3 * 3600 + 1
    d.check_battery()
    assert len(sent) == 2
    bat["v"] = {"pct": 15, "plugged": True}
    clk["t"] += 10 * 3600
    d.check_battery()
    assert len(sent) == 2                                                    # на зарядке — не тревожим
    bat["v"] = None
    d.check_battery()
    assert "Нет данных о заряде" in sent[-1]


def test_internet_outage_reported_after_recovery_only_if_long():
    net = {"ok": False}
    d, sent, local, clk = mk(net_fn=lambda: net["ok"])
    d.check_net()
    clk["t"] += 660
    d.check_net()
    assert local and not sent                                                # офлайн: только локальное уведомление
    clk["t"] += 300
    net["ok"] = True
    d.check_net()
    assert len(sent) == 1 and "Интернет пропадал" in sent[0] and "16 мин" in sent[0]
    net["ok"] = False
    d.check_net()
    clk["t"] += 120
    net["ok"] = True
    d.check_net()
    assert len(sent) == 1                                                    # короткий сбой не шумит


def test_lab_restart_with_cooldown_and_no_lab_flag(tmp_path, monkeypatch):
    from office import config
    monkeypatch.setattr(config, "ROOT", tmp_path)
    d, sent, local, clk = mk(lab_alive_fn=lambda: False)
    d.check_lab()
    d.check_lab()
    assert sent.count("RESTART") == 1 and any("перезапущена" in x for x in sent)
    (tmp_path / "NO_LAB").touch()
    clk["t"] += 4000
    d.check_lab()
    assert sent.count("RESTART") == 1                                        # NO_LAB: не перезапускаем


def test_economy_mode_on_and_off_with_hysteresis():
    p = {"v": {"claude": True, "gemini": False}}
    d, sent, local, clk = mk(pressure_fn=lambda: p["v"])
    d.check_limits()
    assert d.economy and "Режим экономии" in sent[0] and "Claude" in sent[0]
    p["v"] = {"claude": False, "gemini": False}
    clk["t"] += 600
    d.check_limits()
    assert d.economy                                                         # слишком рано снимать
    clk["t"] += 3600
    d.check_limits()
    assert not d.economy and "выключен" in sent[-1]


def test_llm_pressure_counts_recent_failures_vs_successes():
    llm._EVENTS["gemini"].clear()
    for _ in range(3):
        llm.mark("gemini", False)
    assert llm.pressure()["gemini"] and not llm.pressure()["claude"]
    for _ in range(5):
        llm.mark("gemini", True)
    assert not llm.pressure()["gemini"]
    llm._EVENTS["gemini"].clear()


def test_start_reports_downtime(tmp_path, monkeypatch):
    monkeypatch.setattr(it, "HEARTBEAT", tmp_path / "hb")
    d, sent, local, clk = mk()
    d.on_start()
    assert not sent                                                          # первого сердцебиения ещё не было
    d.heartbeat()
    clk["t"] += 120
    d.on_start()
    assert not sent
    clk["t"] += 1800
    d.on_start()
    assert "не работал 32 мин" in sent[0]


# ---------- реальные деньги ----------
def seed(b, days, office, spy, btc, dd_dip=None):
    t0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    for i in range(days + 1):
        ts = (t0 + dt.timedelta(days=i)).isoformat(timespec="seconds")
        f = i / max(days, 1)
        o = 100000 * (1 + office * f)
        if dd_dip and i == days // 2:
            o *= 1 - dd_dip
        for name, v in (("office", o), ("spy", 100000 * (1 + spy * f)), ("btc", 100000 * (1 + btc * f))):
            b.x("INSERT INTO shadow_eq(ts,name,equity,cash) VALUES(?,?,?,0)", (ts, name, v))
    return t0 + dt.timedelta(days=days)


def add_trades(j, n):
    for i in range(n):
        j._x("INSERT INTO trades(asset,entry_ts,exit_ts,entry,exit,qty,fees,pnl,exit_reason) VALUES('X','a','b',1,1,1,0,10,'take')")
    j._x("INSERT INTO trades(asset,entry_ts,exit_ts,entry,exit,qty,fees,pnl,exit_reason) VALUES('X','a','b',1,1,1,0,5,'partial')")


def test_realmoney_all_conditions_and_message_once_a_week():
    b, j = Board(), Journal()
    now = seed(b, 31, 0.05, 0.02, -0.03)
    add_trades(j, 20)
    sent = []
    p = realmoney.check(b, j, sent.append, j, now)
    assert p["all_ok"] and len(sent) == 1 and "реальные деньги" in sent[0] and "Решение принимаете вы" in sent[0] and "реальную торговлю сам не включит" in sent[0]
    realmoney.check(b, j, sent.append, j, now + dt.timedelta(days=1))
    assert len(sent) == 1
    realmoney.check(b, j, sent.append, j, now + dt.timedelta(days=8))
    assert len(sent) == 2


def test_realmoney_each_condition_blocks():
    for kw, n_tr, days in (({"office": 0.05, "spy": 0.02, "btc": -0.03}, 19, 31),        # мало сделок (частичная фиксация не считается)
                           ({"office": 0.05, "spy": 0.02, "btc": -0.03}, 25, 20),        # мало дней
                           ({"office": 0.01, "spy": 0.02, "btc": -0.03}, 25, 31),        # хуже S&P 500
                           ({"office": 0.05, "spy": 0.02, "btc": 0.09}, 25, 31),         # хуже биткоина
                           ({"office": 0.05, "spy": 0.02, "btc": -0.03, "dd_dip": 0.12}, 25, 31)):   # просадка ≥ 10%
        b, j = Board(), Journal()
        now = seed(b, days, **kw)
        add_trades(j, n_tr)
        sent = []
        assert not realmoney.check(b, j, sent.append, j, now)["all_ok"] and not sent
    assert "наблюдение только началось" in realmoney.progress_line(realmoney.progress(Board(), Journal()))
