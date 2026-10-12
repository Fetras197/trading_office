
from office import review, weights
from office.board import Board
from office.journal import Journal
from office.llm import LLMError


def seed(b, role, hits, misses, conf=0.8):
    for k in [1] * hits + [0] * misses:
        rid = b.publish("c", role, "X", "buy: t", conf, data={"stance": "buy"})
        b.x("INSERT INTO outcomes(report_id,label,correct) VALUES(?,?,?)", (rid, 1, k))


def test_new_role_neutral_until_min_n_then_shrunk_proposal_needs_yes():
    b = Board()
    seed(b, "market", 4, 4)                     # 8 оценок < 10 — вес не предлагается
    seed(b, "quant", 9, 1)
    seed(b, "news", 3, 9)
    props = weights.propose(b)
    roles = {__import__("json").loads(p["payload"])["role"]: p for p in weights.pending(b)}
    assert "market" not in roles and len(props) == len(weights.pending(b)) >= 1
    assert weights.weight_of(weights.active(b), "news") == 1.0              # до «да» вес не меняется
    nid = next(p["id"] for p in weights.pending(b) if '"news"' in p["payload"] and p["kind"] == "weight")
    txt = weights.decide(b, nid, True)
    assert "Понизить" in txt and "Новости" in txt
    assert weights.weight_of(weights.active(b), "news") < 1.0
    assert weights.weight_of(weights.active(b), "quant") == 1.0
    assert weights.propose(b) == [] or True                                  # повторный расчёт не падает


def test_disable_only_with_enough_evidence_and_decline_keeps_weight():
    b = Board()
    seed(b, "buffett", 5, 30)                  # 35 оценок, точность 14%
    seed(b, "quant", 30, 5)
    weights.propose(b)
    kinds = {p["kind"] for p in weights.pending(b)}
    assert {"weight", "disable"} <= kinds
    for p in weights.pending(b):
        weights.decide(b, p["id"], False)
    assert weights.active(b) == {}


def test_weighted_vote_and_calibration():
    wts = {"market": {"weight": 1.5, "disabled": False}, "news": {"weight": 0.5, "disabled": False}, "quant": {"weight": 1.0, "disabled": True}}
    reps = [{"role": "market", "stance": "buy", "confidence": 0.8}, {"role": "news", "stance": "sell", "confidence": 0.8},
            {"role": "quant", "stance": "sell", "confidence": 1.0}, {"role": "lab", "stance": "abstain", "confidence": 0.0}]
    # (1.5*0.8 - 0.5*0.8) / (1.5+0.5) = 0.4 ; отключённый и воздержавшийся не считаются
    assert weights.weighted_vote(reps, wts) == 0.4
    b = Board()
    seed(b, "market", 6, 6, conf=0.8)          # заявляет 0.8, сбывается 0.5
    st = weights.stats(b)
    assert st["market"]["overconfidence"] == 0.3 and weights.calibrated_conf(st, "market", 0.8) == 0.5
    assert weights.calibrated_conf(st, "newrole", 0.8) == 0.8 and "new role" in weights.track_record(st, "newrole")


class R:
    def __init__(self, out): self.out, self.calls = out, 0
    def ask(self, role, payload, system=None):
        self.calls += 1
        if isinstance(self.out, Exception):
            raise self.out
        self.payload = payload
        return self.out


def mk_trade(b, j):
    cid = "cyc1"
    run = j.add_run("2026-10-10T10:00:00+00:00", "ONDO/USD", 0.5, 0.01, cid)
    b.publish(cid, "market", "ONDO/USD", "buy: тренд", 0.8, data={"stance": "buy"})
    b.publish(cid, "news", "ONDO/USD", "sell: слухи", 0.6, data={"stance": "sell"})
    j._x("INSERT INTO trades(asset,entry_ts,exit_ts,entry,exit,qty,fees,pnl,r_mult,run_id,exit_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
         ("ONDO/USD", "2026-10-10T10:00:00+00:00", "2026-10-11T10:00:00+00:00", 0.5, 0.52, 1000, 1, 19.0, 1.2, run, "take"))


def test_review_writes_lesson_to_memory_and_hall_sees_it_next_time():
    b, j = Board(), Journal()
    mk_trade(b, j)
    out = {"thesis": "Тренд и объём", "outcome": "right", "right_roles": ["market", "bogus"], "wrong_roles": ["news"], "entry": "good", "luck": "low", "lesson": "Слухи без цифр не мешают тренду", "tags": ["тренд"]}
    r = R(out)
    ids = review.run(b, j, r)
    assert len(ids) == 1 and r.payload["entry_reports"][0]["role"] == "market"
    assert review.run(b, j, r) == []                                        # повторно не разбираем
    rep = b.latest("postmortem", "ONDO/USD")
    assert rep["data"]["right_roles"] == ["market"]                         # несуществующая роль отброшена
    assert review.lessons_for(b, "ONDO/USD", "crypto") == [next(x for x in review.lessons_for(b, "ONDO/USD", "crypto"))]
    assert "Слухи без цифр" in review.lessons_for(b, "SOL/USD", "crypto")[0]    # урок по типу актива виден и для другой монеты


def test_review_invalid_answer_does_not_mark_reviewed():
    b, j = Board(), Journal()
    mk_trade(b, j)
    try:
        review.run(b, j, R(LLMError("мусор")))
    except LLMError:
        pass
    assert j.kv_get("reviewed:1") is None
