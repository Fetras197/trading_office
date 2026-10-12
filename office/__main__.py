"""CLI: python -m office supervise | lab | once SYMBOL [--dry-run] | screen | status | halt | resume | accuracy"""
import argparse
import json
import sys

from . import accuracy, config, report
from .hall import utcnow


def main(argv=None):
    ap = argparse.ArgumentParser(prog="office")
    ap.add_argument("cmd", choices=["supervise", "lab", "once", "screen", "status", "halt", "resume", "accuracy", "summary"])
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
    elif a.cmd == "lab":
        from .supervisor import lab_loop
        lab_loop()
    elif a.cmd == "supervise":
        from .supervisor import Supervisor
        Supervisor().run()
    else:
        from .supervisor import Supervisor
        s = Supervisor()
        if a.cmd == "once":
            if not a.symbol:
                ap.error("once требует SYMBOL")
            s.hall.auto_trade = not a.dry_run
            s.watch()
            r = s.hall.run_asset(a.symbol)
            print(json.dumps({"asset": r.asset, "status": r.status, "detail": r.detail, "cycle_id": r.cycle_id, "order": r.order}, ensure_ascii=False, default=str))
            print(report.chain_text(s.board, r.cycle_id, r.asset))
            if r.status in ("opened", "closed"):
                s.notify(report.trade_message(s.board, r))
        elif a.cmd == "screen":
            s.stock_open = bool(s.client.clock().get("is_open"))
            s.screen()
            print("кандидаты:", s.candidates, "| акции открыты:", s.stock_open)
        elif a.cmd == "summary":
            s.summary()
        elif a.cmd == "accuracy":
            print(json.dumps(accuracy.role_accuracy(s.board), ensure_ascii=False, indent=1))
        elif a.cmd == "status":
            acc = s.client.account()
            print(json.dumps({"equity": acc["equity"], "cash": acc["cash"], "status": acc["status"], "HALT": config.STOP_FILE.exists(), "now": utcnow()}))
            print("позиции:", [(n, p["qty"], p["avg"], p["stop"], p["take"]) for n, p in s.j.positions().items()])
            print("входов сегодня:", s.j.entries_on(utcnow()[:10]))
            print("кандидаты:", [r["symbol"] for r in s.board.q("SELECT symbol FROM candidates ORDER BY rank")])
            print("approved стратегий:", s.board.q("SELECT COUNT(DISTINCT symbol||name) n FROM strategies WHERE approved=1")[0]["n"],
                  "| протестировано символов:", s.board.q("SELECT COUNT(DISTINCT symbol) n FROM strategies")[0]["n"])
            for r in s.board.q("SELECT cycle_id, asset, role, verdict FROM reports WHERE role IN ('trader') ORDER BY id DESC LIMIT 5"):
                print(r["asset"], r["cycle_id"], r["verdict"][:80])
    return 0


if __name__ == "__main__":
    sys.exit(main())
