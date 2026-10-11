"""Оркестратор: цикл раз в N часов по активам. Команды: run | once | watch | status | stop | resume | accuracy."""
import argparse
import datetime as dt
import fcntl
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from . import accuracy, config, data, telegram
from .alpaca import AlpacaBroker, AlpacaClient, AlpacaError
from .brains import LLMBrain, RuleBrain
from .journal import Journal
from .llm import make_backend
from .office import Office, utcnow


def build(approve_mode: str):
    config.load_env()
    j = Journal(config.DB_PATH)
    client = AlpacaClient()
    broker = AlpacaBroker(client, j, config.ASSETS)
    brain = LLMBrain(make_backend("router"), lessons=lambda: (j.kv_get("lessons") or [])[-5:])
    approve = None
    if approve_mode == "telegram":
        if not telegram.configured():
            raise SystemExit("нет Telegram-кредов (TG_TOKEN/TG_CHAT или tgnotify conf); для работы без кнопок: --auto")
        approve = lambda name, ticket, reason: telegram.ask_approval(name, ticket, reason)  # noqa: E731
    return j, client, broker, Office(brain, broker, j, approve=approve, shadow=RuleBrain()), brain


def fetch_all(client):
    """Свечи по всем активам параллельно. Акции при закрытом рынке исключаются."""
    def one(a):
        try:
            df = data.candles(a, "1h", **({"limit": 500} if a.kind == "crypto" else {"rng": "60d"}))
            return a, df, None
        except Exception as e:  # noqa: BLE001
            return a, None, str(e)[:100]
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(one, config.ASSETS))
    out = {}
    for a, df, err in res:
        if df is not None and len(df) >= 210:
            out[a.name] = (a, df)
    return out


def cycle(approve_mode="telegram", only=None):
    if config.STOP_FILE.exists():
        print("STOP-файл есть — цикл пропущен")
        return
    lock = open(config.ROOT / ".cycle.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # два цикла одновременно = гонка по лимитам
    except OSError:
        print("другой цикл уже идёт — пропуск")
        return
    _cycle(approve_mode, only)


def _cycle(approve_mode, only):
    j, client, broker, office, brain = build(approve_mode)
    now = utcnow()
    market = fetch_all(client)
    prices = {n: float(df["close"].iloc[-1]) for n, (a, df) in market.items()}
    for name, ev in broker.sync(prices, now):
        telegram.notify(f"🔔 {name}: позиция закрыта ({ev['reason']}) pnl {ev['pnl']:.2f}")
    eq = broker.equity()
    j.snap_equity(now, eq)
    halted = office.gate.trading_halted(eq, office.day_start_equity(now, eq))
    if halted:
        telegram.notify(f"⛔ Дневной стоп {config.Limits().daily_stop:.0%}: новые входы заблокированы до следующих UTC-суток")
    results = []
    held = set(j.positions())
    for name in sorted(market, key=lambda n: n not in held):  # сначала активы с позициями
        a, df = market[name]
        if only and name not in only:
            continue
        if halted and name not in held:
            continue
        if a.kind == "stock" and not data.market_open(df):
            results.append((name, "skipped", "рынок закрыт"))
            continue
        try:
            bundle = data.bundle(a, df)
            r = office.step(a, bundle, now, prices)
        except Exception as e:  # noqa: BLE001 — сбой одного актива не валит цикл
            results.append((name, "error", f"{type(e).__name__}: {e}"[:150]))
            continue
        results.append((r.asset, r.status, r.detail))
        print(f"{r.asset}: {r.status} {r.detail}")
        if r.status == "error":
            telegram.notify(f"❗ {r.asset}: {r.detail}")
    accuracy.settle(j, lambda n: market[n][1] if n in market else data.candles(config.BY_NAME[n], "1h", limit=500))
    acc = accuracy.role_accuracy(j, shadow=0)
    dg = brain.digest({"results": results, "equity": eq, "accuracy": acc, "trades": j.q("SELECT * FROM trades ORDER BY id DESC LIMIT 5")})
    if dg.get("lessons"):
        j.kv_set("lessons", ((j.kv_get("lessons") or []) + dg["lessons"])[-20:])
    lines = [f"📊 Цикл {now[:16]}Z  equity ${eq:,.0f}"] + [f"• {n}: {s} {d}" for n, s, d in results] + [dg.get("digest", "")]
    telegram.notify("\n".join(lines))


def watch():
    """Без LLM: сверка стопов/тейков с брокером, settle исходов. Для cron раз в 15 мин."""
    j, client, broker, office, _ = build("auto")
    market = fetch_all(client)
    prices = {n: float(df["close"].iloc[-1]) for n, (a, df) in market.items()}
    for name, ev in broker.sync(prices, utcnow()):
        telegram.notify(f"🔔 {name}: позиция закрыта ({ev['reason']}) pnl {ev['pnl']:.2f}")
    j.snap_equity(utcnow(), broker.equity())


def status():
    j, client, broker, office, _ = build("auto")
    acc = client.account()
    print(json.dumps({"equity": acc["equity"], "cash": acc["cash"], "status": acc["status"], "STOP": config.STOP_FILE.exists()}))
    print("позиции:", [(n, p["qty"], p["avg"], p["stop"], p["take"]) for n, p in j.positions().items()])
    print("входов сегодня:", j.entries_on(utcnow()[:10]))
    for r in j.q("SELECT ts, asset, summary FROM runs ORDER BY id DESC LIMIT 7"):
        print(r["ts"][:16], r["asset"], (r["summary"] or "")[:90])
    print("точность ролей:", accuracy.role_accuracy(j, shadow=0))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="office")
    ap.add_argument("cmd", choices=["run", "once", "watch", "status", "stop", "resume", "accuracy"])
    ap.add_argument("--interval", type=float, default=4.0, help="часы между циклами (run)")
    ap.add_argument("--auto", action="store_true", help="без кнопок Telegram — исполнять сразу (в рамках RiskGate)")
    ap.add_argument("--only", help="список активов через запятую (once)")
    a = ap.parse_args(argv)
    mode = "auto" if a.auto else "telegram"
    if a.cmd == "stop":
        config.STOP_FILE.touch()
        print("STOP создан: новые решения не принимаются (позиции под защитными стопами)")
    elif a.cmd == "resume":
        config.STOP_FILE.unlink(missing_ok=True)
        print("STOP снят")
    elif a.cmd == "status":
        status()
    elif a.cmd == "watch":
        watch()
    elif a.cmd == "accuracy":
        config.load_env()
        print(json.dumps(accuracy.role_accuracy(Journal(config.DB_PATH)), ensure_ascii=False, indent=1))
    elif a.cmd == "once":
        cycle(mode, set(a.only.split(",")) if a.only else None)
    else:
        while True:
            t0 = time.time()
            try:
                cycle(mode)
            except (AlpacaError, OSError) as e:
                telegram.notify(f"❗ Цикл упал: {type(e).__name__}: {e}"[:300])
            print(f"{dt.datetime.now():%F %T} цикл готов, следующий через {a.interval} ч", flush=True)
            while time.time() - t0 < a.interval * 3600:
                time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
