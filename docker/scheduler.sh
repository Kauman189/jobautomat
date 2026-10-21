#!/usr/bin/env bash
# scheduler.sh — nightly scheduler inside the container.
#
# Runs `python main.py --modo cron` every day at the time given by
# CRON_HOUR:CRON_MINUTE (default 02:00, container local time = TZ).
#
# Variables:
#   CRON_HOUR        run hour, 0-23                     (def. 2)
#   CRON_MINUTE      run minute, 0-59                   (def. 0)
#   RUN_ON_START     "true" runs one pass at startup    (def. false)
#   JOB_LOG_PATH     log file (def. /data/logs/jobautomat.log)
#   JOB_LOG_MAX_MB   size after which it rotates to .1  (def. 20)
#
# It does not use the system cron daemon: a `sleep` loop in PID 1 receives
# SIGTERM correctly (docker stop) and inherits the container environment.
set -uo pipefail

HORA="${CRON_HOUR:-2}"
MINUTO="${CRON_MINUTE:-0}"
LOG="${JOB_LOG_PATH:-/data/logs/jobautomat.log}"
LOG_MAX_MB="${JOB_LOG_MAX_MB:-20}"
LOCK=/tmp/jobautomat.lock

log() { printf '%s scheduler: %s\n' "$(date -Is)" "$*" >&2; }

case "$HORA" in
    ''|*[!0-9]*) log "ERROR: CRON_HOUR debe ser numérico (0-23), recibido: $HORA"; exit 2 ;;
esac
case "$MINUTO" in
    ''|*[!0-9]*) log "ERROR: CRON_MINUTE debe ser numérico (0-59), recibido: $MINUTO"; exit 2 ;;
esac
if [ "$HORA" -lt 0 ] || [ "$HORA" -gt 23 ] || [ "$MINUTO" -lt 0 ] || [ "$MINUTO" -gt 59 ]; then
    log "ERROR: hora inválida ${HORA}:${MINUTO}"
    exit 2
fi

HHMM="$(printf '%02d:%02d' "$HORA" "$MINUTO")"
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

rotar_log() {
    [ -f "$LOG" ] || return 0
    local bytes limite
    bytes=$(wc -c < "$LOG" 2>/dev/null || echo 0)
    limite=$(( LOG_MAX_MB * 1024 * 1024 ))
    if [ "$bytes" -gt "$limite" ]; then
        mv -f "$LOG" "${LOG}.1"
    fi
}

ejecutar() {
    # Avoids overlapping two runs (e.g. RUN_ON_START + the hour arriving).
    local rc=0
    if command -v flock >/dev/null 2>&1; then
        flock -n 9 || { log "ya hay una ejecución en curso; se omite"; return 0; }
    fi

    log "===== inicio de ejecución $(date -Is) (modo cron) ====="
    rotar_log
    {
        printf '\n===== %s inicio =====\n' "$(date -Is)"
        python main.py --modo cron 2>&1
        rc=$?
        printf '===== %s fin (exit %s) =====\n' "$(date -Is)" "$rc"
    } >> "$LOG" 2>&1
    # The log goes to a file and also to stdout for `docker logs`.
    tail -n 40 "$LOG" 2>/dev/null >&2 || true
    log "fin de ejecución (exit $rc)"
    return 0
}

trap 'log "SIGTERM/SIGINT recibido; saliendo"; exit 0' TERM INT

log "scheduler arrancado: todos los días a las ${HHMM} (TZ=${TZ:-sin definir})"
log "log: ${LOG}"

if [ "${RUN_ON_START:-false}" = "true" ]; then
    log "RUN_ON_START=true: ejecución inmediata al arrancar"
    ejecutar 9>/tmp/jobautomat.lock
fi

while true; do
    ahora=$(date +%s)
    objetivo=$(date -d "today ${HHMM}" +%s 2>/dev/null) || {
        log "ERROR: 'date -d' no soportado en este sistema"; exit 2; }
    if [ "$objetivo" -le "$ahora" ]; then
        objetivo=$(date -d "tomorrow ${HHMM}" +%s)
    fi
    espera=$(( objetivo - ahora ))
    log "próxima ejecución: $(date -d "@${objetivo}" -Is) (en ${espera}s)"

    # `sleep` in the background + wait: the trap is served instantly on
    # `docker stop` even hours before the next run.
    sleep "$espera" &
    wait $! 2>/dev/null || true

    ejecutar 9>/tmp/jobautomat.lock
done
