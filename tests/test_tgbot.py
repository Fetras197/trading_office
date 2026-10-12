import datetime as dt
import json
import re
from types import SimpleNamespace

from office import config, qa, tgbot, views, weights
from office.board import Board
from office.journal import Journal

VIEW = {"equity": 100450.0, "cash": 84000.0, "last_equity": 100000.0,
        "positions": [{"symbol": "ONDOUSD", "qty": 100.0, "avg": 0.51697, "price": 0.5252, "value": 15218.0, "pl": 238.5, "plpc": 0.0159}]}


class Router:
    def __init__(self, ans="Купили ONDO из-за тренда."): self.ans, self.calls = ans, []
    def ask(self, role, payload, system=None):
        self.calls.append((role, payload))
        return {"answer": self.ans}


def mk(tmp_path, monkeypatch, router=None):
    monkeypatch.setattr(config, "STOP_FILE", tmp_path / "STOP")
    b, j = Board(), Journal()
    j._x("INSERT INTO positions(asset,qty,avg,stop,take,fees,run_id,opened_ts) VALUES('ONDO/USD',100,0.51697,0.5033,0.5417,1,7,'2026-10-12T07:44:10+00:00')")
    sent, closed = [], []
    sup = SimpleNamespace(client=SimpleNamespace(account_view=lambda: VIEW), board=b, j=j, candidates=["SOL/USD"], router=router or Router(), bench=lambda: {"SPY": 0.006, "BTC": -0.004},
                          report_text=lambda: "ПОЛНЫЙ ОТЧЁТ", close_position=lambda n: closed.append(n) or {"pnl": 12.0, "pnl_pct": 0.012})
    bot = tgbot.TgBot(sup, send=lambda t, proof=True: sent.append((t, proof)), creds=("tok", "42"))
    return bot, sup, sent, closed


def test_commands_status_positions_picture_report_help(tmp_path, monkeypatch):
    bot, sup, sent, _ = mk(tmp_path, monkeypatch)
    sup.board.publish("R-p", "picture", "*", "neutral: x", 0.6, data={"regime": "risk_off", "summary": "Осторожно.", "main_risks": ["решение ФРС"], "forbidden": ["не покупать перед ФРС"],
                                                                      "priority_assets": ["SOL/USD"], "avoid_assets": [], "size_mult": 0.5, "no_new_entries": False})
    for cmd in ("/статус", "/позиции", "/картина", "/отчёт", "/помощь", "/нет_такой"):
        bot.handle(cmd)
    t = [x[0] for x in sent]
    assert "Состояние офиса" in t[0] and "+0,45% за день" in t[0] and "Работа: идёт" in t[0]
    assert "ONDO: вход 0,517 $" in t[1] and "стоп 0,503 $" in t[1]
    assert "Режим рынка: риск выключен" in t[2] and "Размер новых позиций: 50% от обычного" in t[2] and "решение ФРС" in t[2]
    assert t[3] == "ПОЛНЫЙ ОТЧЁТ" and "Команды:" in t[4] and t[5].startswith("Такой команды нет")


def test_pause_resume_close(tmp_path, monkeypatch):
    bot, sup, sent, closed = mk(tmp_path, monkeypatch)
    bot.handle("/пауза")
    assert config.STOP_FILE.exists() and "Пауза" in sent[-1][0] and sent[-1][1] is False
    bot.handle("/статус")
    assert "на паузе" in sent[-1][0]
    bot.handle("/продолжить")
    assert not config.STOP_FILE.exists()
    bot.handle("/закрыть ondo")
    assert closed == ["ONDO/USD"] and "по вашей команде" in sent[-1][0] and "+12 $" in sent[-1][0]
    bot.handle("/закрыть xyz")
    assert "Позиции по XYZ нет" in sent[-1][0] and closed == ["ONDO/USD"]
    bot.handle("/закрыть")
    assert "Укажите актив" in sent[-1][0]


def test_decisions_apply_only_announced_proposals(tmp_path, monkeypatch):
    bot, sup, sent, _ = mk(tmp_path, monkeypatch)
    bot.handle("да")
    assert "нет предложений" in sent[-1][0]
    b = sup.board
    ids = []
    for role in ("news", "quant"):
        ids.append(b.x("INSERT INTO proposals(ts,kind,payload,text) VALUES('t','weight',?,?)", (json.dumps({"role": role, "to": 0.8}), f"Понизить вес {role}")).lastrowid)
    other = b.x("INSERT INTO proposals(ts,kind,payload,text) VALUES('t','weight',?,?)", (json.dumps({"role": "market", "to": 0.7}), "не объявлено")).lastrowid
    sup.j.kv_set("meeting_props", ids)
    bot.handle("да 2")
    assert weights.weight_of(weights.active(b), "quant") == 0.8 and weights.weight_of(weights.active(b), "news") == 1.0 and "Применено" in sent[-1][0]
    bot.handle("нет")
    assert b.q("SELECT status FROM proposals WHERE id=?", (ids[0],))[0]["status"] == "rejected"
    assert b.q("SELECT status FROM proposals WHERE id=?", (other,))[0]["status"] == "pending"        # не из собрания — не трогаем
    bot.handle("да 5")
    assert "нет предложений" in sent[-1][0]


def test_question_gets_context_with_purchase_reasons(tmp_path, monkeypatch):
    r = Router()
    bot, sup, sent, _ = mk(tmp_path, monkeypatch, r)
    b, j = sup.board, sup.j
    j.add_run("2026-10-12T07:40:00+00:00", "ONDO/USD", 0.5, 0.01, "cyc7")             # runs.id=1 ... положим run_id нужный
    run_id = j.q("SELECT id FROM runs")[0]["id"]
    j.add_decision(run_id, "2026-10-12T07:44:00+00:00", "ONDO/USD", "buy", 0.5169, 0.5033, 0.5417, 100, "ok", "ok")
    j.update_decision(1, executed=1)
    b.publish("cyc7", "market", "ONDO/USD", "buy: strong trend", 0.8, data={"stance": "buy"})
    b.publish("cyc7", "bear", "ONDO/USD", "sell: overbought", 0.6, refs=[1], stances={1: "dispute"}, data={})
    bot.handle("почему купили ONDO?")
    assert sent[0][0].startswith("Секунду") and sent[1][0] == "Купили ONDO из-за тренда."
    ctx = r.calls[0][1]
    roles = [x["role"] for x in ctx["assets"][0]["purchase_cycle_reports"]]
    assert ctx["assets"][0]["asset"] == "ONDO/USD" and roles == ["Технический анализ", "Медведь"]       # роли уже по-русски
    assert ctx["assets"][0]["position"]["avg"] == 0.51697
    assert qa.find_assets("что думаешь про биткоин?", ["ONDO/USD"]) == ["BTC/USD"] and qa.find_assets("sol или eth", ["SOL/USD", "ETH/USD"]) == ["SOL/USD", "ETH/USD"]


def test_poll_ignores_foreign_chats_saves_offset_and_survives_409(tmp_path, monkeypatch):
    monkeypatch.setattr(tgbot.time, "sleep", lambda s: None)
    bot, sup, sent, _ = mk(tmp_path, monkeypatch)
    upd = {"ok": True, "result": [{"update_id": 10, "message": {"chat": {"id": 999}, "text": "/пауза"}}, {"update_id": 11, "message": {"chat": {"id": 42}, "text": "/помощь"}}]}
    bot.api = lambda tok, method, **kw: upd
    assert bot.poll_once() == 1 and sup.j.kv_get("tg_offset") == 12 and not config.STOP_FILE.exists() and len(sent) == 1
    bot.api = lambda tok, method, **kw: {"ok": False, "error_code": 409}
    assert bot.poll_once() == 0 and bot.conflicts == 1
    bot.api = lambda tok, method, **kw: {"ok": True, "result": [{"update_id": 13, "message": {"chat": {"id": 42}, "text": "/статус"}}]}
    sup.client = SimpleNamespace(account_view=lambda: (_ for _ in ()).throw(RuntimeError("брокер")))
    assert bot.poll_once() == 1 and "внутренней ошибки" in sent[-1][0]               # сбой команды не останавливает приём


def test_views_are_plain_russian():
    now = dt.datetime.now(dt.timezone.utc)
    evs = [{"ts": (now + dt.timedelta(hours=2)).isoformat(timespec="minutes"), "title": "Core CPI m/m", "ccy": "USD", "impact": "High", "kind": "macro"}]
    props = [{"text": "Понизить вес роли «Новости» с 1,00 до 0,80: точность 40% на 25 оценках"}]
    rows = [{"name": "office", "title": "Весь офис", "ret": 0.012, "max_dd": -0.01}, {"name": "spy", "title": "Держать S&P 500", "ret": 0.005, "max_dd": -0.02}]
    texts = [views.briefing_text(None, None, evs, VIEW, {}, ["SOL/USD"]),
             views.evening_text(VIEW, {"SPY": 0.01, "BTC": -0.01}, [{"asset": "ONDO/USD", "pnl": 12.0, "exit_reason": "take"}], 1, rows, ["не гнаться за ростом"], evs, {"trial": 1, "live": 0}),
             views.weekly_text(rows, [{"asset": "ONDO/USD", "pnl": 12.0}], ["урок"], [{"role": "news", "n": 25, "accuracy": 0.4}], props, {"trial": 1, "live": 0, "failed": 2}, "Неделя прошла ровно."),
             views.compare_line(rows)]
    for t in texts:
        assert not re.search(r"[A-Za-z_]{3,}", t.replace("ONDO", "").replace("SOL", "")), t       # ни английских слов, ни кодов
        assert "#" not in t
    assert "1) Понизить вес" in texts[2] and "«да»" in texts[2] and "базовая инфляция" in texts[0]
