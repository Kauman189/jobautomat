#!/usr/bin/env python3
"""scripts/export_session.py — Copia la sesión de LinkedIn a la ruta que usa el servicio.

`search/linkedin.py` espera un `storage_state` de patchright en la ruta indicada
por `SESSION_PATH` (por defecto `data/linkedin_session.json`). Este script:

1. Lee la sesión que exportó `mcp-server-linkedin` (por defecto
   `~/.linkedin-mcp/cookies.json`).
2. Acepta los dos formatos posibles y los normaliza:
     * `{"cookies": [...], "origins": [...]}`  (storage_state completo)
     * `[{...cookie...}, ...]`                 (lista suelta de cookies)
3. Valida que haya cookies (y avisa si falta `li_at`, la de sesión).
4. Escribe el destino con permisos 600 (contiene credenciales de sesión).

No imprime NUNCA valores de cookies: sólo recuentos y nombres de campos.

Uso
---
    python scripts/export_session.py                  # copia con valores por defecto
    python scripts/export_session.py --origen RUTA --destino RUTA
    SESSION_PATH=data/otra.json python scripts/export_session.py

Si el origen no existe, el script imprime cómo generarlo y sale con código 1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Rutas por defecto: la raíz del repo es el padre de scripts/.
RAIZ_REPO = Path(__file__).resolve().parents[1]
ORIGEN_DEFECTO = Path.home() / ".linkedin-mcp" / "cookies.json"
DESTINO_DEFECTO = RAIZ_REPO / "data" / "linkedin_session.json"

INSTRUCCIONES = """\
No se encontró ninguna sesión de LinkedIn que copiar.

Cómo generarla (una sola vez, en una máquina con navegador):

  1. Deja que `mcp-server-linkedin` haga login una vez; deja la sesión
     exportada en ~/.linkedin-mcp/cookies.json (es lo que produce) y vuelve a
     ejecutar este script.
  2. O genera el storage_state a mano con patchright en modo visible:

        from patchright.sync_api import sync_playwright
        with sync_playwright() as p:
            navegador = p.chromium.launch(headless=False)
            contexto = navegador.new_context()
            pagina = contexto.new_page()
            pagina.goto("https://www.linkedin.com/login")
            input("Inicia sesión y pulsa Enter...")
            contexto.storage_state(path="data/linkedin_session.json")
            navegador.close()

  3. O copia un storage_state ya existente:

        python scripts/export_session.py --origen /ruta/al/storage_state.json

Después define SESSION_PATH si usas otra ruta; el default es
data/linkedin_session.json (relativo a service/).
"""


def _resolver_destino(valor: str | None) -> Path:
    """Resuelve el destino: CLI > SESSION_PATH > default, relativo a service/."""
    crudo = valor or os.environ.get("SESSION_PATH") or str(DESTINO_DEFECTO)
    ruta = Path(os.path.expanduser(crudo))
    if not ruta.is_absolute():
        # SESSION_PATH se interpreta respecto a service/, igual que el módulo.
        ruta = (RAIZ_REPO / "service" / ruta).resolve()
    return ruta


def cargar_cookies(origen: Path) -> tuple[list[dict], list[dict]]:
    """Devuelve (cookies, origins) normalizados desde el JSON de origen."""
    if not origen.exists():
        print(f"ERROR: no existe el origen {origen}", file=sys.stderr)
        print(INSTRUCCIONES, file=sys.stderr)
        raise SystemExit(1)

    try:
        datos = json.loads(origen.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: {origen} no es JSON válido: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if isinstance(datos, list):
        cookies, origins = datos, []
    elif isinstance(datos, dict):
        cookies = datos.get("cookies")
        origins = datos.get("origins") or []
    else:
        print(f"ERROR: formato no reconocido en {origen}", file=sys.stderr)
        raise SystemExit(1)

    if not isinstance(cookies, list) or not cookies:
        print(
            f"ERROR: {origen} no contiene cookies; no hay sesión que copiar.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    validas = [c for c in cookies if isinstance(c, dict) and c.get("name")]
    if not validas:
        print(f"ERROR: {origen} tiene cookies sin nombre válido.", file=sys.stderr)
        raise SystemExit(1)

    return validas, origins if isinstance(origins, list) else []


def escribir_estado(destino: Path, cookies: list[dict], origins: list[dict]) -> None:
    """Escribe el storage_state de forma atómica y con permisos 600."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_suffix(destino.suffix + ".tmp")
    estado = {"cookies": cookies, "origins": origins}
    with temporal.open("w", encoding="utf-8") as fichero:
        json.dump(estado, fichero, ensure_ascii=False)
    os.chmod(temporal, 0o600)
    temporal.replace(destino)
    os.chmod(destino, 0o600)


def _resumen(cookies: list[dict]) -> list[str]:
    """Datos no sensibles para el informe final."""
    nombres = {str(c.get("name") or "") for c in cookies}
    dominios = sorted({str(c.get("domain") or "") for c in cookies})
    lineas = [
        f"  cookies: {len(cookies)}",
        f"  dominios: {', '.join(d for d in dominios if d) or '(sin dominio)'}",
        f"  cookie de sesión 'li_at': {'sí' if 'li_at' in nombres else 'NO'}",
    ]

    # Aviso de caducidad: se compara el máximo 'expires' con ahora (epoch).
    caducidades = [
        float(c["expires"])
        for c in cookies
        if isinstance(c.get("expires"), (int, float)) and float(c["expires"]) > 0
    ]
    if caducidades:
        restante_dias = (max(caducidades) - time.time()) / 86400
        lineas.append(f"  caduca en: {restante_dias:.0f} días")
        if restante_dias < 0:
            lineas.append("  AVISO: la sesión ya ha caducado; habrá que renovarla.")
    return lineas


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copia la sesión de LinkedIn (storage_state) a la ruta del servicio."
    )
    parser.add_argument(
        "--origen",
        default=str(ORIGEN_DEFECTO),
        help=f"storage_state de origen (def: {ORIGEN_DEFECTO})",
    )
    parser.add_argument(
        "--destino",
        default=None,
        help="ruta destino (def: $SESSION_PATH o data/linkedin_session.json)",
    )
    args = parser.parse_args()

    origen = Path(os.path.expanduser(args.origen))
    destino = _resolver_destino(args.destino)

    cookies, origins = cargar_cookies(origen)
    escribir_estado(destino, cookies, origins)

    print(f"Sesión copiada: {origen} -> {destino}")
    for linea in _resumen(cookies):
        print(linea)
    print("Permisos aplicados: 600 (sólo el usuario puede leerla).")
    print("Siguiente paso: python scripts/smoke_scraper.py (prueba en vivo).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
