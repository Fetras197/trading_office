#!/data/data/com.termux/files/usr/bin/bash
# Торговый зал (paper). Запускать ТОЛЬКО из Termux: ./office.sh start|stop|restart|status|halt|resume|watchdog|once|log
#   start    supervisor (скринер 15м, циклы зала, сводки) + лаборатория (фон, nice)
#   stop     остановить оба процесса (watchdog их больше не поднимает)
#   halt     новые решения запрещены (STOP-файл), процессы и защитные стопы работают; resume снимает
#   watchdog поднять упавшее (для crontab, не трогает остановленное через stop)
# DIR — путь в Termux (pid/логи/STOP), UDIR — тот же каталог внутри Ubuntu (там лежит .venv: editable-пакеты ссылаются на /root/projects/...)
export PATH=/data/data/com.termux/files/usr/bin:$PATH  # cron приходит с урезанным PATH
DIR=/data/data/com.termux/files/home/projects/trading_office
UDIR=/root/projects/trading_office
PD="proot-distro login ubuntu --shared-tmp --bind /data/data/com.termux/files/home/projects:/root/projects --"
IN="cd $UDIR && set -a && . ./.env && set +a && export PATH=/root/.local/bin:\$PATH UV_LINK_MODE=copy PYTHONPATH=$UDIR/vendor/TradingAgents && exec nice -n"
cd "$DIR" || exit 1

alive() { local p; p=$(cat "$DIR/.$1.pid" 2>/dev/null) || return 1; [ -n "$p" ] && kill -0 "$p" 2>/dev/null && tr '\0' ' ' < /proc/$p/cmdline | grep -q "office $2"; }

launch() {  # имя команда nice
  alive "$1" "$2" && return 0
  setsid nohup proot-distro login ubuntu --no-kill-on-exit --shared-tmp --bind /data/data/com.termux/files/home/projects:/root/projects -- bash -c "$IN $3 .venv/bin/python -m office $2" >>"$DIR/logs/$1.log" 2>&1 </dev/null &
  echo $! > "$DIR/.$1.pid"; disown
}

start() {
  command -v termux-wake-lock >/dev/null && termux-wake-lock  # без wakelock Android усыпляет процессы
  rm -f "$DIR/.user_stopped"; mkdir -p "$DIR/logs"
  launch supervisor supervise 5; [ -e "$DIR/NO_LAB" ] || launch lab lab 15   # NO_LAB: лабораторию не запускать (экономия памяти)
  sleep 3; status
}

status() {
  for x in "supervisor supervise" "lab lab"; do set -- $x; alive "$1" "$2" && echo "$1: работает (pid $(cat "$DIR/.$1.pid"))" || echo "$1: не запущен"; done
  [ -e "$DIR/STOP" ] && echo "HALT: новые решения запрещены" || echo "торговля разрешена"
  $PD bash -c "cd $UDIR && set -a && . ./.env && set +a && .venv/bin/python -m office status" 2>&1 | grep -v "no-kill-on-exit" | tail -12
}

case "$1" in
  start) start ;;
  stop) touch "$DIR/.user_stopped"; for n in supervisor lab; do [ -f "$DIR/.$n.pid" ] && kill "$(cat "$DIR/.$n.pid")" 2>/dev/null; rm -f "$DIR/.$n.pid"; done
        $PD pkill -f "python -m office" 2>/dev/null; echo "остановлено" ;;
  status) status ;;
  restart) "$0" stop; sleep 3; "$0" start ;;
  reload) touch "$DIR/RESTART"; echo "RESTART: процессы перезапустят себя сами (между задачами)" ;;
  halt) touch "$DIR/STOP"; echo "HALT: новые решения запрещены; позиции под защитными стопами" ;;
  resume) rm -f "$DIR/STOP"; echo "RESUME" ;;
  watchdog) [ -e "$DIR/.user_stopped" ] && exit 0
        mkdir -p "$DIR/logs"; termux-battery-status > "$DIR/logs/health.json.tmp" 2>/dev/null && mv "$DIR/logs/health.json.tmp" "$DIR/logs/health.json"   # запасной источник заряда для IT-отдела
        if ! { alive supervisor supervise && { [ -e "$DIR/NO_LAB" ] || alive lab lab; }; }; then
          echo "$(date -Is) watchdog: перезапуск" >>"$DIR/logs/watchdog.log"
          alive supervisor supervise || /data/data/com.termux/files/home/.local/bin/tgnotify "⚠️ Торговый офис не отвечал и был перезапущен сторожем. Позиции всё это время под защитными стопами брокера." >/dev/null 2>&1
          start >/dev/null 2>&1
        fi ;;
  once) shift; $PD bash -c "$IN 0 .venv/bin/python -m office once $*" ;;
  log) tail -n "${2:-60}" "$DIR/logs/${3:-supervisor}.log" ;;
  *) echo "usage: $0 start|stop|restart|reload|status|halt|resume|watchdog|once SYMBOL [--dry-run]|log [N] [supervisor|lab]"; exit 1 ;;
esac
