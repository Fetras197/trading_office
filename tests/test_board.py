import pytest

from office.board import Board, RejectedReport


def mk():
    b = Board()
    a = b.publish("c1", "market", "BTC/USD", "buy", 0.7, data={"x": 1})
    return b, a


def test_analyst_needs_no_refs():
    b, a = mk()
    assert b.get(a)["role"] == "market"


def test_no_refs_rejected_and_logged():
    b, a = mk()
    with pytest.raises(RejectedReport, match="нет ссылок"):
        b.publish("c1", "bull", "BTC/USD", "buy", 0.6)
    assert b.q("SELECT COUNT(*) n FROM rejected")[0]["n"] == 1
    assert b.q("SELECT COUNT(*) n FROM reports WHERE role='bull'")[0]["n"] == 0


def test_ref_must_exist_in_same_cycle_and_asset():
    b, a = mk()
    other = b.publish("c2", "market", "BTC/USD", "buy", 0.7)
    with pytest.raises(RejectedReport, match="несуществующий"):
        b.publish("c1", "bull", "BTC/USD", "buy", 0.6, refs=[other], stances={other: "agree"})
    with pytest.raises(RejectedReport, match="несуществующий"):
        b.publish("c1", "bull", "ETH/USD", "buy", 0.6, refs=[a], stances={a: "agree"})
    with pytest.raises(RejectedReport, match="несуществующий"):
        b.publish("c1", "bull", "BTC/USD", "buy", 0.6, refs=[999], stances={999: "agree"})


def test_explicit_stance_required():
    b, a = mk()
    with pytest.raises(RejectedReport, match="нет явной позиции"):
        b.publish("c1", "bull", "BTC/USD", "buy", 0.6, refs=[a], stances={})
    with pytest.raises(RejectedReport, match="не agree/dispute"):
        b.publish("c1", "bull", "BTC/USD", "buy", 0.6, refs=[a], stances={a: "maybe"})


def test_must_cite_role():
    b, a = mk()
    bull = b.publish("c1", "bull", "BTC/USD", "buy", 0.6, refs=[a], stances={a: "agree"}, must_cite=("analyst",))
    with pytest.raises(RejectedReport, match="обязательна ссылка на отчёт роли bear"):
        b.publish("c1", "trader", "BTC/USD", "buy", 0.6, refs=[bull], stances={bull: "agree"}, must_cite=("bull", "bear"))
    ok = b.publish("c1", "trader", "BTC/USD", "buy", 0.6, refs=[bull, a], stances={bull: "agree", a: "dispute"}, must_cite=("bull",))
    assert b.get(ok)["refs"] == [bull, a]


def test_bad_confidence_and_empty_verdict():
    b, _ = mk()
    with pytest.raises(RejectedReport):
        b.publish("c1", "market", "BTC/USD", "buy", 1.5)
    with pytest.raises(RejectedReport):
        b.publish("c1", "market", "BTC/USD", " ", 0.5)
