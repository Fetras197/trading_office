from office.config import COSTS, Costs, Limits
from office.risk import RiskGate

G = RiskGate()
BASE = dict(kind="crypto", costs=COSTS["crypto"], entry=100.0, stop=98.0, atr=1.0, equity=100_000, day_start_equity=100_000,
            entries_today=0, open_positions=0, cash=100_000, has_position=False)


def chk(**kw):
    return G.check_entry(**{**BASE, **kw})


def test_risk_2pct_including_costs():
    v = chk()
    assert v.ok
    c = COSTS["crypto"]
    loss = v.qty * (100 * (1 + c.slippage) * (1 + c.fee) - 98 * (1 - c.slippage) * (1 - c.fee))
    assert loss <= 0.02 * 100_000 + 1e-6


def test_notional_cap_25pct():
    v = chk(stop=99.5, atr=0.5)  # узкий стоп → лимит по нотионалу
    assert v.ok and v.qty * 100 <= 0.25 * 100_000 * 1.0005


def test_cash_limits_no_leverage():
    v = chk(cash=1_000)
    assert v.ok and v.qty * 100 <= 1_000


def test_daily_stop():
    assert not chk(equity=94_900).ok and "дневной стоп" in chk(equity=94_900).reason
    assert chk(equity=95_100).ok


def test_max_trades_per_day():
    assert not chk(entries_today=5).ok
    assert chk(entries_today=4).ok


def test_max_positions():
    assert not chk(open_positions=3).ok
    assert chk(open_positions=2).ok


def test_stop_required_and_below_entry():
    assert not chk(stop=None).ok
    assert not chk(stop=101).ok
    assert not chk(stop=0).ok


def test_stop_distance_atr_range():
    assert not chk(stop=99.9).ok       # < 0.5 ATR
    assert not chk(stop=90, atr=1).ok  # > 4 ATR


def test_stocks_whole_shares():
    v = chk(kind="stock", costs=Costs(), entry=187.5, stop=185.0, atr=2.0)
    assert v.ok and v.qty == int(v.qty)


def test_size_mult_only_shrinks():
    a, b = chk(), chk(size_mult=0.5)
    assert abs(b.qty - a.qty * 0.5) < 1e-3
    assert chk(size_mult=5).qty == a.qty


def test_existing_position_rejected():
    assert not chk(has_position=True).ok


def test_below_min_notional():
    assert not chk(equity=100, cash=100).ok


def test_custom_limits():
    g = RiskGate(Limits(max_positions=1))
    assert not g.check_entry(**{**BASE, "open_positions": 1}).ok
