#!/usr/bin/env python3
"""export_linkedin_session.py — Exports the LinkedIn session to storage_state.

Runs **on the Mac** (with a GUI) whenever the server session expires.
It opens a Chromium window, lets you sign in by hand (2FA included) and saves
the ``storage_state`` consumed by the container's headless scraper.

Usage:
    pip install patchright && patchright install chromium
    python scripts/export_linkedin_session.py
    # non-interactive, started by the panel's "Connect LinkedIn" button:
    python scripts/export_linkedin_session.py --auto
    # alternative path:
    python scripts/export_linkedin_session.py --out /tmp/linkedin_session.json

With ``--auto`` nothing is read from stdin: the script polls the browser
cookies every ``INTERVALO_SONDEO`` seconds until LinkedIn sets ``li_at`` (up to
``ESPERA_MAXIMA``), waits ``ESPERA_ASENTARSE`` seconds for the rest of the
session to settle and writes the same file as the interactive mode.

Exit codes: 0 = session saved, 1 = timeout / cancelled, 2 = patchright missing.

The resulting JSON (``{"cookies": [...], "origins": [...]}``) is copied to the
server as ``data/linkedin_session.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

URL_LOGIN = "https://www.linkedin.com/login"
URL_FEED = "https://www.linkedin.com/feed/"

# --auto pacing. Polling cheaply is enough: the cookie appears as soon as the
# sign-in completes, no navigation is needed.
INTERVALO_SONDEO = 2.0  # seconds between cookie checks
ESPERA_MAXIMA = 300.0  # give up after ~5 minutes without a session
ESPERA_ASENTARSE = 3.0  # let LinkedIn finish setting the rest of the cookies

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


def _tiene_li_at(contexto) -> bool:
    """True when the ``li_at`` cookie (LinkedIn auth) is already in the context.

    A closed page/browser raises; that is not an authentication signal, so it
    keeps polling until the timeout expires.
    """
    try:
        return any(c.get("name") == "li_at" for c in contexto.cookies())
    except Exception:  # noqa: BLE001 — browser closed by the user.
        return False


def _guardar(contexto, pagina, destino) -> int:
    """Write ``storage_state`` to ``destino`` (mode 600) with sanity warnings."""
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
    return 0


def _esperar_enter(contexto, pagina, destino) -> int:
    """Interactive mode: wait for the user to confirm on the terminal."""
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
        return 1
    return _guardar(contexto, pagina, destino)


def _esperar_sesion(contexto, pagina, destino, espera_maxima: float = ESPERA_MAXIMA) -> int:
    """Non-interactive mode: poll for ``li_at`` and save as soon as it appears."""
    print()
    print("=" * 70)
    print("Inicia sesión en LinkedIn en la ventana de Chromium que se abrió.")
    print("Completa el 2FA si te lo pide. La sesión se guarda sola en cuanto entres.")
    print(f"Esperando la cookie de sesión (máximo {int(espera_maxima)} s)...")
    print("=" * 70, flush=True)

    limite = time.monotonic() + espera_maxima
    while time.monotonic() < limite:
        if _tiene_li_at(contexto):
            print()
            print(f"[i] Sesión detectada; esperando {int(ESPERA_ASENTARSE)} s a que se asiente.")
            time.sleep(ESPERA_ASENTARSE)
            codigo = _guardar(contexto, pagina, destino)
            if codigo == 0:
                print("[ok] Sesión de LinkedIn guardada correctamente.")
            return codigo
        # Heartbeat: a detached run must not look hung for five minutes.
        print(".", end="", flush=True)
        time.sleep(INTERVALO_SONDEO)

    print()
    print(
        "ERROR: no se inició sesión en LinkedIn dentro del tiempo esperado; "
        "no se guardó nada.",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=DESTINO_POR_DEFECTO,
        help=f"ruta de salida del JSON (def. {DESTINO_POR_DEFECTO})",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help=(
            "sin interacción: espera a que aparezca la cookie 'li_at' y guarda "
            "(lo lanza el botón 'Conectar LinkedIn' del panel)"
        ),
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

        try:
            if args.auto:
                codigo = _esperar_sesion(contexto, pagina, destino)
            else:
                codigo = _esperar_enter(contexto, pagina, destino)
        except KeyboardInterrupt:
            print("\nCancelado.")
            codigo = 1
        finally:
            try:
                contexto.close()
                navegador.close()
            except Exception:  # noqa: BLE001 — the window may already be gone.
                pass

    return codigo


if __name__ == "__main__":
    sys.exit(main())
