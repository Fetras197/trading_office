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

from . import accuracy, config, data, events, lab, report, research, scheduler, screener
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
                                 lab=lambda s: self._lab(s), picture=self.research.ensure_picture)
        self.next = {"screen": 0.0, "cycle": 0.0, "summary": time.time() + SUMMARY_S}
        self.sched = scheduler.Scheduler(self.j, self.jobs())

    def scope(self) -> dict:
        return {"held": list(self.j.positions()), "candidates": list(self.candidates)}

    def jobs(self) -> list:
        """Исследовательский отдел: порядок в списке = порядок первичного наполнения при самом первом запуске."""
        R, J = self.research, scheduler.Job
        return [J("events", R.run_events, every=3600),
                J("macro", R.run_macro, at=["07:00"], catchup_h=8),
                J("macro_pre_event", self.pre_event_macro, every=300),
                J("news", R.run_news, every=3600),
                J("crypto", R.run_crypto, every=3600),
                J("sectors", R.run_sectors, at=["07:10"], catchup_h=8),
                J("portfolio", R.run_portfolio, at=["07:20"], catchup_h=8),
                J("picture", R.run_picture, at=["03:30", "07:30", "11:30", "15:30", "19:30", "23:30"], catchup_h=3)]

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
            self.notify(report.close_message(name, ev["pnl"], ev["pnl"] / cost if cost else 0.0, ev["reason"]))
            self.sched.trigger("portfolio")
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
        return out[:MAX_ASSETS]

    def cycle(self):
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
        elif r.status == "error":
            self.notify(report.service("asset_fail", r.asset))

    def _run_asset(self, symbol):
        if config.restart_requested():  # перезапуск ждёт: новые активы в цикл не берём
            return Result(symbol, "skipped", "ожидается перезапуск", "")
        return self.hall.run_asset(symbol)

    def summary(self):
        view, now = self.client.account_view(), utcnow()
        appr = self.board.q("SELECT symbol, name, tf FROM strategies WHERE approved=1 AND ts>=datetime('now','-1 day') ORDER BY oos_sharpe DESC LIMIT 3")
        text = report.summary_message(view, self.bench(), self.j.entries_on(now[:10]), report.lab_line(lab.stats(self.board), appr))
        acc = accuracy.role_accuracy(self.board)
        if acc:
            text += "\nТочность оценок: " + ", ".join(f"{report.ACC_RU[a['role']]} {a['accuracy'] * 100:.0f}%".replace(".", ",") for a in acc if a["role"] in report.ACC_RU)
        self.notify(text)

    # --- главный цикл ---
    def tick(self, now=None):
        now = now or time.time()
        config.reexec_if_requested("supervisor")
        for name, fn, every in (("screen", self._screen_and_watch, SCREEN_S), ("cycle", self.cycle, CYCLE_S), ("summary", self.summary, SUMMARY_S)):
            if now >= self.next[name]:
                self.next[name] = now + every
                try:
                    fn()
                except Exception as e:  # noqa: BLE001 — упавшая задача не валит supervisor
                    print(f"{utcnow()} {name} упал: {type(e).__name__}: {e}", flush=True)
                    self.notify(report.service("task_fail", detail={"screen": "скринер и сверка с брокером", "cycle": "цикл торгового зала", "summary": "сводка"}[name]))

    def _screen_and_watch(self):
        self.watch()
        self.screen()

    def run(self):
        lock = open(config.ROOT / ".supervisor.lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("supervisor уже запущен")
        self.notify(report.service("start"))
        threading.Thread(target=self.research_loop, name="research", daemon=True).start()
        while True:
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
