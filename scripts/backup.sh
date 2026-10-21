#!/usr/bin/env bash
#
# backup.sh — copia de seguridad del estado persistente de jobautomat.
#
# Empaqueta en un único .tar.gz con marca de tiempo lo que hay en ./data:
#
#   state.db               oportunidades, scores y materiales generados
#   perfil.json            perfil del candidato
#   settings.json          overrides del panel (puede no existir)
#   linkedin_session.json  cookies de LinkedIn (¡datos sensibles!)
#   candidaturas/          packs CV/carta ya generados
#   logs/                  log del pipeline
#
# Uso:
#   scripts/backup.sh                       # escribe ./backups/jobautomat-<fecha>.tar.gz
#   DATA_DIR=/srv/jobautomat/data scripts/backup.sh
#   BACKUP_DIR=/mnt/nas/jobautomat scripts/backup.sh
#
# Notas:
#   - Si `sqlite3` está disponible, `state.db` se copia con `.backup`, que da
#     una instantánea consistente aunque el planificador esté escribiendo.
#     Sin él se copia el fichero (y su -wal/-shm) tal cual: para una copia
#     garantizada, para el servicio antes (`docker compose stop app bot web`).
#   - Los avisos de elementos ausentes no abortan: un `data/` recién creado no
#     tiene `settings.json` ni `logs/` y aun así merece copia.
#   - `backups/` no está en .gitignore; si versionas el estado, exclúyelo.
#
# Salida: 0 si el archivo se creó, 1 si no había nada que copiar o falló algo.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

DATA_DIR="${DATA_DIR:-$ROOT_DIR/data}"
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/backups}"

ELEMENTOS=(state.db perfil.json settings.json linkedin_session.json candidaturas logs)

log()  { printf '[backup] %s\n' "$*"; }
aviso() { printf '[backup] aviso: %s\n' "$*" >&2; }
die()  { printf '[backup] ERROR: %s\n' "$*" >&2; exit 1; }

command -v tar >/dev/null 2>&1 || die "no encuentro 'tar' en el PATH"

if [[ ! -d "$DATA_DIR" ]]; then
  die "no existe el directorio de datos: $DATA_DIR (¿estás dentro del repo?)"
fi

if [[ -z "$(ls -A "$DATA_DIR" 2>/dev/null)" ]]; then
  die "el directorio de datos está vacío: $DATA_DIR (nada que copiar)"
fi

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/jobautomat-backup.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

incluidos=()
for item in "${ELEMENTOS[@]}"; do
  origen="$DATA_DIR/$item"
  if [[ ! -e "$origen" ]]; then
    aviso "no existe $item en $DATA_DIR (se omite)"
    continue
  fi

  if [[ "$item" == "state.db" ]] && command -v sqlite3 >/dev/null 2>&1; then
    # Instantánea consistente; si falla (BD corrupta, bloqueo), copia directa.
    if sqlite3 "$origen" ".backup '$STAGE/state.db'" >/dev/null 2>&1; then
      incluidos+=("$item")
      continue
    fi
    aviso "sqlite3 .backup falló en state.db; copio el fichero tal cual"
  fi

  cp -Rp "$origen" "$STAGE/"
  if [[ "$item" == "state.db" ]]; then
    for extra in "$origen-wal" "$origen-shm"; do
      [[ -e "$extra" ]] && cp -p "$extra" "$STAGE/"
    done
  fi
  incluidos+=("$item")
done

if [[ ${#incluidos[@]} -eq 0 ]]; then
  die "no hay nada que copiar en $DATA_DIR (esperaba al menos state.db o perfil.json)"
fi

mkdir -p "$BACKUP_DIR" || die "no puedo crear el directorio de copias: $BACKUP_DIR"
[[ -w "$BACKUP_DIR" ]] || die "el directorio de copias no es escribible: $BACKUP_DIR"

marca="$(date +%Y%m%d-%H%M%S)"
destino="$BACKUP_DIR/jobautomat-$marca.tar.gz"
n=1
while [[ -e "$destino" ]]; do
  destino="$BACKUP_DIR/jobautomat-$marca-$n.tar.gz"
  n=$((n + 1))
done

log "origen:  $DATA_DIR"
log "incluye: ${incluidos[*]}"

if ! tar -czf "$destino" -C "$STAGE" .; then
  rm -f "$destino"
  die "tar falló al crear $destino"
fi

[[ -s "$destino" ]] || die "el archivo creado está vacío: $destino"

tamano="$(du -h "$destino" 2>/dev/null | awk '{print $1}' | head -n 1)"
log "copias guardadas en: $BACKUP_DIR"
log "OK — $destino (${tamano:-?})"
