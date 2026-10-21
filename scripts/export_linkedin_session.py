#!/usr/bin/env python3
"""export_linkedin_session.py — Exporta la sesión de LinkedIn a storage_state.

Se ejecuta **en el Mac** (con GUI) cada vez que caduca la sesión del servidor.
Abre Chromium con ventana, deja que inicies sesión a mano (incluido 2FA) y
guarda el ``storage_state`` que consume el buscador headless del contenedor.

Uso:
    pip install patchright && patchright install chromium
    python scripts/export_linkedin_session.py
    # ruta alternativa:
    python scripts/export_linkedin_session.py --out /tmp/linkedin_session.json

El JSON resultante (``{"cookies": [...], "origins": [...]}``) se copia al
servidor como ``data/linkedin_session.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

URL_LOGIN = "https://www.linkedin.com/login"
URL_FEED = "https://www.linkedin.com/feed/"

# Ruta por defecto: <repo>/data/linkedin_session.json
_DIR_SCRIPT = os.path.dirname(os.path.abspath(__file__))
DESTINO_POR_DEFECTO = os.path.normpath(
    os.path.join(_DIR_SCRIPT, "..", "data", "linkedin_session.json")
)


def _lanzar(p):
    """Lanza Chromium con ventana.

    patchright recomienda el Chrome real (``channel="chrome"``); si no está
    instalado en el Mac, cae al Chromium que descarga ``patchright install``.
    """
    try:
        return p.chromium.launch(headless=False, channel="chrome")
    except Exception as err:  # noqa: BLE001 — fallback deliberado.
        print(f"[i] Chrome no disponible ({err}); usando Chromium de patchright.")
        return p.chromium.launch(headless=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=DESTINO_POR_DEFECTO,
        help=f"ruta de salida del JSON (def. {DESTINO_POR_DEFECTO})",
    )
    args = parser.parse_args()

    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: falta patchright.  pip install patchright", file=sys.stderr)
        return 2

    destino = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(destino), exist_ok=True)

    with sync_playwright() as p:
        navegador = _lanzar(p)
        contexto = navegador.new_context(
            locale="es-ES",
            timezone_id="Europe/Madrid",
            viewport={"width": 1440, "height": 900},
        )
        pagina = contexto.new_page()
        pagina.goto(URL_LOGIN, wait_until="domcontentloaded")

        print()
        print("=" * 70)
        print("Inicia sesión en LinkedIn en la ventana de Chromium que se abrió.")
        print("Completa el 2FA si te lo pide y espera a ver tu feed.")
        print("Cuando estés dentro, vuelve aquí y pulsa Enter.")
        print("=" * 70)
        try:
            input("Pulsa Enter para guardar la sesión... ")
        except (KeyboardInterrupt, EOFError):
            print("\nCancelado.")
            navegador.close()
            return 1

        # Comprobación suave: si sigue en /login, la sesión no sirve.
        url_actual = pagina.url
        if "/login" in url_actual or "/authwall" in url_actual:
            print(f"[!] La pestaña sigue en {url_actual}: parece que no hay sesión.")
            print("    Se guarda igualmente, pero revísalo antes de copiarlo.")

        estado = contexto.storage_state()
        cookies = estado.get("cookies", [])
        if not any(c.get("name") == "li_at" for c in cookies):
            print("[!] No se encontró la cookie 'li_at': la sesión no está autenticada.")
        if any(c.get("name") == "JSESSIONID" for c in cookies) is False:
            print("[i] Sin 'JSESSIONID'; LinkedIn puede regenerarla al primer uso.")

        with open(destino, "w", encoding="utf-8") as fh:
            json.dump(estado, fh, ensure_ascii=False, indent=2)
        os.chmod(destino, 0o600)

        print(f"[ok] {len(cookies)} cookies y {len(estado.get('origins', []))} origins")
        print(f"[ok] guardado en {destino}")
        print("     Cópialo al servidor como data/linkedin_session.json (chown 10001).")
        _ = URL_FEED  # documentado arriba; el usuario navega a mano

        contexto.close()
        navegador.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
