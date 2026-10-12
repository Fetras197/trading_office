"""Supervisor 24/7. Тик 30 с. Расписание:
  15 мин: скринер (все крипто-пары + S&P500/NDX) -> top-5; сверка стопов/тейков с брокером (watch);
  1 ч:    полный цикл зала: крипта BTC/ETH/SOL; акции SPY/NVDA/AAPL/MSFT в часы биржи; + top-5 скринера + уже открытые позиции;
  6 ч:    сводка в Telegram.
Лаборатория — отдельный фоновый процесс (`python -m office lab`). STOP-файл = новые решения не принимаются."""
import concurrent.futures as cf
import fcntl
import time

from . import accuracy, config, data, lab, report, screener
from .alpaca import AlpacaBroker, AlpacaClient
from .board import Board
from .hall import Hall, utcnow
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
        self.hall = hall or Hall(self.board, self.j, self.broker, RoleRouter(), ClaudeBackend(), notify=notify,
                                 lab=lambda s: self._lab(s))
        self.next = {"screen": 0.0, "cycle": 0.0, "summary": time.time() + SUMMARY_S}
        self.candidates: list[str] = []
        self.stock_open = False

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
            for r in ex.map(self.hall.run_asset, self.targets()):
                res.append(r)
                print(f"{utcnow()} {r.asset}: {r.status} {r.detail}", flush=True)
                if r.status in ("opened", "closed"):
                    self.notify(report.trade_message(self.board, r))
                elif r.status == "error":
                    self.notify(report.service("asset_fail", r.asset))
        try:
            accuracy.settle(self.board, self.j, lambda n: data.candles(config.BY_NAME[n], "1h", **({"limit": 500} if "/" in n else {"rng": "60d"})))
        except Exception:  # noqa: BLE001
            pass
        return res

    def summary(self):
        def day_change(sym):
            try:
                a = config.make_asset(sym)
                d = data.candles(a, "1d", **({"limit": 5} if a.kind == "crypto" else {"rng": "10d"}))["close"]
                return float(d.iloc[-1] / d.iloc[-2] - 1)
            except Exception:  # noqa: BLE001
                return 0.0
        eq, now = self.broker.equity(), utcnow()
        day_start = self.j.kv_get(f"day_start:{now[:10]}") or eq
        positions = []
        for n, p in self.j.positions().items():
            try:
                a = config.BY_NAME[n]
                px = float(data.candles(a, "1h", **({"limit": 5} if a.kind == "crypto" else {"rng": "5d"}))["close"].iloc[-1])
                positions.append((n, px / p["avg"] - 1))
            except Exception:  # noqa: BLE001
                pass
        appr = self.board.q("SELECT symbol, name, tf FROM strategies WHERE approved=1 AND ts>=datetime('now','-1 day') ORDER BY oos_sharpe DESC LIMIT 3")
        text = report.summary_message(eq, day_start, {"SPY": day_change("SPY"), "BTC": day_change("BTC/USD")}, positions,
                                      self.j.entries_on(now[:10]), report.lab_line(lab.stats(self.board), appr))
        acc = accuracy.role_accuracy(self.board)
        if acc:
            text += "\nТочность оценок: " + ", ".join(f"{report.ACC_RU[a['role']]} {a['accuracy'] * 100:.0f}%".replace(".", ",") for a in acc if a["role"] in report.ACC_RU)
        self.notify(text)

    # --- главный цикл ---
    def tick(self, now=None):
        now = now or time.time()
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
            try:
                lab.test_symbol(board, s)
            except Exception as e:  # noqa: BLE001
                print(f"{utcnow()} lab {s}: {type(e).__name__}: {str(e)[:100]}", flush=True)
            time.sleep(sleep_s)
