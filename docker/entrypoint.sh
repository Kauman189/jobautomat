#!/usr/bin/env bash
# entrypoint.sh — prepares the environment and dispatches the command.
#
# Modes:
#   scheduler        (default) starts the nightly scheduler.
#   once             runs the pipeline once in cron mode (force-run).
#   <command...>     runs that command as-is inside /app/service,
#                    e.g.  python main.py --modo manual
#
# Responsibilities:
#   1. Materialize /app/service/.env from the read-only mounted .env,
#      forcing DB_PATH / PERFIL_PATH to the /data volume so the state
#      survives the container.
#   2. Seed the patchright browser volume the first time.
#   3. Check write permissions on /data.
set -euo pipefail

SERVICE_DIR=/app/service
ENV_DEST="${SERVICE_DIR}/.env"

log() { printf '%s entrypoint: %s\n' "$(date -Is)" "$*" >&2; }

# --------------------------------------------------------------------------
# 1. Container .env
# --------------------------------------------------------------------------
preparar_env() {
    local origen="${JOB_ENV_FILE:-/run/jobautomat/.env}"

    if [ ! -f "$origen" ]; then
        log "AVISO: no existe ${origen}; se usan los valores por defecto y las"
        log "       variables de entorno. Monta tu .env ahí o revisa el compose."
        return 0
    fi

    # Copies the .env dropping the paths managed by the container: DB_PATH,
    # PERFIL_PATH and any *_SESSION_PATH point to the /data volume.
    # (`|| true` because grep returns 1 when no line is left.)
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

    # Early warning if secrets are missing (config.py will fail anyway, but
    # this message shows up earlier in the scheduler logs).
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
# 2. patchright browsers on the persistent volume
# --------------------------------------------------------------------------
sembrar_navegadores() {
    local destino="${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"
    [ "$destino" = "/opt/pw-browsers" ] && return 0   # no volume: use the image

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
# 3. /data permissions
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
# Dispatch
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
