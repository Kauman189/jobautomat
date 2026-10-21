#!/usr/bin/env python3
"""scripts/check_offline.py — OFFLINE check of the scraper (no network, no browser).

It does not touch LinkedIn nor open Chromium: it simulates the client to verify
that the contract of `search/linkedin.py` holds and that configuration errors
surface as a readable `LinkedInError` (not a raw traceback).

It checks:
  1. `from search import search_jobs, get_job_details` works and both are
     callables with the expected signature.
  2. No session file -> clear LinkedInError, without launching the browser.
  3. A storage_state with `{"cookies": [...]}` and a bare list of cookies are
     accepted alike.
  4. With a fake client: search_jobs deduplicates, honors `limit` and returns
     the 4 keys; get_job_details normalizes salary/mode/contract/requirements
     and leaves missing data as None.

Usage: python scripts/check_offline.py
Exits 0 if everything passes.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

RAIZ_REPO = Path(__file__).resolve().parents[1]
SERVICIO = RAIZ_REPO / "service"
if str(SERVICIO) not in sys.path:
    sys.path.insert(0, str(SERVICIO))

FALLOS: list[str] = []


def comprobar(condicion, mensaje):
    """Records the result of a single check."""
    if condicion:
        print(f"  OK   {mensaje}")
    else:
        print(f"  FALLO {mensaje}")
        FALLOS.append(mensaje)


class ClienteFalso:
    """Test double for ClienteLinkedIn: same methods, no browser."""

    def __init__(self):
        self.llamadas = []

    def buscar(self, query, maximo):
        self.llamadas.append(("buscar", query, maximo))
        # Two cards repeated on purpose (the second one is a duplicate).
        return [
            {
                "job_id": "1111111111",
                "titulo": "Product Designer",
                "empresa": "Acme",
                "url": "https://www.linkedin.com/jobs/view/1111111111/",
            },
            {
                "job_id": "2222222222",
                "titulo": "Senior Product Designer",
                "empresa": "Globex",
                "url": "https://www.linkedin.com/jobs/view/2222222222/",
            },
            {
                "job_id": "1111111111",
                "titulo": "Product Designer",
                "empresa": "Acme",
                "url": "https://www.linkedin.com/jobs/view/1111111111/",
            },
        ]

    def abrir_oferta(self, job_id):
        self.llamadas.append(("abrir_oferta", job_id))
        if job_id == "2222222222":
            # Bare offer: every optional field missing -> None.
            return {
                "job_id": job_id,
                "titulo": "Senior Product Designer",
                "empresa": "Globex",
                "ubicacion": "Madrid, España",
                "insights": [],
                "descripcion": "Diseño de producto.",
                "url": None,
            }
        return {
            "job_id": job_id,
            "titulo": "Product Designer",
            "empresa": "Acme",
            "ubicacion": "Barcelona, España · hace 2 días · 37 solicitudes",
            "insights": ["Jornada completa", "En remoto", "45.000 € - 55.000 € al año"],
            "descripcion": (
                "Buscamos una persona para el equipo de producto.\n"
                "Requisitos:\n"
                "- 3 años de experiencia con Figma\n"
                "- Inglés C1\n"
                "Sobre la empresa: Acme diseña cosas."
            ),
            "url": None,
        }

    def cerrar(self):
        self.llamadas.append(("cerrar",))


def main() -> int:
    print("1) Importación y contrato público")
    try:
        from search import LinkedInError, get_job_details, search_jobs
        from search import linkedin as modulo
    except Exception as exc:  # noqa: BLE001
        print(f"  FALLO import: {type(exc).__name__}: {exc}")
        return 1
    print("  OK   from search import search_jobs, get_job_details")
    comprobar(callable(search_jobs) and callable(get_job_details), "ambas funciones son callables")
    comprobar(issubclass(LinkedInError, RuntimeError), "LinkedInError hereda de RuntimeError")

    firma_busqueda = inspect.signature(search_jobs)
    comprobar(
        list(firma_busqueda.parameters) == ["queries", "limit"],
        f"firma search_jobs{tuple(firma_busqueda.parameters)}",
    )
    comprobar(firma_busqueda.parameters["limit"].default == 20, "search_jobs(limit=20) por defecto")
    firma_detalle = inspect.signature(get_job_details)
    comprobar(list(firma_detalle.parameters) == ["job_id"], "firma get_job_details(job_id)")
    comprobar(callable(getattr(modulo, "close", None)), "existe close() para cerrar el navegador")

    print("\n2) Sesión inexistente -> LinkedInError claro (sin navegador)")
    ruta_falsa = Path(tempfile.gettempdir()) / "no_existe_sesion_linkedin.json"
    if ruta_falsa.exists():
        ruta_falsa.unlink()
    os.environ["SESSION_PATH"] = str(ruta_falsa)
    modulo._CLIENTE = None
    try:
        search_jobs(["Product Designer"], limit=1)
        comprobar(False, "search_jobs debía lanzar LinkedInError")
    except LinkedInError as exc:
        mensaje = str(exc)
        comprobar("no existe la sesión" in mensaje.lower(), "mensaje explica que falta la sesión")
        comprobar("export_session" in mensaje, "mensaje indica cómo arreglarlo")
    except Exception as exc:  # noqa: BLE001
        comprobar(False, f"excepción inesperada: {type(exc).__name__}: {exc}")

    try:
        get_job_details("1111111111")
        comprobar(False, "get_job_details debía lanzar LinkedInError")
    except LinkedInError:
        comprobar(True, "get_job_details también falla con LinkedInError")
    except Exception as exc:  # noqa: BLE001
        comprobar(False, f"excepción inesperada: {type(exc).__name__}: {exc}")

    print("\n3) Formatos de sesión aceptados")
    carpeta = Path(tempfile.mkdtemp(prefix="linkedin_offline_"))
    cookie = {
        "name": "li_at",
        "value": "x",
        "domain": ".linkedin.com",
        "path": "/",
        "expires": 4102444800,
        "httpOnly": True,
        "secure": True,
        "sameSite": "None",
    }

    estado_completo = carpeta / "estado.json"
    estado_completo.write_text(json.dumps({"cookies": [cookie], "origins": []}), encoding="utf-8")
    cargado = modulo._cargar_sesion(estado_completo)
    comprobar(cargado["cookies"][0]["name"] == "li_at", "storage_state completo aceptado")

    lista_suelta = carpeta / "lista.json"
    lista_suelta.write_text(json.dumps([cookie]), encoding="utf-8")
    cargado = modulo._cargar_sesion(lista_suelta)
    comprobar(
        len(cargado["cookies"]) == 1 and "origins" in cargado,
        "lista suelta de cookies se envuelve en storage_state",
    )

    vacio = carpeta / "vacio.json"
    vacio.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    try:
        modulo._cargar_sesion(vacio)
        comprobar(False, "una sesión sin cookies debía fallar")
    except LinkedInError as exc:
        comprobar("cookies" in str(exc).lower(), "sesión sin cookies -> LinkedInError")

    print("\n4) Contrato con cliente simulado (sin red)")
    falso = ClienteFalso()
    modulo._CLIENTE = falso  # inyecta el doble: _obtener_cliente lo reutiliza
    ofertas = search_jobs(["Product Designer"], limit=2)
    comprobar(len(ofertas) == 2, f"respeta limit (devueltas {len(ofertas)})")
    comprobar(len({o["job_id"] for o in ofertas}) == len(ofertas), "deduplica por job_id")
    comprobar(
        all(set(o) == {"job_id", "titulo", "empresa", "url"} for o in ofertas),
        "cada oferta trae exactamente job_id/titulo/empresa/url",
    )
    comprobar(ofertas[0]["url"].endswith("/jobs/view/1111111111/"), "url bien formada")

    detalle = get_job_details("1111111111")
    comprobar(
        set(detalle)
        == {
            "job_id",
            "titulo",
            "empresa",
            "url",
            "ubicacion",
            "descripcion",
            "requisitos",
            "salario",
            "remoto",
            "contrato",
        },
        "detalle con las 10 claves del contrato",
    )
    comprobar(detalle["ubicacion"] == "Barcelona, España", "ubicación recortada antes de ' · '")
    comprobar(detalle["remoto"] == "Remoto", f"modalidad normalizada ({detalle['remoto']!r})")
    comprobar(
        detalle["contrato"] == "Jornada completa", f"contrato normalizado ({detalle['contrato']!r})"
    )
    comprobar(bool(detalle["salario"]), f"salario extraído ({detalle['salario']!r})")
    comprobar(
        (detalle["requisitos"] or "").startswith("- 3 años"),
        "requisitos recortados tras el encabezado",
    )

    pelada = get_job_details("2222222222")
    comprobar(
        pelada["salario"] is None and pelada["remoto"] is None and pelada["contrato"] is None,
        "campos ausentes quedan en None",
    )
    comprobar(
        pelada["url"].endswith("/jobs/view/2222222222/"), "url de respaldo si la página no la da"
    )

    # The same client was used for both search and detail (one browser).
    tipos = [llamada[0] for llamada in falso.llamadas]
    comprobar(
        tipos.count("buscar") == 1 and tipos.count("abrir_oferta") == 2,
        "un único cliente sirve búsqueda y detalles",
    )

    modulo._CLIENTE = None
    os.environ.pop("SESSION_PATH", None)

    print()
    if FALLOS:
        print(f"RESULTADO: {len(FALLOS)} comprobación(es) fallida(s).")
        return 1
    print("RESULTADO: OK — contrato verificado offline (sin red, sin navegador).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
