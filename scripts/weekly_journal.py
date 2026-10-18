"""Недельный журнал офиса для коммита в master (защита от отключения расписания GitHub после 60 дней без активности).
Только stdlib: читает office.db из ветки state (путь — аргумент), пишет journal/YYYY-Www.md."""
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

state, out = Path(sys.argv[1]), Path(sys.argv[2])
now = dt.datetime.now(dt.timezone.utc)
since = (now - dt.timedelta(days=7)).isoformat(timespec="seconds")
db = sqlite3.connect(state / "office.db")
q = lambda sql, a=(): db.execute(sql, a).fetchall()  # noqa: E731
eq = q("SELECT ts, equity FROM equity ORDER BY ts DESC LIMIT 1")
eq0 = q("SELECT equity FROM equity WHERE ts>=? ORDER BY ts LIMIT 1", (since,))
trades = q("SELECT asset, pnl, exit_reason FROM trades WHERE exit_ts>=? AND exit_reason!='partial'", (since,))
buys = q("SELECT COUNT(*) FROM fills WHERE side='buy' AND ts>=?", (since,))[0][0]
nogo = q("SELECT stage, COUNT(*) FROM nogo WHERE ts>=? GROUP BY stage ORDER BY 2 DESC", (since,))
try:
    budget = json.loads((state / "logs" / "gemini_budget.json").read_text())
except (OSError, ValueError):
    budget = {}
iso = now.isocalendar()
lines = [f"# Журнал офиса — неделя {iso.year}-W{iso.week:02d}", "", f"Сформирован {now:%Y-%m-%d %H:%M} UTC (автоматически, workflow keepalive).", "",
         f"- Эквити: {eq[0][1]:,.2f} $ на {eq[0][0][:16]}" if eq else "- Эквити: нет данных"]
if eq and eq0 and eq0[0][0]:
    lines.append(f"- За 7 дней: {eq[0][1] / eq0[0][0] - 1:+.2%}")
lines += [f"- Покупок: {buys}; закрыто сделок: {len(trades)}, итог {sum(t[1] for t in trades):+.2f} $",
          "- Отсечки кандидатов за неделю: " + (", ".join(f"{s} {n}" for s, n in nogo) or "нет"),
          f"- Бюджет Gemini за последние сутки: {budget}", ""]
out.mkdir(parents=True, exist_ok=True)
(out / f"{iso.year}-W{iso.week:02d}.md").write_text("\n".join(lines))
print("\n".join(lines))
