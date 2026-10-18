"""Облачный проход на фейках: порядок задач, ограниченные уведомления, пауза через OFFICE_HALT, урезание board.db."""
import sqlite3

from office import __main__ as cli
from office import config
from office.board import Board
from office.cloud import Office
from office.hall import Result
from office.journal import Journal


class C:
    def account_view(self): return {"equity": 100_000.0, "last_equity": 100_000.0, "cash": 100_000.0, "positions": []}
    def price(self, s): return 1.0
    def clock(self): return {"is_open": False}
    def day_change(self, s): return 0.0


class FakeHall:
    fresh_left = 3

    def __init__(self): self.ran = []

    def run_asset(self, s):
        self.ran.append(s)
        return Result(s, "hold", "нет технического сигнала", "c")


def mk(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(config, "STOP_FILE", tmp_path / "STOP")
    o = Office(board=Board(), journal=Journal(), client=C(), hall=FakeHall(), notify=sent.append, router=object())
    o.watch = lambda: None
    o.sched.run_due = lambda: []
    o.screen = lambda: setattr(o, "candidates", ["DOGE/USD"])
    return o, sent


def test_targets_candidates_first_core_after_no_stocks_when_closed(monkeypatch, tmp_path):
    o, _ = mk(monkeypatch, tmp_path)
    o.candidates = ["DOGE/USD", "ONDO/USD", "AAPL"]
    assert o.targets() == ["DOGE/USD", "BTC/USD", "ETH/USD", "SOL/USD"]       # ONDO исключён, акции вне часов биржи — нет
    o.stock_open = True
    assert o.targets()[:2] == ["DOGE/USD", "AAPL"] and "SPY" in o.targets()


def test_run_once_sends_welcome_once_and_runs_hall(monkeypatch, tmp_path):
    o, sent = mk(monkeypatch, tmp_path)
    res = o.run_once()
    assert len(res) == 4 and o.hall.ran[0] == "DOGE/USD" and sum("переехал в облако" in m for m in sent) == 1
    o.run_once()
    assert sum("переехал в облако" in m for m in sent) == 1


def test_halt_variable_creates_stop_and_notifies_once(monkeypatch, tmp_path):
    monkeypatch.setenv("OFFICE_HALT", "1")
    o, sent = mk(monkeypatch, tmp_path)
    o.run_once()
    o.run_once()
    assert config.STOP_FILE.exists() and sum("режим паузы" in m.lower() for m in sent) == 1


def test_failure_notices_are_rate_limited(monkeypatch, tmp_path):
    o, sent = mk(monkeypatch, tmp_path)
    o.hall.run_asset = lambda s: Result(s, "error", "boom", "c")
    o.run_once()
    o.run_once()
    assert sum("не удалось обработать" in m for m in sent) == 4                  # по одному на актив, повторов нет


def test_evening_has_why_not_block_with_three_nearest(monkeypatch, tmp_path):
    o, sent = mk(monkeypatch, tmp_path)
    ts = "2099-01-01T10:00:00+00:00"
    for a, st, why, sc in (("BTC/USD", "votes", "аналитики не одобрили: «за» 1", 0.7), ("ETH/USD", "signal", "нет технического сигнала", 0.3), ("SOL/USD", "limits", "лимит 3 позиций исчерпан", 0.95), ("XRP/USD", "signal", "нет", 0.1)):
        o.j.add_nogo(ts, a, st, why, sc)
    monkeypatch.setattr(o, "_local_midnight_utc", lambda: "2098-12-31T21:00:00+00:00")
    monkeypatch.setattr(o.shadow, "summary", lambda since=None: [])
    o.evening()
    m = sent[-1]
    assert "Почему не вошли" in m and "1) SOL — лимит риска" in m and "2) BTC — аналитики" in m and "3) ETH" in m and "XRP" not in m


def test_prune_board_drops_lab_and_old_reports(tmp_path):
    p = tmp_path / "b.db"
    b = Board(p)
    b.x("INSERT INTO strategies(ts,symbol,name,approved) VALUES('t','X','n',0)")
    b.x("INSERT INTO reports(cycle_id,role,asset,ts,verdict,confidence,refs,stances,data) VALUES('c','market','X','2020-01-01T00:00:00','v',0.5,'[]','{}','{}')")
    b.x("INSERT INTO reports(cycle_id,role,asset,ts,verdict,confidence,refs,stances,data) VALUES('c','macro','*','2020-01-01T00:00:00','v',0.5,'[]','{}','{}')")
    b.db.close()
    cli.prune_board(p)
    db = sqlite3.connect(p)
    assert db.execute("SELECT COUNT(*) FROM strategies").fetchone()[0] == 0
    assert [r[0] for r in db.execute("SELECT role FROM reports")] == ["macro"]


def test_candidates_restored_from_previous_pass_without_excluded(monkeypatch, tmp_path):
    b = Board()
    for i, s in enumerate(["DOGE/USD", "ONDO/USD", "USDC/USD", "LINK/USD"], 1):
        b.x("INSERT INTO candidates(ts,symbol,score,rank,data) VALUES('t',?,1,?,'{}')", (s, i))
    monkeypatch.setattr(config, "STOP_FILE", tmp_path / "STOP")
    o = Office(board=b, journal=Journal(), client=C(), hall=FakeHall(), notify=lambda t: None, router=object())
    assert o.candidates == ["DOGE/USD", "LINK/USD"]


def test_force_jobs_env_resets_schedule_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("OFFICE_FORCE_JOBS", "macro, news")
    o, _ = mk(monkeypatch, tmp_path)
    o.j.kv_set("job:macro:last", "2026-10-18T09:00:00+03:00")
    o.j.kv_set("job:crypto:last", "2026-10-18T09:00:00+03:00")
    o.run_once()
    assert o.j.kv_get("job:macro:last") is None and o.j.kv_get("job:news:last") is None and o.j.kv_get("job:crypto:last")
