"""Supervisor 24/7. Тик 30 с. Расписание:
  15 мин: скринер (все крипто-пары + S&P500/NDX) -> top-5; сверка стопов/тейков с брокером (watch);
  1 ч:    полный цикл зала: крипта BTC/ETH/SOL; акции SPY/NVDA/AAPL/MSFT в часы биржи; + top-5 скринера + уже открытые позиции;
  6 ч:    сводка в Telegram.
Лаборатория — отдельный фоновый процесс (`python -m office lab`). STOP-файл = новые решения не принимаются."""
import concurrent.futures as cf
import datetime as dt
import fcntl
import threading
import time

from . import accuracy, config, data, events, it, realmoney, memory, tgbot, views, lab, labtrial, manage, report, research, review, scheduler, screener, shadow, smartmoney, weights
from .alpaca import AlpacaBroker, AlpacaClient
from .board import Board
from .hall import Hall, Result, utcnow
from .journal import Journal
from .llm import ClaudeBackend, RoleRouter

SCREEN_S, CYCLE_S, SUMMARY_S = 15 * 60, 3600, 6 * 3600
MAX_ASSETS = 10  # бюджет LLM на часовой цикл (core + top-5 + позиции)


class Supervisor:
    def __init__(self, board=None, journal=None, client=None, hall=None, notify=report.notify):
        config.load_env()
        self.board = board or Board(config.ROOT / "board.db")
        self.j = journal or Journal(config.DB_PATH)
        self.client = client or AlpacaClient()
        self.broker = AlpacaBroker(self.client, self.j)
        self.notify = notify
        self.router = RoleRouter()
        self.candidates: list[str] = []
        self.stock_open = False
        self.research = research.Research(self.board, self.j, self.client, self.router, self.scope, notify=notify)
        self.research.on_urgent = self.urgent
        self.hall = hall or Hall(self.board, self.j, self.broker, self.router, ClaudeBackend(), notify=notify,
                                 lab=lambda s: self._lab(s), picture=self.research.ensure_picture, smart=smartmoney.signal_for)
        self.next = {"screen": 0.0, "cycle": 0.0, "summary": time.time() + SUMMARY_S}
        self.it = it.ITDept(self.j, notify)
        self.manager = manage.Manager(self.broker, self.j, self.client, notify)
        self.shadow = shadow.Shadow(self.board, self.client.price, lambda: self.client.account_view()["equity"], lab_fn=lambda: lab.shadow_signals(self.board), start_fn=lambda: self.j.kv_get("start_equity"))
        self.sched = scheduler.Scheduler(self.j, self.jobs())

    def scope(self) -> dict:
        return {"held": list(self.j.positions()), "candidates": list(self.candidates)}

    def jobs(self) -> list:
        """Исследовательский отдел: порядок в списке = порядок первичного наполнения при самом первом запуске."""
        R, J = self.research, scheduler.Job
        return [J("events", R.run_events, every=3600),
                J("macro", R.run_macro, at=["07:00"], catchup_h=8),
                J("macro_pre_event", self.pre_event_macro, every=300),
                J("news", self._eco(R.run_news), every=3600),
                J("crypto", self._eco(R.run_crypto), every=3600),
                J("sectors", self._eco(R.run_sectors), at=["07:10"], catchup_h=8),
                J("portfolio", self._eco(R.run_portfolio), at=["07:20"], catchup_h=8),
                J("it", self.it.check, every=300),
                J("realmoney", lambda: realmoney.check(self.board, self.j, self.notify, self.j), every=21600),
                J("manage", self.manager.run, every=300),
                J("smartmoney", smartmoney.refresh, every=1800),
                J("regime", R.run_regime, every=1800),
                J("summary", self.summary, every=SUMMARY_S),
                J("briefing", self.briefing, at=["08:00"], catchup_h=3, bootstrap=False),
                J("evening", self.evening, at=["21:00"], catchup_h=3, bootstrap=False),
                J("weekly", self.weekly, at=["20:00"], weekday=6, catchup_h=6, bootstrap=False),
                J("labtrial", lambda: labtrial.promote(self.board), every=3600),
                J("shadow", self.shadow.step, every=3600),
                J("review", self.run_review, every=1800),
                J("weights", lambda: weights.propose(self.board), at=["06:50"], catchup_h=12),
                J("picture", R.run_picture, at=["03:30", "07:30", "11:30", "15:30", "19:30", "23:30"], catchup_h=3)]

    # ---------- сообщения по расписанию и по запросу (этап 4) ----------
    def _local_midnight_utc(self) -> str:
        m = config.localnow().replace(hour=0, minute=0, second=0, microsecond=0)
        return m.astimezone(dt.timezone.utc).isoformat(timespec="seconds")

    def report_text(self) -> str:
        """Полная сводка по запросу /отчёт: сводка + теневые портфели."""
        view, now = self.client.account_view(), utcnow()
        text = report.summary_message(view, self.bench(), self.j.entries_on(now[:10]), report.lab_line(lab.stats(self.board)), title="Полный отчёт")
        return text + "\n\n" + views.shadow_text(self.shadow.summary())

    def briefing(self):
        """08:00 — утренний брифинг: картина дня, события, план."""
        pic = self.research.ensure_picture()
        rg = self.board.latest("regime", max_age_h=6)
        self.notify(views.briefing_text(pic, rg["data"] if rg else None, events.upcoming(self.board, 24), self.client.account_view(), self.j.positions(), self.candidates))

    def evening(self):
        """21:00 — вечерний отчёт: итоги дня, теневые портфели (доходность и просадка), уроки, завтрашний календарь."""
        since = self._local_midnight_utc()
        trades = self.j.q("SELECT asset, pnl, exit_reason FROM trades WHERE exit_ts>=? AND exit_reason!='partial' ORDER BY id", (since,))
        opened = self.j.q("SELECT COUNT(*) n FROM fills WHERE side='buy' AND ts>=?", (since,))[0]["n"]
        lessons = [r["text"] for r in memory.recall(self.board, "lesson", days=1, limit=3)]
        self.notify(views.evening_text(self.client.account_view(), self.bench(), trades, opened, self.shadow.summary(), lessons, events.upcoming(self.board, 24), lab.stats(self.board)))

    def weekly(self):
        """Воскресенье 20:00 — недельное собрание. Предложения (веса/отключение ролей) применяются только после «да» (tgbot.decide)."""
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat(timespec="seconds")
        weights.propose(self.board)
        props = weights.pending(self.board)
        self.j.kv_set("meeting_props", [p["id"] for p in props])
        trades = self.j.q("SELECT asset, pnl FROM trades WHERE exit_ts>=? AND exit_reason!='partial'", (since,))
        lessons = [r["text"] for r in memory.recall(self.board, "lesson", days=7, limit=5)]
        acc = [{"role": r, "n": v["n"], "accuracy": v["acc"]} for r, v in weights.stats(self.board).items()]
        insight = ""
        try:
            insight = str(self.router.ask("weekly", {"week_shadow": self.shadow.summary(since), "closed_trades": trades, "open_positions": [report.pos_name(p["symbol"]) + f" {p['plpc']:+.1%}" for p in self.client.account_view()["positions"]], "lessons": lessons, "proposals": [p["text"] for p in props]},
                                          system="You are the head of a paper-trading firm. In 3 short sentences in Russian (plain words, no English, up to 70 words) sum up the week: how we did vs the S&P 500 and bitcoin, what we learned. "
                                                 "Use only the input data. Output ONE JSON: {\"insight\": \"...\"}").get("insight", ""))[:600]
        except Exception:  # noqa: BLE001 — итоги недели не должны зависеть от лимитов
            pass
        self.notify(views.weekly_text(self.shadow.summary(since), trades, lessons, acc, props, lab.stats(self.board), insight, realmoney.progress_line(realmoney.progress(self.board, self.j))))

    def close_position(self, name: str) -> dict:
        """Ручное закрытие по команде владельца (/закрыть) — под тем же замком исполнения, что и зал."""
        with self.hall.exec_lock:
            p = self.j.positions()[name]
            px = float(self.client.price(config.BY_NAME[name].alpaca))
            r = self.broker.sell(name, px, utcnow(), "manual")
        self.sched.trigger("portfolio")
        self.sched.trigger("review")
        return {**r, "pnl_pct": r["pnl"] / (p["avg"] * p["qty"]) if p["avg"] * p["qty"] else 0.0}

    def run_review(self):
        """Разбор закрытых сделок (2.2) — Claude; не больше 3 за проход."""
        review.run(self.board, self.j, self.router, lambda n: data.candles(config.BY_NAME[n], "1h", **({"limit": 500} if "/" in n else {"rng": "60d"})))

    def _eco(self, fn):
        """В режиме экономии задания на Gemini пропускаются (их данные устаревают, зал работает по уже готовым отчётам)."""
        return lambda: None if self.it.economy else fn()

    def pre_event_macro(self):
        """За час до важного события USD: обновить макро-анализ и картину дня (1 раз на событие)."""
        now = dt.datetime.now(dt.timezone.utc)
        for e in events.upcoming(self.board, 1, now, high_only=True):
            if e["assets"] == "all" and now <= events._dt(e["ts"]) and self.j.kv_get(f"pre:{e['key']}") is None:
                self.j.kv_set(f"pre:{e['key']}", now.isoformat())
                self.research.run_macro()
                self.research.run_picture()
                return

    def urgent(self, asset: str):
        """Срочная новость: внеочередной цикл зала по активу (не чаще раза в 30 минут)."""
        if asset not in self.scope()["held"] + self.candidates + list(config.CRYPTO_CORE) or config.STOP_FILE.exists():
            return
        k, now = f"urgent_run:{asset}", time.time()
        if now - (self.j.kv_get(k) or 0) < 1800:
            return
        self.j.kv_set(k, now)
        self._handle(self._run_asset(asset))

    def research_loop(self):
        while True:
            try:
                self.sched.run_due()
            except Exception as e:  # noqa: BLE001
                print(f"{utcnow()} исследовательский отдел: {type(e).__name__}: {e}", flush=True)
            time.sleep(20)

    def _lab(self, symbol):
        try:
            return lab.signal_for(self.board, symbol)
        except Exception:  # noqa: BLE001 — лаборатория не критична для зала
            return None

    # --- задачи ---
    def watch(self):
        """Сверка с брокером + эквити-снапшот; закрытия стопом/тейком -> Telegram."""
        prices = {}
        for n in self.j.positions():
            try:
                a = config.BY_NAME[n]
                prices[n] = float(data.candles(a, "1h", **({"limit": 5} if a.kind == "crypto" else {"rng": "5d"}))["close"].iloc[-1])
            except Exception:  # noqa: BLE001
                pass
        for name, ev in self.broker.sync(prices, utcnow()):
            t = self.j.q("SELECT entry, qty FROM trades WHERE asset=? ORDER BY id DESC LIMIT 1", (name,))
            cost = t[0]["entry"] * t[0]["qty"] if t else 0
            self.notify(report.close_message(name, ev["pnl"], ev["pnl"] / cost if cost else 0.0, ev["reason"], ev.get("offline", False)))
            self.sched.trigger("portfolio")
            self.sched.trigger("review")
        for sym in self.broker.orphans():   # на брокере есть позиция, которой нет в журнале — молча не игнорируем
            if self.j.kv_get(f"orphan:{sym}") is None:
                self.j.kv_set(f"orphan:{sym}", utcnow())
                self.notify(f"⚠️ У брокера открыта позиция {report.pos_name(sym)}, которой нет в журнале офиса. Проверьте её вручную: офис её не сопровождает.")
        eq = self.broker.equity()
        self.j.snap_equity(utcnow(), eq)
        if self.j.kv_get("start_equity") is None:
            self.j.kv_set("start_equity", eq)

    def screen(self):
        self.stock_open = bool(self.client.clock().get("is_open"))
        held = set(self.j.positions())
        top = screener.scan(self.board, self.client, self.stock_open, exclude=held | set(config.CRYPTO_CORE + config.STOCK_CORE))
        self.candidates = [r["symbol"] for r in top]

    def targets(self) -> list[str]:
        held = list(self.j.positions())
        core = list(config.CRYPTO_CORE) + (list(config.STOCK_CORE) if self.stock_open else [])
        out = []
        for s in held + core + self.candidates:
            if s not in out and (self.stock_open or "/" in s):  # акции вне часов биржи не торгуем
                out.append(s)
        econ = getattr(self, "it", None) and self.it.economy      # режим экономии лимитов (5.1): меньше активов
        return out[:it.ECON_ASSETS if econ else MAX_ASSETS]

    def cycle(self):
        try:
            self.watch()   # состояние позиций берём у брокера в начале каждого цикла
        except Exception as e:  # noqa: BLE001
            print(f"{utcnow()} сверка с брокером: {type(e).__name__}: {str(e)[:120]}", flush=True)
        if config.STOP_FILE.exists():
            return
        res = []
        with cf.ThreadPoolExecutor(2) as ex:
            for r in ex.map(self._run_asset, self.targets()):
                res.append(r)
                self._handle(r)
        try:
            accuracy.settle(self.board, self.j, lambda n: data.candles(config.BY_NAME[n], "1h", **({"limit": 500} if "/" in n else {"rng": "60d"})))
        except Exception:  # noqa: BLE001
            pass
        return res

    def bench(self) -> dict:
        out = {}
        for k, sym in (("SPY", "SPY"), ("BTC", "BTC/USD")):
            try:
                out[k] = self.client.day_change(sym)
            except Exception:  # noqa: BLE001 — сравнение не критично
                out[k] = 0.0
        return out

    def _handle(self, r):
        print(f"{utcnow()} {r.asset}: {r.status} {r.detail}", flush=True)
        if r.status in ("opened", "closed"):
            self.notify(report.trade_message(self.board, r))
            self.sched.trigger("portfolio")   # 1.6: портфельный аналитик после каждой сделки
            if r.status == "closed":
                self.sched.trigger("review")
        elif r.status == "error":
            self.notify(report.service("asset_fail", r.asset))

    def _run_asset(self, symbol):
        if config.restart_requested():  # перезапуск ждёт: новые активы в цикл не берём
            return Result(symbol, "skipped", "ожидается перезапуск", "")
        return self.hall.run_asset(symbol)

    def summary(self):
        view, now = self.client.account_view(), utcnow()
        text = report.summary_message(view, self.bench(), self.j.entries_on(now[:10]), report.lab_line(lab.stats(self.board)))
        cmp_ = views.compare_line(self.shadow.summary())
        if cmp_:
            text += "\n" + cmp_
        acc = accuracy.role_accuracy(self.board)
        if acc:
            text += "\nТочность оценок: " + ", ".join(f"{report.ACC_RU[a['role']]} {a['accuracy'] * 100:.0f}%".replace(".", ",") for a in acc if a["role"] in report.ACC_RU)
        self.notify(text)

    # --- главный цикл ---
    def tick(self, now=None):
        now = now or time.time()
        config.reexec_if_requested("supervisor")
        for name, fn, every in (("screen", self._screen_and_watch, SCREEN_S), ("cycle", self.cycle, CYCLE_S)):
            if now >= self.next[name]:
                econ = getattr(self, "it", None) and self.it.economy and name == "cycle"
                self.next[name] = now + every * (it.ECON_CYCLE_MULT if econ else 1)
                try:
                    fn()
                except Exception as e:  # noqa: BLE001 — упавшая задача не валит supervisor
                    print(f"{utcnow()} {name} упал: {type(e).__name__}: {e}", flush=True)
                    self.notify(report.service("task_fail", detail={"screen": "скринер и сверка с брокером", "cycle": "цикл торгового зала"}[name]))

    def _screen_and_watch(self):
        self.watch()
        self.screen()

    def run(self):
        lock = open(config.ROOT / ".supervisor.lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("supervisor уже запущен")
        self.it.on_start()
        try:
            self.watch()   # при старте: закрытое у брокера, пока офис был выключен, попадает в журнал и в Telegram
        except Exception as e:  # noqa: BLE001
            print(f"{utcnow()} стартовая сверка: {type(e).__name__}: {str(e)[:120]}", flush=True)
        self.notify(report.service("start"))
        threading.Thread(target=self.research_loop, name="research", daemon=True).start()
        threading.Thread(target=tgbot.TgBot(self).run, name="tgbot", daemon=True).start()
        while True:
            self.it.heartbeat()
            self.tick()
            time.sleep(30)


def lab_loop(sleep_s: int = 5):
    """Фоновая лаборатория: крипта и акции — две отдельные очереди (самые давно не тестированные первыми, core вперёд),
    чередуются 1 крипто : 2 акции; на каждом символе 3 таймфрейма × 11 семейств."""
    config.load_env()
    board = Board(config.ROOT / "board.db")
    client = AlpacaClient()
    while True:
        u = screener.universe(client)
        last = {r["symbol"]: r["t"] for r in board.q("SELECT symbol, MAX(ts) t FROM strategies GROUP BY symbol")}

        def queue(core, rest):
            return sorted(dict.fromkeys(core + rest), key=lambda s: (last.get(s, ""), s not in core))
        qc, qs = queue(config.CRYPTO_CORE, u["crypto"]), queue(config.STOCK_CORE, u["stock"])
        order = []
        while qc or qs:
            order += qc[:1] + qs[:2]
            qc, qs = qc[1:], qs[2:]
        for s in order[:60]:
            config.reexec_if_requested("lab")
            try:
                lab.test_symbol(board, s)
            except Exception as e:  # noqa: BLE001
                print(f"{utcnow()} lab {s}: {type(e).__name__}: {str(e)[:100]}", flush=True)
            time.sleep(sleep_s)
