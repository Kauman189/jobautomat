"""search/jobspy.py — Third posting source: Indeed, ZipRecruiter and Glassdoor.

Uses the ``python-jobspy`` library (``scrape_jobs``), which talks directly to
the portals' public APIs/HTML: **no browser and no session**, so it does not
compete with the LinkedIn rate limit or session.

Output contract (identical to ``search.linkedin`` and ``search.boards``)::

    {"job_id", "titulo", "empresa", "url", "ubicacion",
     "remoto", "salario", "contrato", "descripcion", "source"}

Anything unknown stays ``None``: no data is invented.

Mapping
-------
* ``job_id``   = ``<site prefix>-<stable hash of the url>`` (8 hex). The prefix
  (``indeed-``, ``ziprecruiter-``, ``glassdoor-``) avoids colliding with
  LinkedIn's numeric ids in the SQLite table.
* ``titulo``   = ``title``
* ``empresa``  = ``company``
* ``url``      = ``job_url``
* ``ubicacion``= ``location``
* ``remoto``   = ``Remoto`` when ``is_remote`` is true; otherwise ``None``
  (JobSpy does not tell hybrid from on-site, so it is not guessed).
* ``salario``  = range text (``min_amount – max_amount`` + currency +
  interval); if only the minimum exists, only the minimum.
* ``contrato`` = ``job_type`` translated to ``scoring.py`` vocabulary
  (``Jornada completa`` / ``Media jornada`` / ``Prácticas`` / ``Freelance`` /
  ``Temporal`` / ``Indefinido``) or ``None``.
* ``descripcion`` = ``description`` as plain text, trimmed.
* ``fecha_publicacion`` = ``date_posted`` normalized to ``YYYY-MM-DD``.
* ``source``   = origin site (``indeed``, ``zip_recruiter``, ``glassdoor``).

Design
------
* Searches with ``is_remote=True`` to prioritize remote, which is the profile
  criterion; JobSpy also returns non-remote postings and those are marked
  ``remoto=None`` (``scoring`` discards them with its pre-filter).
* **Isolation per query and per site**: every ``scrape_jobs`` call has its own
  ``try/except``. A site that is down (ZipRecruiter usually answers 403 without
  a proxy), slow or rate-limited does not take down the rest.
* The ``jobspy`` import is lazy and tolerant: if the dependency is not
  installed, the function returns ``[]`` and the run continues with the other
  sources.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import math
import os
import re
import sys
import time
from collections.abc import Iterable
from typing import Any

log = logging.getLogger("jobautomat.search.jobspy")

# This module's directory (to detect `jobspy` shadowing).
_DIR_MODULO = os.path.dirname(os.path.abspath(__file__))

# JobSpy sites we care about (LinkedIn is excluded on purpose: we have our own
# session-based scraper).
SITES = ("indeed", "zip_recruiter", "glassdoor")

# ``job_id`` prefix per site (stable vocabulary for the DB).
_PREFIJOS = {
    "indeed": "indeed",
    "zip_recruiter": "ziprecruiter",
    "ziprecruiter": "ziprecruiter",
    "glassdoor": "glassdoor",
}

# Country for Indeed (required by JobSpy) and default location.
PAIS_INDEED = "Spain"
UBICACION_POR_DEFECTO = "Spain"

# JobSpy contract type -> scoring.py vocabulary.
_CONTRATOS = (
    (
        (
            "fulltime",
            "full time",
            "full-time",
            "permanent",
            "indefinido",
            "jornada completa",
            "tiempo completo",
            "permanente",
        ),
        "Jornada completa",
    ),
    (("parttime", "part time", "part-time", "media jornada", "tiempo parcial"), "Media jornada"),
    (("internship", "intern", "becario", "beca", "practicas", "practica"), "Prácticas"),
    (
        (
            "freelance",
            "contractor",
            "contrato mercantil",
            "autonomo",
            "contract",
            "contrato de obra",
        ),
        "Freelance",
    ),
    (("temporary", "temporal", "fixed term", "fixed-term", "temporalidad"), "Temporal"),
)

# JobSpy pay interval -> readable suffix.
_INTERVALOS = {
    "yearly": "año",
    "annual": "año",
    "year": "año",
    "annually": "año",
    "monthly": "mes",
    "month": "mes",
    "weekly": "semana",
    "week": "semana",
    "daily": "día",
    "day": "día",
    "hourly": "hora",
    "hour": "hora",
}

# Maximum stored description length (cost control in scoring).
_MAX_DESCRIPCION = 12000

# Pause between Glassdoor requests (caps at ~2 req/s).
_PAUSA_GLASSDOOR = 0.7


# --------------------------------------------------------------------------- #
# Normalization utilities
# --------------------------------------------------------------------------- #


def _sin_acentos(texto: str) -> str:
    """Lowercase and strip accents, to compare text loosely."""
    reemplazos = {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ü": "u",
        "ñ": "n",
        "à": "a",
        "è": "e",
        "ì": "i",
        "ò": "o",
        "ù": "u",
    }
    return "".join(reemplazos.get(c, c) for c in (texto or "").lower())


def _nulo(valor: Any) -> bool:
    """True for ``None``, empty string or ``NaN`` (pandas/numpy)."""
    if valor is None:
        return True
    if isinstance(valor, str):
        return not valor.strip()
    try:
        return bool(valor != valor)  # NaN is never equal to itself.
    except Exception:  # noqa: BLE001 — odd types: treated as valid.
        return False


def _limpio(valor: Any) -> str | None:
    """Return the trimmed text, or ``None`` if nothing useful remains."""
    if _nulo(valor):
        return None
    texto = " ".join(str(valor).split())
    return texto or None


def _texto_descripcion(valor: Any) -> str | None:
    """Plain-text description, trimmed to ``_MAX_DESCRIPCION``."""
    if _nulo(valor):
        return None
    texto = str(valor).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not texto:
        return None
    return texto[:_MAX_DESCRIPCION]


def _es_cierto(valor: Any) -> bool:
    """Tolerantly read a boolean that may arrive as NaN."""
    if _nulo(valor):
        return False
    if isinstance(valor, str):
        return valor.strip().lower() in ("true", "1", "yes", "si", "sí")
    return bool(valor)


def _prefijo_site(site: Any) -> str:
    """Stable ``job_id`` prefix for a JobSpy site."""
    clave = _sin_acentos(str(site or "")).replace("-", "_").strip()
    return _PREFIJOS.get(clave, clave.replace("_", "") or "jobspy")


def _id_estable(url: str, site: Any) -> str:
    """``<prefix>-<sha1(url)[:8]>``: stable across runs."""
    digest = hashlib.sha1(url.strip().lower().encode("utf-8")).hexdigest()[:8]
    return f"{_prefijo_site(site)}-{digest}"


def _mapear_contrato(job_type: Any) -> str | None:
    """Translate ``job_type`` to ``scoring.py`` vocabulary (or ``None``)."""
    texto = _sin_acentos(_limpio(job_type) or "")
    if not texto:
        return None
    # JobSpy uses "fulltime"/"parttime"; compare without spaces or hyphens.
    compacto = texto.replace(" ", "").replace("-", "").replace("_", "")
    for variantes, etiqueta in _CONTRATOS:
        for variante in variantes:
            if variante.replace(" ", "").replace("-", "") in compacto:
                return etiqueta
    return None


def _formatear_numero(valor: Any) -> str | None:
    """Readable amount in Spanish format (``50.000``, ``25,5``)."""
    if _nulo(valor):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numero) or numero <= 0:
        return None
    if numero >= 1000 and abs(numero - round(numero)) < 0.01:
        return f"{numero:,.0f}".replace(",", ".")
    if abs(numero - round(numero)) < 0.01:
        return str(int(round(numero)))
    return (f"{numero:.2f}").replace(".", ",")


def _formatear_salario(minimo: Any, maximo: Any, moneda: Any, intervalo: Any) -> str | None:
    """Compose the salary text respecting range, currency and interval.

    ``50.000 – 70.000 EUR/año`` · ``50.000 EUR/año`` (minimum only) ·
    ``30 EUR/hora``. Returns ``None`` if no figure was published.
    """
    txt_min = _formatear_numero(minimo)
    txt_max = _formatear_numero(maximo)
    if not txt_min and not txt_max:
        return None
    if txt_min and txt_max:
        if txt_min == txt_max:
            rango = txt_min
        else:
            rango = f"{txt_min} – {txt_max}"
    else:
        rango = txt_min or txt_max

    codigo = _limpio(moneda)
    sufijo = _INTERVALOS.get(_sin_acentos(_limpio(intervalo) or ""))

    partes: list[str] = [str(rango)]
    if codigo:
        partes.append(codigo.upper() if len(codigo) == 3 else codigo)
    texto = " ".join(partes)
    if sufijo:
        texto += "/" + sufijo
    return texto


# --------------------------------------------------------------------------- #
# Tolerant import of the dependency
# --------------------------------------------------------------------------- #


def _cargar_scrape_jobs():
    """Return ``scrape_jobs`` or ``None`` when jobspy is not installed.

    The import is absolute (``import jobspy``) so this module is not confused
    with the package. If ``search/`` is on ``sys.path`` (e.g. cwd inside that
    folder), this file would shadow the package: retry with that path removed
    temporarily.
    """
    try:
        from jobspy import scrape_jobs  # lazy import: optional dependency

        return scrape_jobs
    except Exception as err:  # noqa: BLE001 — ImportError or version error.
        primero = err

    original = list(sys.path)
    sombra = [ruta for ruta in sys.path if os.path.abspath(ruta or os.getcwd()) == _DIR_MODULO]
    if sombra:
        sys.path[:] = [ruta for ruta in sys.path if ruta not in sombra]
        try:
            from jobspy import scrape_jobs

            return scrape_jobs
        except Exception as err:  # noqa: BLE001
            primero = err
        finally:
            sys.path[:] = original

    log.error(
        "jobspy no disponible (%s: %s); la fuente JobSpy queda desactivada.",
        type(primero).__name__,
        primero,
    )
    return None


# --------------------------------------------------------------------------- #
# Row normalization
# --------------------------------------------------------------------------- #


def _fecha_publicacion(valor: Any) -> str | None:
    """Normalize JobSpy's ``date_posted`` to ``YYYY-MM-DD``.

    JobSpy returns a pandas ``datetime``/``Timestamp`` usually, but it may also
    arrive as epoch or string. Without a valid date ``None`` is returned
    (nothing is invented).
    """
    if _nulo(valor):
        return None
    if isinstance(valor, datetime.datetime):
        return valor.date().isoformat()
    if isinstance(valor, datetime.date):
        return valor.isoformat()
    if isinstance(valor, (int, float)):
        return _fecha_desde_epoch(valor)

    # numpy.datetime64 or other types with ``to_pydatetime``/``item``.
    for metodo in ("to_pydatetime", "item"):
        funcion = getattr(valor, metodo, None)
        if callable(funcion) and not isinstance(valor, str):
            try:
                convertido = funcion()
            except Exception:  # noqa: BLE001 — exotic type: fall back to text
                continue
            if isinstance(convertido, datetime.datetime):
                return convertido.date().isoformat()
            if isinstance(convertido, datetime.date):
                return convertido.isoformat()

    texto = str(valor).strip()
    if not texto:
        return None
    if re.fullmatch(r"-?\d+(?:\.\d+)?", texto):
        return _fecha_desde_epoch(float(texto))

    candidato = texto[:-1] + "+00:00" if texto.endswith(("Z", "z")) else texto
    try:
        return datetime.datetime.fromisoformat(candidato).date().isoformat()
    except ValueError:
        pass
    try:  # "2026-10-01 08:30:00" or "2026-10-01"
        return datetime.datetime.strptime(texto[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _fecha_desde_epoch(numero: Any) -> str | None:
    """Convert an epoch (seconds or milliseconds) to ``YYYY-MM-DD``."""
    try:
        valor = float(numero)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(valor):
        return None
    if abs(valor) >= 1e11:  # milliseconds
        valor /= 1000.0
    try:
        momento = datetime.datetime.fromtimestamp(valor, tz=datetime.UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return momento.date().isoformat()


def _normalizar_fila(fila: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a JobSpy row to the normalized contract.

    Returns ``None`` when something essential is missing (title or URL).
    """
    titulo = _limpio(fila.get("title"))
    url = _limpio(fila.get("job_url"))
    if not titulo or not url:
        return None

    site = _limpio(fila.get("site")) or "jobspy"

    return {
        "job_id": _id_estable(url, site),
        "titulo": titulo,
        "empresa": _limpio(fila.get("company")),
        "url": url,
        "ubicacion": _limpio(fila.get("location")),
        "remoto": "Remoto" if _es_cierto(fila.get("is_remote")) else None,
        "salario": _formatear_salario(
            fila.get("min_amount"),
            fila.get("max_amount"),
            fila.get("currency"),
            fila.get("interval"),
        ),
        "contrato": _mapear_contrato(fila.get("job_type")),
        "descripcion": _texto_descripcion(fila.get("description")),
        "fecha_publicacion": _fecha_publicacion(fila.get("date_posted")),
        "source": site,
    }


def _clave_deduplicacion(oferta: dict[str, Any]) -> tuple:
    """Loose key to recognize the same posting inside JobSpy.

    Compared without accents or punctuation: the same role may arrive from
    Indeed and Glassdoor with the company spelled differently.
    """
    url = (oferta.get("url") or "").split("?")[0].rstrip("/").lower()
    titulo = " ".join(_sin_acentos(str(oferta.get("titulo") or "")).split())
    empresa = " ".join(_sin_acentos(str(oferta.get("empresa") or "")).split())
    return url, titulo, empresa


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def buscar_ofertas_jobspy(
    queries: Iterable[str],
    limite: int = 20,
    horas_recientes: int = 72,
    ubicacion: str = UBICACION_POR_DEFECTO,
    sites: Iterable[str] = SITES,
    traer_descripcion: bool = True,
) -> list[dict[str, Any]]:
    """Search postings on Indeed, ZipRecruiter and Glassdoor through JobSpy.

    :param queries: search terms (one call each). ``main.py`` passes ``ROLES``
        + ``TOPICS`` from ``settings.json``; the fixed list is only a fallback
        when there is no config.
    :param limite: wanted results **per query and site**.
    :param horas_recientes: maximum age in hours (``hours_old``).
    :param ubicacion: location for Indeed/Glassdoor (``Spain`` by default).
    :param sites: JobSpy sites to query.
    :param traer_descripcion: request the full description when the portal does
        not include it in the listing (Glassdoor). Costs extra requests.

    Returns a deduplicated list of dicts with the normalized contract. Failures
    per query or per site are logged and ignored: an exception is never
    propagated to the caller.
    """
    scrape_jobs = _cargar_scrape_jobs()
    if scrape_jobs is None:
        return []

    lista_queries = [str(q).strip() for q in (queries or []) if str(q).strip()]
    if not lista_queries:
        return []

    lista_sites = [s for s in sites if s]
    limite = max(1, int(limite or 1))

    ofertas: list[dict[str, Any]] = []
    vistas = set()
    # Per-site counters for the final log (a failure is visible at a glance).
    por_site: dict[str, int] = {s: 0 for s in lista_sites}
    fallos: dict[str, int] = {s: 0 for s in lista_sites}

    for query in lista_queries:
        for site in lista_sites:
            nombre = str(site)
            try:
                marco = scrape_jobs(
                    site_name=[nombre],
                    search_term=query,
                    location=ubicacion,
                    results_wanted=limite,
                    hours_old=horas_recientes,
                    is_remote=True,  # prioritize remote, profile criterion
                    country_indeed=PAIS_INDEED,  # required by Indeed
                    fetch_description=traer_descripcion,
                    verbose=0,
                )
            except Exception as err:  # noqa: BLE001 — isolate site/query.
                fallos[nombre] = fallos.get(nombre, 0) + 1
                log.warning(
                    "JobSpy: '%s' falló en %s (%s): %s",
                    query,
                    nombre,
                    type(err).__name__,
                    err,
                )
                continue
            finally:
                if nombre == "glassdoor":
                    time.sleep(_PAUSA_GLASSDOOR)

            if marco is None or getattr(marco, "empty", True):
                log.info("JobSpy: 0 resultados para '%s' en %s.", query, nombre)
                continue

            try:
                filas = marco.to_dict(orient="records")
            except Exception as err:  # noqa: BLE001 — unexpected format.
                fallos[nombre] = fallos.get(nombre, 0) + 1
                log.warning(
                    "JobSpy: DataFrame ilegible de %s (%s): %s",
                    nombre,
                    type(err).__name__,
                    err,
                )
                continue

            nuevas = 0
            for fila in filas:
                if not isinstance(fila, dict):
                    continue
                try:
                    oferta = _normalizar_fila(fila)
                except Exception as err:  # noqa: BLE001 — corrupt row.
                    log.debug("JobSpy: fila descartada (%s): %s", type(err).__name__, err)
                    continue
                if not oferta:
                    continue
                clave = _clave_deduplicacion(oferta)
                if clave in vistas:
                    continue
                vistas.add(clave)
                ofertas.append(oferta)
                nuevas += 1

            por_site[nombre] = por_site.get(nombre, 0) + nuevas
            log.info(
                "JobSpy: '%s' en %s -> %s filas, %s nuevas.",
                query,
                nombre,
                len(filas),
                nuevas,
            )

    resumen = ", ".join(
        "{}: {}{}".format(
            site,
            por_site.get(site, 0),
            f" ({fallos[site]} fallos)" if fallos.get(site) else "",
        )
        for site in por_site
    )
    log.info("JobSpy terminado: %s ofertas únicas (%s).", len(ofertas), resumen)
    return ofertas
