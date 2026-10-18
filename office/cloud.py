"""Облачный офис: один проход = `python -m office cloud` (GitHub Actions, каждые 30–60 минут; состояние лежит в ветке `state`).
Проход: сверка с брокером -> задания исследовательского отдела по расписанию (Scheduler помнит последний запуск в journal.kv) -> скринер -> цикл зала -> вечерний/утренний отчёты.
Телефон офис больше не запускает: единственный канал связи с владельцем — сообщения в Telegram. Команд из чата нет (стоп-кран: переменная репозитория OFFICE_HALT=1)."""
import concurrent.futures as cf
import datetime as dt
import os
import time

from . import accuracy, config, data, events, memory, realmoney, report, research, review, scheduler, screener, shadow, smartmoney, views, lab, manage
from .alpaca import AlpacaBroker, AlpacaClient
from .board import Board
from .hall import Hall, Result, utcnow
from .journal import Journal
from .llm import RoleRouter

MAX_ASSETS = 16          # активов за проход (кандидаты скринера + ядро): без сигнала актив стоит одну загрузку свечей
FRESH_ROUNDS = 3        # новых раундов аналитиков за проход (бюджет Gemini)
WORKERS = 3


class Office:
    def __init__(self, board=None, journal=None, client=None, hall=None, notify=report.notify, router=None):
        config.load_env()
        self.board = board or Board(config.BOARD_PATH)
        self.j = journal or Journal(config.DB_PATH)
        self.client = client or AlpacaClient()
        self.broker = AlpacaBroker(self.client, self.j)
        self.notify = notify
        self.router = router or RoleRouter()
        self.candidates: list[str] = [r["symbol"] for r in self.board.q("SELECT symbol FROM candidates ORDER BY rank") if not config.excluded(r["symbol"])]   # итог скринера прошлого прохода
        self.stock_open = False
        self.research = research.Research(self.board, self.j, self.client, self.router, self.scope, notify=notify)
        self.research.on_urgent = self.urgent
        self.hall = hall or Hall(self.board, self.j, self.broker, self.router, notify=notify, smart=smartmoney.signal_for, fresh_rounds=FRESH_ROUNDS)
        self.manager = manage.Manager(self.broker, self.j, self.client, notify)
        self.shadow = shadow.Shadow(self.board, self.client.price, lambda: self.client.account_view()["equity"], lab_fn=lambda: lab.shadow_signals(self.board), start_fn=lambda: self.j.kv_get("start_equity"))
        self.sched = scheduler.Scheduler(self.j, self.jobs())

    # ---------- расписание исследовательского отдела (порядок = порядок первичного наполнения) ----------
    def scope(self) -> dict:
        return {"held": list(self.j.positions()), "candidates": list(self.candidates)}

    def jobs(self) -> list:
        R, J = self.research, scheduler.Job
        return [J("events", R.run_events, every=3600),
                J("regime", R.run_regime, every=1800),
                J("macro", R.run_macro, at=["07:00"], catchup_h=8),
                J("macro_pre_event", self.pre_event_macro, every=600),
                J("news", R.run_news, every=9000),
                J("crypto", R.run_crypto, every=7200),
                J("sectors", R.run_sectors, at=["07:10"], catchup_h=8),
                J("portfolio", R.run_portfolio, at=["07:20"], catchup_h=8),
                J("manage", self.manager.run, every=600),
                J("smartmoney", smartmoney.refresh, every=3 * 3600),
                J("realmoney", lambda: realmoney.check(self.board, self.j, self.notify, self.j), every=21600),
                J("shadow", self.shadow.step, every=3600),
                J("review", self.run_review, every=3600),
                J("summary", self.summary, every=6 * 3600),
                J("picture", R.run_picture, at=["03:30", "07:30", "11:30", "15:30", "19:30", "23:30"], catchup_h=3),
                J("briefing", self.briefing, at=["08:00"], catchup_h=3, bootstrap=False),
                J("evening", self.evening, at=["21:00"], catchup_h=3, bootstrap=False),
                J("weekly", self.weekly, at=["20:00"], weekday=6, catchup_h=6, bootstrap=False)]

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
        """Срочная новость: внеочередной разбор актива (не чаще раза в 30 минут), только если он в зоне внимания."""
        if asset not in self.scope()["held"] + self.candidates + list(config.CRYPTO_CORE) or config.STOP_FILE.exists():
            return
        k, now = f"urgent_run:{asset}", time.time()
        if now - (self.j.kv_get(k) or 0) < 1800:
            return
        self.j.kv_set(k, now)
        self._handle(self.hall.run_asset(asset))

    # ---------- сообщения ----------
    def notify_limited(self, key: str, text: str, hours: float = 6) -> None:
        """Служебное сообщение не чаще раза в `hours` (проход идёт каждые 30–60 минут — без этого сбой источника засыпал бы чат)."""
        k, now = f"notified:{key}", time.time()
        if now - (self.j.kv_get(k) or 0) < hours * 3600:
            return
        self.j.kv_set(k, now)
        self.notify(text)

    def _local_midnight_utc(self) -> str:
        m = config.localnow().replace(hour=0, minute=0, second=0, microsecond=0)
        return m.astimezone(dt.timezone.utc).isoformat(timespec="seconds")

    def bench(self) -> dict:
        out = {}
        for k, sym in (("SPY", "SPY"), ("BTC", "BTC/USD")):
            try:
                out[k] = self.client.day_change(sym)
            except Exception:  # noqa: BLE001 — сравнение не критично
                out[k] = 0.0
        return out

    def summary(self):
        view, now = self.client.account_view(), utcnow()
        text = report.summary_message(view, self.bench(), self.j.entries_on(now[:10]), "")
        cmp_ = views.compare_line(self.shadow.summary())
        if cmp_:
            text += "\n" + cmp_
        self.notify(text.strip())

    def briefing(self):
        """08:00 — утренний брифинг: картина дня, события, план."""
        pic = self.research.ensure_picture()
        rg = self.board.latest("regime", max_age_h=6)
        self.notify(views.briefing_text(pic, rg["data"] if rg else None, events.upcoming(self.board, 24), self.client.account_view(), self.j.positions(), self.candidates))

    def evening(self):
        """21:00 — вечерний отчёт: итоги дня, «Почему не вошли» (3 ближайших кандидата), теневые портфели, уроки, календарь."""
        since = self._local_midnight_utc()
        trades = self.j.q("SELECT asset, pnl, exit_reason FROM trades WHERE exit_ts>=? AND exit_reason!='partial' ORDER BY id", (since,))
        opened = self.j.q("SELECT COUNT(*) n FROM fills WHERE side='buy' AND ts>=?", (since,))[0]["n"]
        lessons = [r["text"] for r in memory.recall(self.board, "lesson", days=1, limit=3)]
        self.notify(views.evening_text(self.client.account_view(), self.bench(), trades, opened, self.shadow.summary(), lessons, events.upcoming(self.board, 24),
                                       {}, nogo=self.j.nogo_top(since, 3)))

    def weekly(self):
        """Воскресенье 20:00 — недельное собрание (без предложений по весам: подтвердить «да» в облаке некому)."""
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat(timespec="seconds")
        from . import weights
        trades = self.j.q("SELECT asset, pnl FROM trades WHERE exit_ts>=? AND exit_reason!='partial'", (since,))
        lessons = [r["text"] for r in memory.recall(self.board, "lesson", days=7, limit=5)]
        acc = [{"role": r, "n": v["n"], "accuracy": v["acc"]} for r, v in weights.stats(self.board).items()]
        insight = ""
        try:
            insight = str(self.router.ask("weekly", {"week_shadow": self.shadow.summary(since), "closed_trades": trades,
                                                     "open_positions": [report.pos_name(p["symbol"]) + f" {p['plpc']:+.1%}" for p in self.client.account_view()["positions"]], "lessons": lessons},
                                          system="You are the head of a paper-trading firm. In 3 short sentences in Russian (plain words, no English, up to 70 words) sum up the week: how we did vs the S&P 500 and bitcoin, what we learned. "
                                                 "Use only the input data. Output ONE JSON: {\"insight\": \"...\"}").get("insight", ""))[:600]
        except Exception:  # noqa: BLE001 — итоги недели не должны зависеть от лимитов
            pass
        self.notify(views.weekly_text(self.shadow.summary(since), trades, lessons, acc, [], {}, insight, realmoney.progress_line(realmoney.progress(self.board, self.j))))

    def run_review(self):
        """Разбор закрытых сделок — не больше 3 за проход."""
        review.run(self.board, self.j, self.router, lambda n: data.candles(config.BY_NAME[n], "1h", **({"limit": 500} if "/" in n else {"rng": "60d"})))

    # ---------- задачи прохода ----------
    def watch(self):
        """Сверка с брокером + эквити-снапшот; закрытия стопом/тейком -> Telegram."""
        prices, highs = {}, {}
        for n in self.j.positions():
            try:
                a = config.BY_NAME[n]
                df = data.candles(a, "1h", **({"limit": 8} if a.kind == "crypto" else {"rng": "5d"}))
                prices[n] = float(df["close"].iloc[-1])
                highs[n] = float(df["high"].tail(4).max())   # максимум за последние ~4 часа: касание тейка между проходами
            except Exception:  # noqa: BLE001
                pass
        for name, ev in self.broker.sync(prices, utcnow(), highs):
            t = self.j.q("SELECT entry, qty FROM trades WHERE asset=? ORDER BY id DESC LIMIT 1", (name,))
            cost = t[0]["entry"] * t[0]["qty"] if t else 0
            self.notify(report.close_message(name, ev["pnl"], ev["pnl"] / cost if cost else 0.0, ev["reason"], ev.get("offline", False)))
        for sym in self.broker.orphans():   # на брокере есть позиция, которой нет в журнале (например, вторая копия офиса) — не игнорируем
            if self.j.kv_get(f"orphan:{sym}") is None:
                self.j.kv_set(f"orphan:{sym}", utcnow())
                self.notify(f"⚠️ У брокера открыта позиция {report.pos_name(sym)}, которой нет в журнале офиса. Проверьте её вручную: офис её не сопровождает. "
                            "Если офис ещё запущен на телефоне, остановите его в Termux: ./office.sh stop")
        eq = self.broker.equity()
        self.j.snap_equity(utcnow(), eq)
        if self.j.kv_get("start_equity") is None:
            self.j.kv_set("start_equity", eq)

    def screen(self):
        self.stock_open = bool(self.client.clock().get("is_open"))
        held = set(self.j.positions())
        top = screener.scan(self.board, self.client, self.stock_open, top_n=10, exclude=held | set(config.CRYPTO_CORE + config.STOCK_CORE))
        self.candidates = [r["symbol"] for r in top]

    def targets(self) -> list[str]:
        """Кого разбирать в этом проходе: сначала кандидаты скринера (самые «горячие»), затем ядро. Открытые позиции ведёт manage.py."""
        held = set(self.j.positions())
        core = list(config.CRYPTO_CORE) + (list(config.STOCK_CORE) if self.stock_open else [])
        out = []
        for s in self.candidates + core:
            if s not in out and s not in held and not config.excluded(s) and (self.stock_open or "/" in s):   # акции вне часов биржи не торгуем
                out.append(s)
        return out[:MAX_ASSETS]

    def cycle(self) -> list[Result]:
        if config.STOP_FILE.exists():
            self.notify_limited("halt", "⏸ Режим паузы: новые покупки не совершаются, позиции остаются под защитными стопами.", hours=24)
        res = []
        with cf.ThreadPoolExecutor(WORKERS) as ex:
            for r in ex.map(self.hall.run_asset, self.targets()):
                res.append(r)
                self._handle(r)
        try:
            accuracy.settle(self.board, self.j, lambda n: data.candles(config.BY_NAME[n], "1h", **({"limit": 500} if "/" in n else {"rng": "60d"})))
        except Exception:  # noqa: BLE001
            pass
        return res

    def _handle(self, r: Result):
        print(f"{utcnow()} {r.asset}: {r.status} {r.detail}", flush=True)
        if r.status in ("opened", "closed"):
            self.notify(report.trade_message(self.board, r))
            self.sched.trigger("portfolio")   # портфельный аналитик после каждой сделки
            if r.status == "closed":
                self.sched.trigger("review")
        elif r.status == "error":
            self.notify_limited(f"asset_fail:{r.asset}", report.service("asset_fail", r.asset), hours=6)

    def run_once(self) -> list[Result]:
        """Один проход облачного офиса."""
        if os.environ.get("OFFICE_HALT", "").strip() in ("1", "true", "yes"):
            config.STOP_FILE.touch()
        for name in filter(None, (x.strip() for x in os.environ.get("OFFICE_FORCE_JOBS", "").split(","))):   # ручной запуск: принудительно выполнить задания отдела сейчас
            self.sched.trigger(name)
        if self.j.kv_get("cloud_started") is None:
            self.j.kv_set("cloud_started", utcnow())
            self.notify("☁️ Офис переехал в облако (GitHub Actions): проход каждые 30–60 минут, телефон больше не нужен. Сообщения о сделках, сводки и отчёты приходят сюда.")
        for name, fn in (("сверка с брокером", self.watch), ("исследовательский отдел", self.sched.run_due)):
            try:
                fn()
            except Exception as e:  # noqa: BLE001 — сбой одной части не должен ронять проход
                print(f"{utcnow()} {name}: {type(e).__name__}: {str(e)[:200]}", flush=True)
                self.notify_limited(f"task_fail:{name}", report.service("task_fail", detail=name), hours=6)
        res = []
        try:
            self.screen()
            res = self.cycle()
        except Exception as e:  # noqa: BLE001
            print(f"{utcnow()} цикл зала: {type(e).__name__}: {str(e)[:200]}", flush=True)
            self.notify_limited("task_fail:cycle", report.service("task_fail", detail="цикл торгового зала"), hours=6)
        return res
