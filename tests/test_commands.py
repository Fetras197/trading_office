"""Команды из Telegram: опрос getUpdates на проходе, offset в journal.kv, только свой chat_id, пауза/продолжить/закрыть, время прохода."""

import pytest

from office import commands, config
from office.board import Board
from office.cloud import Office
from office.journal import Journal

CHAT = "777"
NOW = 1_800_000_000.0


class C:
    def __init__(self): self.pos = []
    def account_view(self): return {"equity": 100_000.0, "last_equity": 99_000.0, "cash": 60_000.0, "positions": self.pos}
    def price(self, s): return 100.0
    def clock(self): return {"is_open": True}
    def day_change(self, s): return 0.0


class Broker:
    def __init__(self): self.sold = []
    def sell(self, name, price, ts, reason):
        self.sold.append((name, reason))
        return {"pnl": 120.0}


def upd(i, text, chat=CHAT, date=NOW - 60):
    return {"update_id": i, "message": {"chat": {"id": int(chat)}, "text": text, "date": date}}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STOP_FILE", tmp_path / "STOP")
    monkeypatch.setenv("TG_TOKEN", "tok")
    monkeypatch.setenv("TG_CHAT", CHAT)
    sent, queue = [], []
    o = Office(board=Board(), journal=Journal(), client=C(), hall=type("H", (), {"exec_lock": __import__("threading").RLock()})(), notify=sent.append, router=object())
    o.broker = Broker()
    o.stock_open = True
    o.summary = lambda: sent.append("СВОДКА")
    calls = []

    def fake_fetch(tok, offset, session=None):
        calls.append(offset)
        return [u for u in queue if offset is None or u["update_id"] >= offset]
    monkeypatch.setattr(commands, "fetch", fake_fetch)
    o.commands.now = lambda: NOW
    return o, sent, queue, calls


def test_parse_aliases_and_bot_suffix():
    assert commands.parse("/статус") == ("status", "")
    assert commands.parse("/закрыть@my_bot BTC") == ("close", "BTC")
    assert commands.parse("/отчет") == ("report", "") and commands.parse("/Пауза") == ("pause", "")
    assert commands.parse("привет") is None and commands.parse("/нечто") is None


def test_status_reply_has_pass_time_and_offset_saved(env):
    o, sent, queue, calls = env
    queue.append(upd(10, "/статус"))
    assert o.commands.poll() == 1
    assert "Статус" in sent[-1] and "Проход офиса:" in sent[-1] and "по Минску" in sent[-1]
    assert o.j.kv_get("tg_offset") == 11
    o.commands.poll()                                   # то же сообщение второй раз не исполняется
    assert len(sent) == 1 and calls[-1] == 11


def test_foreign_chat_ignored_but_offset_advances(env):
    o, sent, queue, _ = env
    queue.append(upd(5, "/закрыть все", chat="999"))
    assert o.commands.poll() == 0
    assert not sent and not o.broker.sold and o.j.kv_get("tg_offset") == 6


def test_stale_command_not_executed(env):
    o, sent, queue, _ = env
    queue.append(upd(1, "/закрыть все", date=NOW - 4 * 3600))
    o.commands.poll()
    assert not o.broker.sold and "больше 3 часов" in sent[-1]


def test_pause_persists_and_resume_clears(env, monkeypatch):
    o, sent, queue, _ = env
    queue.append(upd(1, "/пауза"))
    o.commands.poll()
    assert config.STOP_FILE.exists() and o.j.kv_get("paused") == 1
    config.STOP_FILE.unlink()                           # новый проход = новая файловая система: пауза восстанавливается из office.db
    o.screen = lambda: None
    o.watch = lambda: None
    o.sched.run_due = lambda: []
    o.cycle = lambda: []
    o.run_once()
    assert config.STOP_FILE.exists()
    queue.append(upd(2, "/продолжить"))
    o.commands.poll()
    assert not config.STOP_FILE.exists() and o.j.kv_get("paused") == 0


def test_resume_blocked_by_env_halt_says_so(env, monkeypatch):
    o, sent, queue, _ = env
    monkeypatch.setenv("OFFICE_HALT", "1")
    queue.append(upd(1, "/продолжить"))
    o.commands.poll()
    assert "OFFICE_HALT" in sent[-1] and config.STOP_FILE.exists() is False


def test_close_one_all_unknown_and_no_arg(env):
    o, sent, queue, _ = env
    for n in ("AAPL", "BTC/USD"):
        o.j._x("INSERT INTO positions(asset, qty, avg, stop, take, fees, run_id, opened_ts) VALUES(?,?,?,?,?,?,?,?)", (n, 1, 100, 90, 120, 0, 1, "2026-10-20T00:00:00+00:00"))
    queue += [upd(1, "/закрыть"), upd(2, "/закрыть xyz"), upd(3, "/закрыть btc")]
    o.commands.poll()
    assert "Укажите" in sent[0] and "нет" in sent[1] and "по вашей команде" in sent[2]
    assert o.broker.sold == [("BTC/USD", "manual")]
    queue.append(upd(4, "/закрыть все"))
    o.commands.poll()
    assert ("AAPL", "manual") in o.broker.sold


def test_poll_survives_telegram_error(env, monkeypatch):
    import requests
    o, sent, _, _ = env
    monkeypatch.setattr(commands, "fetch", lambda *a, **k: (_ for _ in ()).throw(requests.RequestException("409")))
    assert o.commands.poll() == 0 and not sent


def test_no_credentials_no_poll(env, monkeypatch):
    o, *_ = env
    monkeypatch.delenv("TG_TOKEN")
    assert o.commands.poll() == 0
