"""Команды владельца из Telegram: на каждом проходе офис читает новые сообщения бота (getUpdates), отвечает только на TG_CHAT и выполняет команды.
Постоянного процесса в облаке нет, поэтому команда исполняется на ближайшем проходе (до 30 минут в часы биржи, до часа в остальное время); в ответе — время прохода по Минску.
offset последнего обработанного сообщения лежит в journal.kv (файл office.db уезжает в ветку state) — одно сообщение исполняется один раз.
Команды: /статус, /позиции, /отчёт, /пауза, /продолжить, /закрыть <тикер|все>. Лимиты риска не затрагиваются."""
import os
import re
import time

import requests

from . import config, desks, report, telegram
from .hall import utcnow

STALE_S = 3 * 3600          # команда старше 3 часов не исполняется (иначе забытое «/закрыть» из старой переписки сработало бы спустя дни)
ALIASES = {"статус": "status", "status": "status", "позиции": "positions", "positions": "positions", "отчёт": "report", "отчет": "report", "report": "report",
           "пауза": "pause", "pause": "pause", "продолжить": "resume", "resume": "resume", "закрыть": "close", "close": "close", "help": "help", "помощь": "help", "start": "help"}
HELP = ("Команды: /статус, /позиции, /отчёт, /пауза, /продолжить, /закрыть <тикер> (или /закрыть все).\n"
        "Офис проверяет сообщения на каждом проходе (раз в 30–60 минут), поэтому ответ приходит не мгновенно.")


def parse(text: str) -> tuple[str, str] | None:
    """'/закрыть@my_bot BTC' -> ('close', 'BTC'); не команда или неизвестная команда -> None."""
    m = re.match(r"^/([\w]+)(?:@\w+)?\s*(.*)$", (text or "").strip(), re.S)
    if not m:
        return None
    cmd = ALIASES.get(m.group(1).lower())
    return (cmd, m.group(2).strip()) if cmd else None


def fetch(tok: str, offset: int | None, session=None) -> list[dict]:
    r = (session or requests).post(f"https://api.telegram.org/bot{tok}/getUpdates", json={"timeout": 0, "allowed_updates": ["message"], **({"offset": offset} if offset else {})}, timeout=30).json()
    if not r.get("ok"):
        raise requests.RequestException(f"getUpdates: {str(r.get('description'))[:100]}")
    return r.get("result", [])


class Commands:
    def __init__(self, office, session=None, now_fn=time.time):
        self.o, self.session, self.now = office, session, now_fn

    def stamp(self) -> str:
        return config.localnow().strftime("%d.%m %H:%M") + " по Минску"

    def reply(self, text: str) -> None:
        msg = f"{text}\n\n🕒 Проход офиса: {self.stamp()}"
        if self.o.notify is report.notify:
            report.notify(msg, proof=False)      # ответ на команду не вычитывается нейросетью
        else:
            self.o.notify(msg)

    # ---------- опрос ----------
    def poll(self) -> int:
        """Прочитать новые сообщения, выполнить команды владельца. Сбой сети/Telegram не должен ронять проход. Возвращает число выполненных команд."""
        tok, chat = telegram._creds()
        if not (tok and chat):
            return 0
        j = self.o.j
        try:
            updates = fetch(tok, j.kv_get("tg_offset"), self.session)
        except requests.RequestException as e:
            print(f"telegram getUpdates: {type(e).__name__}: {str(e)[:120]}", flush=True)
            return 0
        done = 0
        for u in sorted(updates, key=lambda x: x["update_id"]):
            j.kv_set("tg_offset", u["update_id"] + 1)      # сначала фиксируем offset: упавшая команда не должна исполниться дважды
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != str(chat):
                continue                                    # чужой чат — молча игнорируем
            cmd = parse(msg.get("text", ""))
            if not cmd:
                continue
            if self.now() - float(msg.get("date") or 0) > STALE_S:
                self.reply(f"⌛ Команда «{msg.get('text', '')[:40]}» пришла больше 3 часов назад и не исполнена. Повторите, если она ещё нужна.")
                continue
            try:
                self.run(*cmd)
                done += 1
            except Exception as e:  # noqa: BLE001
                print(f"команда {cmd}: {type(e).__name__}: {str(e)[:150]}", flush=True)
                self.reply(f"⚠️ Не удалось выполнить /{cmd[0]}: {type(e).__name__}. Проверьте позиции вручную на Alpaca.")
        return done

    def run(self, cmd: str, arg: str) -> None:
        getattr(self, "c_" + cmd)(arg)

    # ---------- команды ----------
    def halted_by_env(self) -> bool:
        return os.environ.get("OFFICE_HALT", "").strip() in ("1", "true", "yes")

    def c_help(self, arg):
        self.reply(HELP)

    def c_status(self, arg):
        v = self.o.client.account_view()
        paused = self.o.j.kv_get("paused") or self.halted_by_env()
        day = (v["equity"] - v["last_equity"]) / v["last_equity"] if v["last_equity"] else 0.0
        self.reply(f"📊 Статус\nСчёт: {report.usd(v['equity'])}, свободно {report.usd(v['cash'])}; за день {report.pct(day)}\n"
                   f"Позиций: {len(v['positions'])}; биржа акций {'открыта' if self.o.stock_open else 'закрыта'}\n"
                   f"Режим: {'⏸ пауза (новых покупок нет, позиции под защитными стопами)' if paused else '▶️ работает'}"
                   + (" — задана переменной OFFICE_HALT в GitHub, командой не снимается" if self.halted_by_env() else ""))

    def c_positions(self, arg):
        v, pos = self.o.client.account_view(), self.o.j.positions()
        if not v["positions"]:
            return self.reply("Открытых позиций нет.")
        lines = []
        for p in v["positions"]:
            name = report.pos_name(p["symbol"])
            jp = next((r for k, r in pos.items() if k.replace("/", "") == p["symbol"] or k == p["symbol"]), None)
            lv = f"; стоп {jp['stop']:.6g}, тейк {jp['take']:.6g}" if jp and jp["stop"] and jp["take"] else ""
            desk = f" [{desks.name_of(self.o.j, jp['desk'])}]" if jp and "desk" in jp.keys() and jp["desk"] else ""
            lines.append(f"• {name}{desk}: {report.usd(p['value'])}, {report.signed(p['pl'])} $ ({report.pct(p['plpc'])}){lv}")
        self.reply("📌 Позиции\n" + "\n".join(lines))

    def c_report(self, arg):
        self.o.summary()
        self.reply("📨 Сводка отправлена выше.")

    def c_pause(self, arg):
        self.o.j.kv_set("paused", 1)
        config.STOP_FILE.touch()
        self.reply("⏸ Пауза включена: новых покупок нет, открытые позиции остаются под защитными стопами. Снять — /продолжить.")

    def c_resume(self, arg):
        self.o.j.kv_set("paused", 0)
        if self.halted_by_env():
            return self.reply("⚠️ Пауза в офисе снята, но в GitHub задана переменная OFFICE_HALT — она держит паузу. Удалите её в Settings → Variables.")
        config.STOP_FILE.unlink(missing_ok=True)
        self.reply("▶️ Пауза снята: офис снова принимает решения на следующем проходе.")

    def c_close(self, arg):
        pos = self.o.j.positions()
        if not arg:
            names = ", ".join(report.name(n) for n in pos) or "позиций нет"
            return self.reply(f"Укажите, что закрыть: /закрыть <тикер> или /закрыть все. Сейчас открыто: {names}.")
        want = list(pos) if arg.strip().lower() in ("все", "всё", "all") else [n for n in pos if report.name(n).upper() == arg.strip().upper().replace("/USD", "").replace("USD", "")
                                                                                or n.upper() == arg.strip().upper()]
        if not want:
            return self.reply(f"Позиции «{arg[:20]}» нет. Открыто: {', '.join(report.name(n) for n in pos) or 'ничего'}.")
        out = []
        for name in want:
            try:
                with self.o.hall.exec_lock:
                    desk = pos[name]["desk"] if "desk" in pos[name].keys() else None
                    r = self.o.broker.sell(name, float(self.o.client.price(name)), utcnow(), "manual")
                t = self.o.j.q("SELECT entry, qty FROM trades WHERE asset=? ORDER BY id DESC LIMIT 1", (name,))
                cost = t[0]["entry"] * t[0]["qty"] if t else 0
                out.append(report.close_message(name, r["pnl"], r["pnl"] / cost if cost else 0.0, "manual", desk=desks.name_of(self.o.j, desk)))
                self.o.sched.trigger("portfolio")
                self.o.sched.trigger("review")
            except Exception as e:  # noqa: BLE001
                out.append(f"⚠️ {report.name(name)}: не закрыто ({type(e).__name__}: {str(e)[:80]})")
        if not self.o.stock_open and any("/" not in n for n in want):
            out.append("ℹ️ Биржа акций закрыта: заявка по акциям исполнится на открытии.")
        self.reply("\n\n".join(out))
