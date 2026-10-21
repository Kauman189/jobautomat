#!/usr/bin/env python3
"""export_linkedin_session.py — Exports the LinkedIn session to storage_state.

Runs **on the Mac** (with a GUI) whenever the server session expires.
It opens a Chromium window, lets you sign in by hand (2FA included) and saves
the ``storage_state`` consumed by the container's headless scraper.

Usage:
    pip install patchright && patchright install chromium
    python scripts/export_linkedin_session.py
    # alternative path:
    python scripts/export_linkedin_session.py --out /tmp/linkedin_session.json

The resulting JSON (``{"cookies": [...], "origins": [...]}``) is copied to the
server as ``data/linkedin_session.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

URL_LOGIN = "https://www.linkedin.com/login"
URL_FEED = "https://www.linkedin.com/feed/"

# Default path: <repo>/data/linkedin_session.json
_DIR_SCRIPT = os.path.dirname(os.path.abspath(__file__))
DESTINO_POR_DEFECTO = os.path.normpath(
    os.path.join(_DIR_SCRIPT, "..", "data", "linkedin_session.json")
)


def _lanzar(p):
    """Launches Chromium with a visible window.

    patchright recommends the real Chrome (``channel="chrome"``); if it is not
    installed on the Mac, fall back to the Chromium downloaded by
    ``patchright install``.
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

        # Soft check: if it is still on /login, the session is useless.
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
