#!/usr/bin/env bash
# scheduler.sh — planificador nocturno dentro del contenedor.
#
# Ejecuta `python main.py --modo cron` todos los días a la hora indicada por
# CRON_HOUR:CRON_MINUTE (por defecto 02:00, hora local del contenedor = TZ).
#
# Variables:
#   CRON_HOUR        hora de ejecución, 0-23            (def. 2)
#   CRON_MINUTE      minuto de ejecución, 0-59          (def. 0)
#   RUN_ON_START     "true" ejecuta una pasada al arrancar (def. false)
#   JOB_LOG_PATH     archivo de log (def. /data/logs/jobautomat.log)
#   JOB_LOG_MAX_MB   tamaño tras el cual se rota a .1     (def. 20)
#
# No usa el daemon cron del sistema: un bucle con `sleep` en PID 1 recibe
# SIGTERM correctamente (docker stop) y hereda el entorno del contenedor.
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
    # Evita solapar dos ejecuciones (p. ej. RUN_ON_START + llegada de la hora).
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
    # El log va a archivo y también a stdout para `docker logs`.
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

    # `sleep` en background + wait: el trap se atiende al instante con
    # `docker stop` aunque falten horas para la siguiente ejecución.
    sleep "$espera" &
    wait $! 2>/dev/null || true

    ejecutar 9>/tmp/jobautomat.lock
done
