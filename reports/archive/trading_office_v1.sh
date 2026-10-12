#!/data/data/com.termux/files/usr/bin/bash
# Управление фирмой в Termux: ./trading_office.sh start|stop|status|once|watch|halt|resume|log
cd "$(dirname "$0")" || exit 1
PID=.run.pid; LOG=office.log
PY=${PYTHON:-python3}
case "$1" in
  start)  [ -f $PID ] && kill -0 "$(cat $PID)" 2>/dev/null && { echo "уже запущено ($(cat $PID))"; exit 0; }
          nohup $PY -m office run --interval "${INTERVAL:-4}" ${AUTO:+--auto} >> $LOG 2>&1 & echo $! > $PID; echo "запущено, pid $(cat $PID)";;
  stop)   [ -f $PID ] && kill "$(cat $PID)" 2>/dev/null; rm -f $PID; echo "остановлено";;
  status) [ -f $PID ] && kill -0 "$(cat $PID)" 2>/dev/null && echo "работает (pid $(cat $PID))" || echo "не запущено"; $PY -m office status;;
  once)   shift; $PY -m office once "$@";;
  watch)  $PY -m office watch;;
  halt)   $PY -m office stop;;
  resume) $PY -m office resume;;
  log)    tail -n "${2:-50}" $LOG;;
  *) echo "usage: $0 start|stop|status|once|watch|halt|resume|log"; exit 1;;
esac
