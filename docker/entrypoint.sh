#!/usr/bin/env bash
# entrypoint.sh — prepara el entorno y despacha el comando.
#
# Modos:
#   scheduler        (por defecto) arranca el planificador nocturno.
#   once             ejecuta el pipeline una vez en modo cron (force-run).
#   <comando...>     ejecuta ese comando tal cual dentro de /app/service,
#                    p. ej.  python main.py --modo manual
#
# Responsabilidades:
#   1. Materializar /app/service/.env a partir del .env montado en sólo
#      lectura, forzando DB_PATH / PERFIL_PATH al volumen /data para que el
#      estado sobreviva al contenedor.
#   2. Sembrar el volumen de navegadores de patchright la primera vez.
#   3. Comprobar permisos de escritura en /data.
set -euo pipefail

SERVICE_DIR=/app/service
ENV_DEST="${SERVICE_DIR}/.env"

log() { printf '%s entrypoint: %s\n' "$(date -Is)" "$*" >&2; }

# --------------------------------------------------------------------------
# 1. .env del contenedor
# --------------------------------------------------------------------------
preparar_env() {
    local origen="${JOB_ENV_FILE:-/run/jobautomat/.env}"

    if [ ! -f "$origen" ]; then
        log "AVISO: no existe ${origen}; se usan los valores por defecto y las"
        log "       variables de entorno. Monta tu .env ahí o revisa el compose."
        return 0
    fi

    # Copia el .env quitando las rutas que gestiona el contenedor: DB_PATH,
    # PERFIL_PATH y cualquier *_SESSION_PATH apuntan al volumen /data.
    # (`|| true` porque grep devuelve 1 si no queda ninguna línea.)
    {
        grep -v -E '^[[:space:]]*(DB_PATH|PERFIL_PATH|[A-Z_]*SESSION[A-Z_]*PATH)[[:space:]]*=' \
            "$origen" || true
        printf '\n# --- rutas dentro del contenedor (gestionadas por el entrypoint) ---\n'
        printf 'DB_PATH=%s\n' "${DB_PATH:-/data/state.db}"
        printf 'PERFIL_PATH=%s\n' "${PERFIL_PATH:-/data/perfil.json}"
    } > "${ENV_DEST}.tmp"

    mv "${ENV_DEST}.tmp" "$ENV_DEST"
    chmod 600 "$ENV_DEST" 2>/dev/null || true
    log "configuración materializada en ${ENV_DEST} (origen: ${origen})"

    # Aviso temprano si faltan secretos (config.py fallará igual, pero con
    # este mensaje se ve antes en los logs del scheduler).
    local faltan=()
    grep -qE '^[[:space:]]*DEEPSEEK_API_KEY[[:space:]]*=[[:space:]]*[^[:space:]]' \
        "$ENV_DEST" || faltan+=("DEEPSEEK_API_KEY")
    grep -qE '^[[:space:]]*TELEGRAM_BOT_TOKEN[[:space:]]*=[[:space:]]*[^[:space:]]' \
        "$ENV_DEST" || faltan+=("TELEGRAM_BOT_TOKEN")
    if [ ${#faltan[@]} -gt 0 ]; then
        log "AVISO: claves sin valor en el .env: ${faltan[*]}"
    fi
}

# --------------------------------------------------------------------------
# 2. Navegadores de patchright en el volumen persistente
# --------------------------------------------------------------------------
sembrar_navegadores() {
    local destino="${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"
    [ "$destino" = "/opt/pw-browsers" ] && return 0   # sin volumen: usa la imagen

    if [ -d "$destino" ] && [ -z "$(ls -A "$destino" 2>/dev/null)" ]; then
        if [ -d /opt/pw-browsers ]; then
            log "sembrando navegadores en ${destino} desde la imagen (una sola vez)"
            cp -a /opt/pw-browsers/. "$destino"/
        else
            log "AVISO: no hay navegadores en la imagen para sembrar en ${destino}"
        fi
    fi
}

# --------------------------------------------------------------------------
# 3. Permisos de /data
# --------------------------------------------------------------------------
comprobar_datos() {
    local datos="${DATA_DIR:-/data}"
    mkdir -p "${datos}/logs" 2>/dev/null || true
    if ! touch "${datos}/.write-test" 2>/dev/null; then
        log "ERROR: ${datos} no es escribible por el usuario $(id -un) (uid $(id -u))."
        log "       En el host: sudo chown -R 10001:10001 ./data"
        exit 1
    fi
    rm -f "${datos}/.write-test"
}

preparar_env
sembrar_navegadores
comprobar_datos

# --------------------------------------------------------------------------
# Despacho
# --------------------------------------------------------------------------
case "${1:-scheduler}" in
    scheduler|serve)
        exec /app/docker/scheduler.sh
        ;;
    once)
        log "ejecución única (force-run) del pipeline"
        exec python main.py --modo cron
        ;;
    *)
        exec "$@"
        ;;
esac
