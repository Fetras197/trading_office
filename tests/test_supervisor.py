import pytest

from office import config
from office.alpaca import AlpacaClient, AlpacaError
from office.supervisor import MAX_ASSETS, Supervisor


class J:
    def positions(self):
        return {"ETH/USD": {}, "TSLA": {}}


def sv(stock_open, cands):
    s = Supervisor.__new__(Supervisor)
    s.j, s.stock_open, s.candidates, s.notify = J(), stock_open, cands, lambda t: None
    s.next = {"screen": 0, "cycle": 0, "summary": 1e18}
    return s


def test_targets_market_closed_crypto_only_held_first():
    t = sv(False, ["PLTR", "DOGE/USD"]).targets()
    assert t[0] == "ETH/USD" and "TSLA" not in t and "NVDA" not in t and "PLTR" not in t and "DOGE/USD" in t


def test_targets_market_open_includes_core_stocks_and_top5_capped():
    t = sv(True, ["A", "B", "C", "D", "E"]).targets()
    assert t[:2] == ["ETH/USD", "TSLA"] and {"SPY", "NVDA", "AAPL", "MSFT"} <= set(t) and len(t) <= MAX_ASSETS and len(set(t)) == len(t)


def test_failed_task_does_not_kill_tick():
    s = sv(False, [])
    msgs = []
    s.notify = msgs.append

    def boom():
        raise RuntimeError("x")
    s._screen_and_watch, s.cycle = boom, boom
    s.tick(now=1.0)
    assert len(msgs) == 2 and s.next["screen"] > 1.0  # упали обе, но расписание сдвинуто, процесс жив


def test_paper_only_env_guard(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "false")
    with pytest.raises(AlpacaError, match="paper"):
        AlpacaClient(key="k", secret="s")


def test_asset_mapping():
    a, b = config.make_asset("sol/usd"), config.make_asset("nvda")
    assert (a.kind, a.alpaca, a.ticker) == ("crypto", "SOL/USD", "SOLUSDT") and (b.kind, b.name) == ("stock", "NVDA")
