import pandas as pd

from office.accuracy import barrier_outcome, role_accuracy, settle
from office.journal import Journal


def bars(rows, start="2026-01-01 01:00"):
    idx = pd.date_range(start, periods=len(rows), freq="h", tz="UTC")
    return pd.DataFrame(rows, columns=["high", "low"], index=idx)


def test_up_first():
    rows = [(101, 99)] * 3 + [(125, 99)] + [(101, 99)] * 20
    assert barrier_outcome(bars(rows), 100, 5.0 / 4.899, 24) == 1   # B = atr*sqrt(24) ≈ 5


def test_down_first_and_both_conservative():
    assert barrier_outcome(bars([(101, 90)] + [(101, 99)] * 23), 100, 1.0) == -1
    assert barrier_outcome(bars([(120, 80)] + [(101, 99)] * 23), 100, 1.0) == -1


def test_timeout_and_not_enough_data():
    assert barrier_outcome(bars([(101, 99)] * 24), 100, 1.0) == 0
    assert barrier_outcome(bars([(101, 99)] * 10), 100, 1.0) is None


def test_settle_and_accuracy():
    j = Journal()
    r = j.add_run("2026-01-01T00:00:00+00:00", "BTC", 100, 1.0, "t")
    j.add_opinion(r, "2026-01-01T00:00:00+00:00", "BTC", "tech_analyst", "buy", 0.7, "x")
    j.add_opinion(r, "2026-01-01T00:00:00+00:00", "BTC", "news_sentiment", "abstain", 0, "x")
    up = bars([(101, 99)] * 2 + [(110, 99)] + [(101, 99)] * 30)
    assert settle(j, lambda n: up) == 1  # abstain не оценивается
    acc = {a["role"]: a for a in role_accuracy(j)}
    assert acc["tech_analyst"]["accuracy"] == 1.0 and "news_sentiment" not in acc
