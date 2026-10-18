"""CLI: python -m office cloud | once SYMBOL [--dry-run] | screen | status | summary | accuracy | prune | halt | resume
Постоянного процесса нет: офис живёт в GitHub Actions (cloud — один проход), локально команды нужны для отладки."""
import argparse
import json
import sqlite3
import sys

from . import accuracy, config, report
from .hall import utcnow


def prune_board(path=None, keep_days: int = 14) -> None:
    """Урезать board.db для хранения в ветке state (лимит файла GitHub — 100 МБ): без лаборатории (strategies) и без старых отчётов."""
    path = path or config.BOARD_PATH
    db = sqlite3.connect(str(path))
    db.execute("DELETE FROM strategies")
    cut = f"-{keep_days} days"
    db.execute("DELETE FROM outcomes WHERE report_id IN (SELECT id FROM reports WHERE ts < datetime('now', ?))", (cut,))
    db.execute("DELETE FROM reports WHERE ts < datetime('now', ?) AND role NOT IN ('macro','picture')", (cut,))
    db.execute("DELETE FROM rejected WHERE ts < datetime('now', ?)", (cut,))
    db.execute("DELETE FROM shadow_eq WHERE ts < datetime('now', '-60 days')")
    db.commit()
    db.execute("VACUUM")
    db.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="office")
    ap.add_argument("cmd", choices=["cloud", "once", "screen", "status", "summary", "accuracy", "prune", "halt", "resume"])
    ap.add_argument("symbol", nargs="?", help="для once: BTC/USD, NVDA ...")
    ap.add_argument("--dry-run", action="store_true", help="once: пройти всю цепочку, но ордер не отправлять")
    a = ap.parse_args(argv)
    config.load_env()
    if a.cmd == "halt":
        config.STOP_FILE.touch()
        print("HALT: новые решения не принимаются (позиции остаются под защитными стопами)")
    elif a.cmd == "resume":
        config.STOP_FILE.unlink(missing_ok=True)
        print("RESUME: STOP снят")
    elif a.cmd == "prune":
        prune_board()
        print("board.db урезан")
    else:
        from .cloud import Office
        o = Office()
        if a.cmd == "cloud":
            res = o.run_once()
            print(json.dumps({"opened": [r.asset for r in res if r.status == "opened"], "assets": len(res),
                              "by_status": {s: sum(1 for r in res if r.status == s) for s in {r.status for r in res}}}, ensure_ascii=False))
        elif a.cmd == "once":
            if not a.symbol:
                ap.error("once требует SYMBOL")
            o.hall.auto_trade = not a.dry_run
            o.watch()
            r = o.hall.run_asset(a.symbol)
            print(json.dumps({"asset": r.asset, "status": r.status, "detail": r.detail, "cycle_id": r.cycle_id, "order": r.order}, ensure_ascii=False, default=str))
            print(report.chain_text(o.board, r.cycle_id, r.asset))
            if r.status in ("opened", "closed"):
                o.notify(report.trade_message(o.board, r))
        elif a.cmd == "screen":
            o.stock_open = bool(o.client.clock().get("is_open"))
            o.screen()
            print("кандидаты:", o.candidates, "| акции открыты:", o.stock_open)
        elif a.cmd == "summary":
            o.summary()
        elif a.cmd == "accuracy":
            print(json.dumps(accuracy.role_accuracy(o.board), ensure_ascii=False, indent=1))
        elif a.cmd == "status":
            acc = o.client.account()
            print(json.dumps({"equity": acc["equity"], "cash": acc["cash"], "status": acc["status"], "HALT": config.STOP_FILE.exists(), "now": utcnow()}))
            print("позиции:", [(n, p["qty"], p["avg"], p["stop"], p["take"]) for n, p in o.j.positions().items()])
            print("входов сегодня:", o.j.entries_on(utcnow()[:10]))
            print("кандидаты:", [r["symbol"] for r in o.board.q("SELECT symbol FROM candidates ORDER BY rank")])
            for r in o.j.nogo_top(utcnow()[:10], 5):
                print("не вошли:", r["asset"], r["stage"], r["reason"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
