import pytest

from office.brains import Decision, Opinion
from office.broker import SimBroker
from office.config import BY_NAME, COSTS, Limits
from office.journal import Journal
from office.llm import LLMError
from office.office import Office
from office.risk import RiskGate

TS = "2026-03-02T12:00:00+00:00"
BUNDLE = {"asset": "BTC", "as_of": TS, "indicators_1h": {"close": 100.0, "atr14": 1.0}, "indicators_4h": {}}


class Brain:
    def __init__(self, d=None, err=None, pf=None):
        self.d, self.err, self.pf = d, err, pf

    def decide(self, b, a):
        if self.err:
            raise self.err
        return self.d

    def preflight(self, t):
        return self.pf


def mk(brain, approve=None, limits=Limits()):
    j = Journal()
    kinds = {n: a.kind for n, a in BY_NAME.items()}
    br = SimBroker(j, kinds, COSTS, 100_000)
    return Office(brain, br, j, gate=RiskGate(limits), approve=approve), j, br


BUY = Decision("buy", 100.0, 98.0, 104.0, 1.0, "ok", [Opinion("trader", "buy", 0.7, "r")])


def test_buy_executes_and_journals_opinion_and_decision():
    o, j, br = mk(Brain(BUY))
    r = o.step(BY_NAME["BTC"], BUNDLE, TS, {"BTC": 100.0})
    assert r.status == "opened" and br.position("BTC")
    assert j.q("SELECT role FROM opinions")[0]["role"] == "trader"
    assert j.q("SELECT executed, verdict FROM decisions")[0] == {"executed": 1, "verdict": "ok"}


def test_agent_error_skips_cycle_no_trade():
    o, j, br = mk(Brain(err=LLMError("битый JSON")))
    r = o.step(BY_NAME["BTC"], BUNDLE, TS, {"BTC": 100.0})
    assert r.status == "skipped" and not j.positions()
    assert j.q("SELECT summary FROM runs")[0]["summary"].startswith("SKIPPED")


def test_riskgate_rejects_missing_stop():
    o, j, _ = mk(Brain(Decision("buy", 100.0, None, 104.0)))
    assert o.step(BY_NAME["BTC"], BUNDLE, TS, {"BTC": 100.0}).status == "rejected" and not j.positions()


def test_approval_declined_and_timeout_do_not_trade():
    for answer in (False, None):
        o, j, _ = mk(Brain(BUY), approve=lambda *a, ans=answer: ans)
        assert o.step(BY_NAME["BTC"], BUNDLE, TS, {"BTC": 100.0}).status == "declined" and not j.positions()


def test_approval_granted_trades():
    o, j, _ = mk(Brain(BUY), approve=lambda *a: True)
    assert o.step(BY_NAME["BTC"], BUNDLE, TS, {"BTC": 100.0}).status == "opened"


def test_executor_preflight_veto():
    pf = Opinion("executor", "n/a", 0, "x", {"ticket_ok": False, "issues": ["stale"]})
    o, j, _ = mk(Brain(BUY, pf=pf))
    assert o.step(BY_NAME["BTC"], BUNDLE, TS, {"BTC": 100.0}).status == "rejected"


def test_max_positions_enforced_across_assets():
    o, j, br = mk(Brain(BUY))
    for n in ("BTC", "ETH", "SOL", "AAPL"):
        o.step(BY_NAME[n], {**BUNDLE, "asset": n}, TS, {k: 100.0 for k in ("BTC", "ETH", "SOL", "AAPL")})
    assert len(j.positions()) == 3


def test_max_5_entries_per_day():
    o, j, br = mk(Brain(BUY), limits=Limits(max_positions=99, max_notional=0.1))
    names = ["BTC", "ETH", "SOL", "AAPL", "MSFT", "NVDA", "SPY"]
    px = {n: 100.0 for n in names}
    res = [o.step(BY_NAME[n], BUNDLE, TS, px).status for n in names]
    assert res.count("opened") == 5 and res[-2:] == ["rejected", "rejected"]


def test_daily_stop_blocks_entries():
    o, j, br = mk(Brain(BUY))
    j.kv_set("day_start:2026-03-02", 200_000)  # эквити 100k против 200k на начало дня = −50%
    assert o.step(BY_NAME["BTC"], BUNDLE, TS, {"BTC": 100.0}).status == "rejected"


def test_sell_closes_position():
    o, j, br = mk(Brain(BUY))
    o.step(BY_NAME["BTC"], BUNDLE, TS, {"BTC": 100.0})
    o.brain = Brain(Decision("sell", reason="exit"))
    r = o.step(BY_NAME["BTC"], BUNDLE, "2026-03-02T16:00:00+00:00", {"BTC": 101.0})
    assert r.status == "closed" and not j.positions()


def test_sell_without_position_ignored():
    o, j, _ = mk(Brain(Decision("sell")))
    assert o.step(BY_NAME["BTC"], BUNDLE, TS, {"BTC": 100.0}).status == "hold"


def test_hold_journals():
    o, j, _ = mk(Brain(Decision("hold", reason="нет сигнала")))
    assert o.step(BY_NAME["BTC"], BUNDLE, TS, {"BTC": 100.0}).status == "hold"
    assert j.q("SELECT action FROM decisions")[0]["action"] == "hold"


def test_shadow_opinions_marked():
    o, j, _ = mk(Brain(Decision("hold")))

    class Sh:
        def decide(self, b, a):
            return Decision(opinions=[Opinion("tech_analyst", "buy", 0.6, "r")])
    o.shadow = Sh()
    o.step(BY_NAME["BTC"], BUNDLE, TS, {"BTC": 100.0})
    row = j.q("SELECT role, shadow FROM opinions")[0]
    assert row == {"role": "rule:tech_analyst", "shadow": 1}


@pytest.mark.parametrize("n", ["BTC"])
def test_equity_snapshot_flow(n):
    o, j, br = mk(Brain(BUY))
    o.step(BY_NAME[n], BUNDLE, TS, {"BTC": 100.0})
    assert br.equity({"BTC": 100.0}) < 100_000  # комиссия списана


def test_any_agent_stage_exception_skips_cycle():
    o, j, _ = mk(Brain(err=KeyError("choices")))
    r = o.step(BY_NAME["BTC"], BUNDLE, TS, {"BTC": 100.0})
    assert r.status == "skipped" and "KeyError" in r.detail and not j.positions()
