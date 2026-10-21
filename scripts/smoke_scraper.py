#!/usr/bin/env python3
"""scripts/smoke_scraper.py — LIVE test of the LinkedIn scraper (patchright).

It performs exactly the service's real flow, but in miniature:

    1. search_jobs(["Product Designer"], limit=3)   -> 3 offers
    2. get_job_details(<job_id of the first>)       -> full detail

It checks that the contract holds (keys present, correct types) and prints a
readable summary. It does NOT print cookies, tokens or the session JSON.

Requirements
------------
    pip install patchright
    patchright install chromium          # once per environment/machine

Before running it, the session must be in place:

    python scripts/export_session.py     # copies ~/.linkedin-mcp/cookies.json

Usage
-----
    python scripts/smoke_scraper.py
    SESSION_PATH=/path/session.json python scripts/smoke_scraper.py
    LINKEDIN_HEADLESS=0 python scripts/smoke_scraper.py    # show the browser

Exits with code 0 if everything lines up and 1 if any check fails. It takes
between 30 and 90 seconds (Chromium startup + real navigation).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

RAIZ_REPO = Path(__file__).resolve().parents[1]
SERVICIO = RAIZ_REPO / "service"
if str(SERVICIO) not in sys.path:
    sys.path.insert(0, str(SERVICIO))

QUERY = "Product Designer"
LIMITE = 3

CLAVES_BUSQUEDA = ("job_id", "titulo", "empresa", "url")
CLAVES_DETALLE = (
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
)


def _recorte(texto, maximo=400):
    """First lines of a text, for the summary (without dumping the whole offer)."""
    if not texto:
        return "(vacío)"
    plano = " ".join(str(texto).split())
    return plano[:maximo] + ("…" if len(plano) > maximo else "")


def main() -> int:
    inicio = time.time()

    entorno = os.environ.get("SESSION_PATH") or "data/linkedin_session.json (default)"
    print(f"Sesión configurada: {entorno}")
    print(f"Navegador: headless={os.environ.get('LINKEDIN_HEADLESS', '1')}")
    print(f"Buscando {QUERY!r} (limit={LIMITE})...\n")

    try:
        from search import get_job_details, search_jobs
        from search.linkedin import close
    except Exception as exc:  # noqa: BLE001
        print(f"FALLO: no se pudo importar search: {exc}", file=sys.stderr)
        return 1

    fallos: list[str] = []

    # --- 1. Search -------------------------------------------------------- #
    try:
        ofertas = search_jobs([QUERY], limit=LIMITE)
    except Exception as exc:  # noqa: BLE001
        print(f"FALLO en search_jobs: {type(exc).__name__}: {exc}", file=sys.stderr)
        close()
        return 1

    if not ofertas:
        print("FALLO: search_jobs devolvió 0 ofertas.", file=sys.stderr)
        close()
        return 1

    print(f"search_jobs -> {len(ofertas)} ofertas")
    for posicion, oferta in enumerate(ofertas, start=1):
        print(f"  {posicion}. [{oferta.get('job_id')}] {oferta.get('titulo')}")
        print(f"     empresa: {oferta.get('empresa')}")
        print(f"     url:     {oferta.get('url')}")
    if len(ofertas) > LIMITE:
        fallos.append(f"search_jobs devolvió {len(ofertas)} > limit={LIMITE}")

    for clave in CLAVES_BUSQUEDA:
        if clave not in ofertas[0]:
            fallos.append(f"search_jobs: falta la clave {clave!r}")
    if not ofertas[0].get("titulo"):
        fallos.append("search_jobs: la primera oferta no tiene título")

    # --- 2. Detail -------------------------------------------------------- #
    job_id = ofertas[0].get("job_id")
    print(f"\nget_job_details({job_id!r})...")
    try:
        detalle = get_job_details(job_id)
    except Exception as exc:  # noqa: BLE001
        print(f"FALLO en get_job_details: {type(exc).__name__}: {exc}", file=sys.stderr)
        close()
        return 1

    for clave in CLAVES_DETALLE:
        if clave not in detalle:
            fallos.append(f"get_job_details: falta la clave {clave!r}")

    print(f"  titulo:     {detalle.get('titulo')}")
    print(f"  empresa:    {detalle.get('empresa')}")
    print(f"  ubicacion:  {detalle.get('ubicacion')}")
    print(f"  remoto:     {detalle.get('remoto')}")
    print(f"  contrato:   {detalle.get('contrato')}")
    print(f"  salario:    {detalle.get('salario')}")
    print(
        f"  descripcion ({len(detalle.get('descripcion') or '')} chars): "
        f"{_recorte(detalle.get('descripcion'), 300)}"
    )
    requisitos = detalle.get("requisitos")
    print(f"  requisitos ({len(requisitos or '')} chars): {_recorte(requisitos, 200)}")

    if not detalle.get("descripcion"):
        fallos.append("get_job_details: descripción vacía")
    if detalle.get("job_id") != str(job_id):
        fallos.append("get_job_details: job_id no coincide con el pedido")

    # Explicit shutdown of the shared browser.
    close()

    transcurrido = time.time() - inicio
    print(f"\nDuración: {transcurrido:.1f}s")
    if fallos:
        print("RESULTADO: FALLO")
        for fallo in fallos:
            print(f"  - {fallo}")
        return 1

    print("RESULTADO: OK — contrato cumplido y datos reales extraídos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
