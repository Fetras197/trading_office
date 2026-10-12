"""Управление с телефона (4.1): long polling Bot API в потоке supervisor. Принимаются только сообщения из чата владельца (TG_CHAT).
Команды по-русски: /статус /пауза /продолжить /закрыть <актив> /отчёт /позиции /картина (+ /помощь). «да»/«да 2»/«нет» — решения по предложениям недельного собрания.
Любой другой текст — вопрос к офису (qa.py). Ответы уходят через report.notify (вычитка Gemini), фиксированные подтверждения — без вычитки."""
import re
import time

import requests

from . import config, qa, realmoney, report, telegram, views, weights
from .alpaca import AlpacaError

HELP = ("Команды: /статус — состояние офиса, /позиции — открытые позиции, /картина — «Картина дня», /отчёт — полная сводка, /пауза — запретить новые покупки, "
        "/продолжить — снять паузу, /закрыть актив — продать позицию (например, /закрыть ONDO). Можно писать вопросы обычным текстом: «почему купили ONDO?», «что думаешь про биткоин?».")
ALIAS = {"статус": "status", "status": "status", "позиции": "positions", "positions": "positions", "картина": "picture", "picture": "picture", "отчёт": "report", "отчет": "report", "report": "report",
         "пауза": "pause", "pause": "pause", "продолжить": "resume", "resume": "resume", "закрыть": "close", "close": "close", "помощь": "help", "help": "help", "start": "help"}
DECISION = re.compile(r"^(да|нет)(?:\s+(все|\d+))?[.!]*$", re.I)


class TgBot:
    def __init__(self, sup, send=report.notify, creds=None, api=None):
        self.sup, self.send = sup, send
        self.tok, self.chat = creds or telegram._creds()
        self.api = api or telegram._api
        self.conflicts = 0

    # ---------- разбор ----------
    def handle(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        m = DECISION.match(text)
        if m:
            return self.decide(m.group(1).lower() == "да", m.group(2))
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            cmd = ALIAS.get(cmd.split("@")[0].lower())
            if not cmd:
                return self.send("Такой команды нет. " + HELP, proof=False)
            return getattr(self, f"cmd_{cmd}")(arg.strip())
        return self.question(text)

    # ---------- команды ----------
    def cmd_help(self, arg):
        self.send(HELP, proof=False)

    def cmd_status(self, arg):
        s = self.sup
        view = s.client.account_view()
        pic = s.board.latest("picture", max_age_h=12)
        rg = s.board.latest("regime", max_age_h=6)
        from . import lab
        self.send(views.status_text(view, config.STOP_FILE.exists(), rg["data"] if rg else None, pic, lab.stats(s.board), s.j.entries_on(report.utcnow_day()), s.bench(), realmoney.progress_line(realmoney.progress(s.board, s.j))))

    def cmd_positions(self, arg):
        self.send(views.positions_text(self.sup.client.account_view(), self.sup.j.positions()))

    def cmd_picture(self, arg):
        s = self.sup
        rg = s.board.latest("regime", max_age_h=6)
        self.send(views.picture_text(s.board.latest("picture", max_age_h=12), rg["data"] if rg else None))

    def cmd_report(self, arg):
        self.send(self.sup.report_text())

    def cmd_pause(self, arg):
        config.STOP_FILE.touch()
        self.send("⏸ Пауза: новые покупки запрещены. Открытые позиции остаются под защитными стопами. Чтобы вернуться к работе, отправьте /продолжить.", proof=False)

    def cmd_resume(self, arg):
        config.STOP_FILE.unlink(missing_ok=True)
        self.send("▶️ Пауза снята: офис снова может открывать позиции.", proof=False)

    def cmd_close(self, arg):
        if not arg:
            return self.send("Укажите актив, например: /закрыть ONDO", proof=False)
        want = arg.split()[0].upper().replace("-", "/")
        names = [n for n in self.sup.j.positions() if n.upper() == want or report.name(n).upper() == want.split("/")[0]]
        if not names:
            return self.send(f"Позиции по {want.split('/')[0]} нет.", proof=False)
        try:
            r = self.sup.close_position(names[0])
        except (AlpacaError, Exception) as e:  # noqa: BLE001 — владельцу нужен понятный ответ, а не молчание
            return self.send(f"Не удалось закрыть {report.name(names[0])}: брокер ответил ошибкой. Проверьте позицию вручную. ({type(e).__name__})", proof=False)
        self.send(report.close_message(names[0], r["pnl"], r["pnl_pct"], "manual"))

    # ---------- «да / нет» ----------
    def decide(self, yes: bool, which):
        ids = self.sup.j.kv_get("meeting_props") or []
        pend = {p["id"]: p for p in weights.pending(self.sup.board) if p["id"] in ids}
        if not pend:
            return self.send("Сейчас нет предложений, ожидающих вашего ответа.", proof=False)
        order = sorted(pend)
        if which and which != "все":
            n = int(which)
            if not 1 <= n <= len(order):
                return self.send(f"Предложения с номером {n} нет.", proof=False)
            order = [order[n - 1]]
        done = [weights.decide(self.sup.board, pid, yes) for pid in order]
        left = [i for i in ids if i not in order]
        self.sup.j.kv_set("meeting_props", left)
        self.send(("✅ Применено:\n" if yes else "Отклонено:\n") + "\n".join(f"• {d}" for d in done if d))

    # ---------- вопросы ----------
    def question(self, text):
        s = self.sup
        self.send("Секунду, смотрю данные…", proof=False)
        known = list(s.j.positions()) + list(s.candidates)
        ctx = qa.build_context(s.board, s.j, text, known)
        self.send(qa.answer(s.router, ctx))

    # ---------- цикл ----------
    def poll_once(self, timeout: int = 25) -> int:
        off = self.sup.j.kv_get("tg_offset") or 0
        r = self.api(self.tok, "getUpdates", offset=off, timeout=timeout, allowed_updates=["message"])
        if not r.get("ok"):
            if r.get("error_code") == 409:
                self.conflicts += 1
                time.sleep(30)    # второй опросчик на том же токене или вебхук: ждём, не падаем
            else:
                time.sleep(10)
            return 0
        self.conflicts = 0
        n = 0
        for u in r.get("result", []):
            self.sup.j.kv_set("tg_offset", u["update_id"] + 1)
            m = u.get("message") or {}
            if str((m.get("chat") or {}).get("id")) != str(self.chat):
                continue       # чужие сообщения игнорируем
            try:
                self.handle(m.get("text", ""))
            except Exception as e:  # noqa: BLE001 — сбой обработчика не должен останавливать приём команд
                print(f"{time.strftime('%F %T')} tgbot: {type(e).__name__}: {str(e)[:150]}", flush=True)
                self.send("Не получилось выполнить команду из-за внутренней ошибки. Попробуйте ещё раз позже.", proof=False)
            n += 1
        return n

    def run(self):
        if not (self.tok and self.chat):
            return
        while True:
            try:
                self.poll_once()
            except requests.RequestException:
                time.sleep(15)
            except Exception as e:  # noqa: BLE001
                print(f"{time.strftime('%F %T')} tgbot цикл: {type(e).__name__}: {str(e)[:150]}", flush=True)
                time.sleep(15)
