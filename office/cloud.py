"""Облачный офис: один проход = `python -m office cloud` (GitHub Actions, каждые 30–60 минут; состояние лежит в ветке `state`).
Проход: сверка с брокером -> задания исследовательского отдела по расписанию (Scheduler помнит последний запуск в journal.kv) -> скринер -> цикл зала -> вечерний/утренний отчёты.
Телефон офис больше не запускает: единственный канал связи с владельцем — сообщения в Telegram. Команды из чата (/статус, /позиции, /отчёт, /пауза, /продолжить, /закрыть) читаются на каждом проходе (commands.py); стоп-кран: переменная репозитория OFFICE_HALT=1 или /пауза."""
import concurrent.futures as cf
import datetime as dt
import os
import time

from . import desks, earnings, llm, rules, accuracy, config, data, events, memory, realmoney, report, research, review, scheduler, screener, shadow, smartmoney, views, lab, manage, interns, daybase, newsguard
from .alpaca import AlpacaBroker, AlpacaClient
from .board import Board
from .commands import Commands
from .hall import Hall, Result, utcnow
from .journal import Journal
from .llm import RoleRouter
from .risk import RiskGate

MAX_ASSETS = 24          # активов за проход (кандидаты скринера + ядро): без сигнала актив стоит одну загрузку свечей
FRESH_ROUNDS = 5        # новых раундов аналитиков за проход (бюджет Gemini)
WORKERS = 3


def _sym_of(broker, name: str) -> str:
    return broker.assets[name].alpaca.replace("/", "")


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
        self.shadow = shadow.Shadow(self.board, self.client.price, lambda: self.client.account_view()["equity"], lab_fn=lambda: lab.shadow_signals(self.board), start_fn=lambda: self.j.kv_get("start_equity"), journal=self.j,
                                   candles_fn=lambda n: data.candles(config.BY_NAME[n], "1h", **({"limit": 500} if "/" in n else {"rng": "60d"})))
        self.interns = interns.Interns(self.board, self.j, lambda: screener.universe(self.client)["stock"],
                                       lambda syms, days: data.alpaca_bars(syms, "stock", days=days, timeframe="1Day"),
                                       lambda a: data.candles(config.make_asset(a), "1h", **({"limit": 500} if "/" in a else {"rng": "60d"})), self.client.price)   # стажёры: бумажные, на правила не влияют
        self._in_evening = False
        try:
            self.client.base_fn = self._day_base          # «за день» в сообщениях — от последнего вечернего отчёта (daybase), а не от закрытия биржи у брокера
        except Exception:  # noqa: BLE001
            pass
        self.commands = Commands(self)
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
                J("interns", self.interns.step, every=3600),
                J("review", self.run_review, every=3600),
                J("summary", self.summary, every=6 * 3600),
                J("picture", R.run_picture, at=["03:30", "07:30", "11:30", "15:30", "19:30", "23:30"], catchup_h=3),
                J("briefing", self.briefing, at=["08:00"], catchup_h=3, bootstrap=False),
                J("evening", self.evening, at=["21:00"], catchup_h=3, bootstrap=False),
                J("weekly", self.weekly_with_council, at=["20:00"], weekday=6, catchup_h=6, bootstrap=False)]

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
        """Срочная новость. Актив в позиции: защита (newsguard) — совет закрывает по рынку или подтягивает стоп до −1% (пауза ей не мешает: это защита).
        Иначе — внеочередной разбор актива (не чаще раза в 30 минут), только если он в зоне внимания."""
        if asset in self.j.positions():
            info = newsguard.info_for(self, asset)
            if info:
                try:
                    newsguard.handle(self, asset, info)
                except Exception as e:  # noqa: BLE001 — сбой защиты не должен ронять проход, но владельцу о нём скажем
                    print(f"{utcnow()} newsguard {asset}: {type(e).__name__}: {str(e)[:150]}", flush=True)
                    self.notify_limited(f"newsguard:{asset}", f"⚠️ Срочная новость по {report.name(asset)}, но защитное решение выполнить не удалось ({type(e).__name__}). Проверьте позицию вручную.", hours=2)
            return
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
        view = self.client.account_view()
        r = self.j.realized(self._local_midnight_utc())                      # те же сутки (по Минску) и то же определение, что в вечернем отчёте
        day = (f"За сутки (по Минску): покупок {r['buys']}, закрыто сделок {r['closed']}"
               + (f", частичных фиксаций {r['partials']}" if r["partials"] else "") + f"; реализованный результат {report.signed(r['pnl'])} $ после комиссий")
        text = report.summary_message(view, self.bench(), r["buys"], "", day_line=day)
        cmp_ = views.compare_line(self.shadow.summary())
        if cmp_:
            text += "\n" + cmp_
        text += "\n\n" + "\n".join(views.desk_lines(desks.stats(self.j, "", view), "🏢 Отделы с начала работы:"))
        self.notify(text.strip())

    def briefing(self):
        """08:00 — утренний брифинг: картина дня, события, план."""
        pic = self.research.ensure_picture()
        rg = self.board.latest("regime", max_age_h=6)
        self.notify(views.briefing_text(pic, rg["data"] if rg else None, events.upcoming(self.board, 24), self.client.account_view(), self.j.positions(), self.candidates))

    def _day_base(self):
        return daybase.get(self.j, config.localnow().date().isoformat(), evening=self._in_evening)

    def evening(self):
        """21:00 — вечерний отчёт. «За день» — от предыдущего вечернего отчёта; после отправки сегодняшний вечер становится новой базой."""
        self._in_evening = True
        try:
            self._evening()
        finally:
            self._in_evening = False
        try:
            daybase.commit(self.j, float(self.client.account_view()["equity"]), config.localnow().date().isoformat())
        except Exception as e:  # noqa: BLE001
            print(f"{utcnow()} daybase: {type(e).__name__}: {str(e)[:120]}", flush=True)

    def _evening(self):
        """21:00 — вечерний отчёт: итоги дня, «Почему не вошли» (3 ближайших кандидата), теневые портфели, уроки, календарь."""
        since = self._local_midnight_utc()
        trades = self.j.q("SELECT asset, pnl, exit_reason FROM trades WHERE exit_ts>=? ORDER BY id", (since,))      # включая частичные фиксации
        opened = self.j.realized(since)["buys"]
        since24 = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat(timespec="seconds")
        lessons = [r["text"] for r in memory.recall(self.board, "lesson", days=1, limit=3)]
        txt = (views.evening_text(self.client.account_view(), self.bench(), trades, opened, self.shadow.summary(), lessons, events.upcoming(self.board, 24),
                                       {}, nogo=self.j.nogo_top(since24, 3), stock_open=self.stock_open, rules_line=rules.line(rules.status(self.j)),
                                       desk_rows=desks.stats(self.j, since, self.client.account_view())))
        orl = llm.or_line()
        self.notify(txt + ("\n\n" + orl if orl else "") + self._interns_lines(since))

    def weekly_with_council(self):
        """Воскресенье 20:00 по Минску: заседание совета директоров (бюджеты, закрытие отделов, новые отделы под лабораторию), затем недельный отчёт."""
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat(timespec="seconds")
        decisions = desks.council(self.j, self.board, self.client.account_view()["equity"], utcnow(), since)
        self.notify("🏛 Совет директоров\n" + "\n".join(f"• {d}" for d in decisions))
        self._interns_council(since)
        self.weekly()

    def weekly(self):
        """Воскресенье 20:00 — недельное собрание (без предложений по весам: подтвердить «да» в облаке некому)."""
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat(timespec="seconds")
        from . import weights
        trades = self.j.q("SELECT asset, pnl, exit_reason FROM trades WHERE exit_ts>=?", (since,))
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
        self.notify(views.weekly_text(self.shadow.summary(since), trades, lessons, acc, [], {}, insight, realmoney.progress_line(realmoney.progress(self.board, self.j)) + "\n" + rules.line(rules.status(self.j))
                                       + "\n\n" + "\n".join(views.desk_lines(desks.stats(self.j, since, self.client.account_view()), "🏢 Отделы за неделю:")) + self._interns_lines(since, "🎓 Стажёры за неделю (бумажные, на боевые правила не влияют):")))

    def _interns_lines(self, since: str, title: str | None = None) -> str:
        """Строки стажёров для вечернего и недельного отчётов; сбой стажёров не должен ломать отчёт."""
        try:
            return "\n\n" + (self.interns.lines(since, title) if title else self.interns.lines(since))
        except Exception as e:  # noqa: BLE001
            print(f"{utcnow()} interns lines: {type(e).__name__}: {str(e)[:120]}", flush=True)
            return ""

    def _interns_council(self, since: str):
        """Воскресный совет: сравнение стажёров с боевыми отделами и предложения владельцу (кого перевести в боевые, кого уволить). Ничего не применяется автоматически."""
        try:
            self.notify(self.interns.council_text(self.j, [r for r in desks.stats(self.j, since, self.client.account_view()) if r["status"] == "active"], dt.datetime.now(dt.timezone.utc)))
        except Exception as e:  # noqa: BLE001
            print(f"{utcnow()} interns council: {type(e).__name__}: {str(e)[:120]}", flush=True)

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
            t = self.j.q("SELECT entry, qty, desk FROM trades WHERE asset=? ORDER BY id DESC LIMIT 1", (name,))
            cost = t[0]["entry"] * t[0]["qty"] if t else 0
            init = self.j.kv_get(f"init_stop:{name}")
            planned = RiskGate.planned_risk(config.COSTS[config.BY_NAME[name].kind], t[0]["entry"], init, t[0]["qty"]) if t and init else None
            self.notify(report.close_message(name, ev["pnl"], ev["pnl"] / cost if cost else 0.0, ev["reason"], ev.get("offline", False), planned, desks.name_of(self.j, (t[0].get("desk") if t else None))))
        for sym in self.broker.orphans():   # на брокере есть позиция, которой нет в журнале (например, вторая копия офиса) — не игнорируем
            if self.j.kv_get(f"orphan:{sym}") is None:
                self.j.kv_set(f"orphan:{sym}", utcnow())
                self.notify(f"⚠️ У брокера открыта позиция {report.pos_name(sym)}, которой нет в журнале офиса. Проверьте её вручную: офис её не сопровождает. "
                            "Если офис ещё запущен на телефоне, остановите его в Termux: ./office.sh stop")
        eq = self.broker.equity()
        self.j.snap_equity(utcnow(), eq)
        if self.j.kv_get("start_equity") is None:
            self.j.kv_set("start_equity", eq)

    def protection_guard(self) -> list[dict]:
        """Каждый проход: у каждой позиции из журнала на бирже должен стоять стоп (акции — ещё и цель). Нет — восстанавливаем по сохранённым уровням и пишем в Telegram.
        Висящий рыночный приказ продажи при закрытой бирже (след сбоя закрытия по новости) снимается, а решение «закрыть» переносится на открытие биржи (очередь newsguard)."""
        out = []
        try:
            live = self.broker.live_positions()
        except Exception as e:  # noqa: BLE001
            print(f"{utcnow()} защитные заявки: нет списка позиций ({type(e).__name__})", flush=True)
            return out
        for name in list(self.j.positions()):
            try:
                if _sym_of(self.broker, name) not in live:
                    continue
                res = self.broker.verify_protection(name, market_open=self.stock_open or "/" in name)
                if not res:
                    continue
                if res["stray_market"] and "/" not in name and not self.j.kv_get(newsguard.KEY + name):
                    self.j.kv_set(newsguard.KEY + name, {"why": "решение совета о закрытии: рыночный приказ остался в очереди биржи, закрою при открытии", "head": f"🛡 Отложенное закрытие {report.name(name)}", "ts": utcnow()})
                if res["error"]:
                    self.notify_limited(f"protect:{name}", report.protection_message(name, res), hours=2)       # не удалось восстановить: не чаще раза в 2 часа
                else:
                    self.notify(report.protection_message(name, res))
                out.append(res)
            except Exception as e:  # noqa: BLE001 — сбой по одной позиции не мешает остальным
                print(f"{utcnow()} защитные заявки {name}: {type(e).__name__}: {str(e)[:150]}", flush=True)
                self.notify_limited(f"protect_fail:{name}", f"⚠️ Не удалось проверить защитные заявки {report.name(name)} ({type(e).__name__}) — проверьте стоп на бирже вручную.", hours=2)
        return out

    def earnings_guard(self) -> list[str]:
        """Правило риска: позиция в акции закрывается заранее, если отчёт компании сегодня или на следующий торговый день (только в часы биржи; первый же проход накануне)."""
        done = []
        if not self.stock_open:
            return done
        for name in [n for n in self.j.positions() if "/" not in n]:
            try:
                why = earnings.must_exit(self.client, self.board, name)
                if not why:
                    continue
                kind = "peer" if why.startswith("отчёт крупного конкурента") else "earnings"      # конкурент из той же отрасли (гэп, как KLAC после LRCX) или сама компания
                with self.hall.exec_lock:
                    desk = self.j.positions()[name].get("desk")
                    r = self.broker.sell(name, float(self.client.price(name)), utcnow(), kind)
                t = self.j.q("SELECT entry, qty FROM trades WHERE asset=? ORDER BY id DESC LIMIT 1", (name,))
                cost = t[0]["entry"] * t[0]["qty"] if t else 0
                self.notify(report.close_message(name, r["pnl"], r["pnl"] / cost if cost else 0.0, kind, desk=desks.name_of(self.j, desk)) + f"\n{why}")
                self.sched.trigger("portfolio")
                self.sched.trigger("review")
                done.append(name)
            except Exception as e:  # noqa: BLE001 — сбой по одной акции не мешает остальным
                print(f"{utcnow()} отчёт-гард {name}: {type(e).__name__}: {str(e)[:150]}", flush=True)
                self.notify_limited(f"earnings_guard:{name}", f"⚠️ Не удалось закрыть {report.name(name)} перед отчётом компании — проверьте позицию вручную.", hours=2)
        return done

    def screen(self):
        """Сканер рынка (без LLM): весь ликвидный список Alpaca → 10–20 кандидатов для отделов; список крупной крипты запоминается для маршрутизации."""
        self.stock_open = bool(self.client.clock().get("is_open"))
        self.hall.stock_open = self.stock_open
        majors: list[str] = []
        top = screener.scan(self.board, self.client, self.stock_open, exclude=set(self.j.positions()), majors_out=majors)
        if majors:
            self.j.kv_set("major_crypto", majors)
        self.candidates = [r["symbol"] for r in top]

    def targets(self) -> list[str]:
        """Кого разбирать в этом проходе: кандидаты сканера по убыванию оценки, затем ядро крипты. Открытые позиции ведёт manage.py независимо от сканера."""
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
        if os.environ.get("OFFICE_HALT", "").strip() in ("1", "true", "yes") or self.j.kv_get("paused"):   # пауза: переменная репозитория или команда /пауза (хранится в office.db)
            config.STOP_FILE.touch()
        for name in filter(None, (x.strip() for x in os.environ.get("OFFICE_FORCE_JOBS", "").split(","))):   # ручной запуск: принудительно выполнить задания отдела сейчас
            self.sched.trigger(name)
        try:   # статус биржи нужен ещё до сканера (вечерний отчёт и т.п. идут в исследовательском отделе раньше)
            self.stock_open = bool(self.client.clock().get("is_open"))
        except Exception:  # noqa: BLE001
            pass
        desks.seed(self.j, utcnow())
        self.hall.stock_open = self.stock_open
        rules.register(self.j, self.notify)
        if self.j.kv_get("cloud_started") is None:
            self.j.kv_set("cloud_started", utcnow())
            self.notify("☁️ Офис переехал в облако (GitHub Actions): проход каждые 30–60 минут, телефон больше не нужен. Сообщения о сделках, сводки и отчёты приходят сюда.")
        for name, fn in (("сверка с брокером", self.watch), ("закрытие перед отчётами", self.earnings_guard), ("защитные заявки на бирже", self.protection_guard), ("отложенные закрытия по новостям", lambda: newsguard.run_queue(self)), ("команды Telegram", self.commands.poll), ("исследовательский отдел", self.sched.run_due)):
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
