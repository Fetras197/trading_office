#!/bin/bash
# Приёмка: гоняет разовые полные циклы (как делал бы supervisor) пока не откроется paper-позиция. Не демон: завершается после первой сделки/лимита попыток.
cd "$(dirname "$0")/.." || exit 1
export PATH=$HOME/.local/bin:$PATH
for round in $(seq 1 ${ROUNDS:-6}); do
  .venv/bin/python -m office screen >/dev/null 2>&1
  SYMS="BTC/USD ETH/USD SOL/USD $(.venv/bin/python -c "
from office.board import Board; print(' '.join(r['symbol'] for r in Board('board.db').q('SELECT symbol FROM candidates ORDER BY rank')))")"
  for s in $SYMS; do
    out=$(.venv/bin/python -m office once "$s" 2>&1 | head -1)
    echo "$(date -Is) r$round $out"
    case "$out" in *'"status": "opened"'*) echo OPENED; exit 0;; esac
  done
  sleep 600
done
echo "NO_TRADE"
