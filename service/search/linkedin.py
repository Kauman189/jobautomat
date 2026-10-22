"""search/linkedin.py — LinkedIn job search with patchright, directly.

Talks to LinkedIn **without** any intermediate MCP server: opens a Chromium
driven by `patchright` (stealth fork of Playwright), loads the session saved on
disk (``storage_state``) and does what a person would do: search, scroll the
results list and open each posting.

Public API (stable contract for ``main.py`` and ``search/__init__.py``):

  * ``search_jobs(queries, limit=20)`` -> list of dicts with ``job_id``,
    ``titulo``, ``empresa`` and ``url``, deduplicated by ``job_id``.
  * ``get_job_details(job_id)`` -> dict with ``job_id``, ``titulo``,
    ``empresa``, ``url``, ``ubicacion``, ``descripcion``, ``requisitos``,
    ``salario``, ``remoto`` and ``contrato``. Missing fields are ``None``.
  * ``LinkedInError`` -> single module error (missing session, expired login,
    anti-bot block, timeout...).

Design
------
1. **One browser per run.** The client is created lazily on first use and
   reused for searches and for *all* postings. Opening/closing Chromium per
   posting would be slow and far more suspicious. ``close()`` shuts it down at
   the end of the run (``atexit`` guarantees it even if the caller forgets).
2. **Headless by default** (Docker/Linux). A visible window is enabled only
   with ``LINKEDIN_HEADLESS=0`` for local debugging.
3. **Human pacing.** Random pauses between actions, progressive scrolling
   instead of a jump to the end, and a long wait between postings, so LinkedIn
   never sees impossible bursts.
4. **Configurable session.** ``SESSION_PATH`` (default
   ``../data/linkedin_session.json``, relative to ``service/``). Both the full
   ``storage_state`` (``{"cookies": [...], "origins": [...]}``) and a bare
   cookie list are accepted (the format ``mcp-server-linkedin`` exports to
   ``~/.linkedin-mcp/cookies.json``).
5. **No secrets in logs.** Cookie values, headers and the session JSON are
   never written to logs: at most, how many cookies were loaded.
6. **Budget before cooldown.** Every page navigation increments a persistent
   daily counter (``data/linkedin_usage.json``); once it reaches
   ``MAX_PETICIONES_LINKEDIN_DIA`` the source is skipped for the rest of the
   day, so the block is never reached instead of being handled afterwards.

A posting description is third-party text: here it is only extracted and
trimmed, never interpreted or executed.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import random
import re
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote_plus

from i18n import t

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Module path -> service root (service/), so relative paths resolve the same
# regardless of the directory the process was started from.
RAIZ_SERVICIO = Path(__file__).resolve().parents[1]
RUTA_SESION_DEFECTO = "../data/linkedin_session.json"

# Age at which the exported session is considered stale (LinkedIn expires
# `li_at` over time). Shared by `sesion_estado()` and the self-host
# diagnostics, so the 14 is defined once.
DIAS_CADUCIDAD_SESION = 14

# Public LinkedIn.
URL_BASE = "https://www.linkedin.com"
URL_BUSQUEDA = (
    URL_BASE
    + "/jobs/search/?keywords={query}&origin=JOB_SEARCH_PAGE_KEYWORD_AUTOCOMPLETE"
    + "&refresh=true&start={inicio}"
)
URL_OFERTA = URL_BASE + "/jobs/view/{job_id}/"

# Pagination: LinkedIn serves 25 cards per page in the logged-in view.
OFERTAS_POR_PAGINA = 25
MAX_PAGINAS = 10

# Timeouts (ms for patchright, s elsewhere).
TIMEOUT_NAVEGACION = 60_000
TIMEOUT_SELECTOR = 20_000
TIMEOUT_DESCARGA_DETALLE = 25_000

# Maximum scrolls per page to trigger lazy loading of cards.
MAX_SCROLLS = 12

# Pauses (seconds). Wide random ranges: less fingerprint than a fixed pattern.
# Kept deliberately slow: LinkedIn rate-limits by page views per hour (a safe
# ceiling is roughly 80-100 per hour), so consecutive navigations must be
# ~15-30 s apart (PAUSA_TRAS_NAVEGAR + PAUSA_ENTRE_OFERTAS / PAUSA_ENTRE_PAGINAS).
PAUSA_ENTRE_QUERIES = (20.0, 40.0)
PAUSA_ENTRE_PAGINAS = (10.0, 18.0)
PAUSA_ENTRE_OFERTAS = (9.0, 18.0)
PAUSA_SCROLL = (1.5, 3.5)
PAUSA_TRAS_NAVEGAR = (6.0, 12.0)

# --------------------------------------------------------------------------- #
# Selectors and LinkedIn DOM
# --------------------------------------------------------------------------- #
# All selectors live in one place so a LinkedIn markup change is fixed here and
# not scattered across the code. Lists are tried in order: the first one that
# exists wins (LinkedIn alternates between two layouts, the logged-in view and
# the public one).

# Result card: `data-occludable-job-id` is the stable attribute LinkedIn puts
# on every <li> of the list and holds the numeric posting id.
SELECTOR_TARJETA = (
    "li[data-occludable-job-id], li.scaffold-layout__list-item, ul.jobs-search__results-list > li"
)
# Results column's own scroll container (lazy loading).
SELECTOR_LISTA_SCROLL = (
    ".scaffold-layout__list > div, .jobs-search-results-list, .jobs-search__results-list"
)

# Inside the card.
SELECTORES_TITULO_TARJETA = (
    "a.job-card-container__link",
    "a.job-card-list__title",
    "a[class*='job-card'][href*='/jobs/view/']",
    "a[href*='/jobs/view/']",
)
SELECTORES_EMPRESA_TARJETA = (
    ".artdeco-entity-lockup__subtitle",
    ".job-card-container__primary-description",
    ".job-card-container__company-name",
)
SELECTORES_UBICACION_TARJETA = (
    ".job-card-container__metadata-item",
    ".artdeco-entity-lockup__caption li",
    ".job-card-container__metadata-wrapper li",
)

# Posting detail.
SELECTORES_TITULO_DETALLE = (
    ".job-details-jobs-unified-top-card__job-title h1",
    ".job-details-jobs-unified-top-card__job-title",
    "h1.t-24",
    "h1",
)
SELECTORES_EMPRESA_DETALLE = (
    ".job-details-jobs-unified-top-card__company-name a",
    ".job-details-jobs-unified-top-card__company-name",
    ".job-details-jobs-unified-top-card__primary-description a",
)
# Line "Madrid, Comunidad de Madrid, España · hace 3 días · 42 solicitudes".
SELECTORES_UBICACION_DETALLE = (
    ".job-details-jobs-unified-top-card__primary-description-container",
    ".job-details-jobs-unified-top-card__primary-description",
    ".job-details-jobs-unified-top-card__bullet",
)
# Conditions block ("Jornada completa", "En remoto", "X €/año").
SELECTOR_INSIGHTS_DETALLE = (
    ".job-details-jobs-unified-top-card__job-insight, "
    ".job-details-jobs-unified-top-card__job-insight-view-model-secondary, "
    "li.job-details-jobs-unified-top-card__job-insight"
)
# Description body (already rendered text, no HTML).
SELECTORES_DESCRIPCION = (
    ".jobs-description__content .jobs-box__html-content",
    ".jobs-description-content__text",
    ".jobs-description__content",
    "#job-details",
    ".jobs-box__html-content",
)
# "…ver más" button that collapses the description.
SELECTORES_VER_MAS = (
    "button.show-more-less-html__button--more",
    ".jobs-description__footer-button",
    "button[aria-label*='más']",
    "button[aria-label*='more']",
)
# Overlays that cover the content and must be closed to read it.
SELECTORES_CERRAR_MODAL = (
    "button[aria-label='Descartar']",
    "button[aria-label='Dismiss']",
    "button.modal__dismiss",
    "button.artdeco-modal__dismiss",
)

# Signals that the session is unusable or LinkedIn blocked the request.
RUTAS_BLOQUEO = ("/login", "/authwall", "/checkpoint", "/uas/", "/signup")
TEXTOS_BLOQUEO = (
    "verificación de seguridad",
    "security verification",
    "unusual activity",
    "actividad inusual",
    "captcha",
    "demasiadas solicitudes",
    "too many requests",
    "has alcanzado el límite",
)

# Headings that introduce the requirements inside the description. Used only to
# trim the text; content is never reinterpreted.
ENCABEZADOS_REQUISITOS = (
    "requisitos",
    "requisitos del puesto",
    "requisitos imprescindibles",
    "lo que buscamos",
    "qué buscamos",
    "tu perfil",
    "perfil buscado",
    "requirements",
    "qualifications",
    "what we're looking for",
    "what we are looking for",
    "your profile",
    "must have",
)

# Work-mode vocabulary -> normalized Spanish value expected by scoring.py.
_MODALIDADES = (
    (("100% remoto", "fully remote", "remote", "remoto", "teletrabajo"), "Remoto"),
    (("híbrido", "hibrido", "hybrid"), "Híbrido"),
    (("presencial", "on-site", "onsite", "in office", "en oficina"), "Presencial"),
)

# Contract-type vocabulary -> normalized value.
_CONTRATOS = (
    (("jornada completa", "full-time", "full time", "tiempo completo"), "Jornada completa"),
    (("media jornada", "part-time", "part time", "tiempo parcial"), "Media jornada"),
    (("contrato temporal", "temporary", "temporal"), "Temporal"),
    (("prácticas", "internship", "becario", "beca"), "Prácticas"),
    (("freelance", "autónomo", "autonomo", "contractor"), "Freelance"),
    (("indefinido", "permanent"), "Indefinido"),
)

_RE_JOB_ID = re.compile(r"(\d{5,})")
# Amount with the currency in front ("45.000 €" shows as "€ 45.000" in English)
# or behind ("45.000 € - 55.000 € al año"), the usual form in Spain.
_RE_SALARIO = re.compile(
    r"(?:"
    r"(?:US\$|\$|€|£|CHF)\s?\d[\d.,]*(?:\s?(?:-|–|a)\s?(?:US\$|\$|€|£)?\s?\d[\d.,]*)?"
    r"|"
    r"\d[\d.,]*\s?(?:k€|€|EUR|USD|\$|£|CHF)"
    r"(?:\s?(?:-|–|a)\s?\d[\d.,]*\s?(?:k€|€|EUR|USD|\$|£|CHF))?"
    r")"
    r"(?:\s?(?:brutos|al año|/año|año|/year|per year|al mes|/mes|/month|/hora|/hour))?",
    re.IGNORECASE,
)


class LinkedInError(RuntimeError):
    """Failure while getting LinkedIn data (session, browser or page)."""


# --------------------------------------------------------------------------- #
# Anti-block cooldown
# --------------------------------------------------------------------------- #
# When LinkedIn blocks the request (rate-limit/anti-bot), retrying is useless:
# every extra request re-triggers the block. On detecting the wall a persistent
# cooldown is written to disk and later runs skip LinkedIn until it expires,
# without even opening the browser.

RUTA_COOLDOWN_DEFECTO = "../data/linkedin_cooldown.json"

# Default cooldown: 6 h. ``main.py`` may override the effective value with the
# one from ``settings.json`` (``LINKEDIN_COOLDOWN_SEGUNDOS``) through
# ``configurar_cooldown``. The environment variable remains the fallback for
# standalone runs (and for scripts calling ``search_jobs``).
COOLDOWN_SEGUNDOS_DEFECTO = 6 * 3600
VARIABLE_COOLDOWN = "LINKEDIN_COOLDOWN_SEGUNDOS"


def _cooldown_desde_entorno() -> int:
    """Read ``LINKEDIN_COOLDOWN_SEGUNDOS``, validating a positive integer."""
    crudo = (os.environ.get(VARIABLE_COOLDOWN) or "").strip()
    if not crudo:
        return COOLDOWN_SEGUNDOS_DEFECTO
    try:
        valor = int(crudo)
    except ValueError:
        log.warning(
            t(
                "%s=%r no es un entero; se usan %s s.",
                "%s=%r is not an integer; using %s s.",
            ),
            VARIABLE_COOLDOWN,
            crudo,
            COOLDOWN_SEGUNDOS_DEFECTO,
        )
        return COOLDOWN_SEGUNDOS_DEFECTO
    if valor <= 0:
        log.warning(
            t(
                "%s=%r no es positivo; se usan %s s.",
                "%s=%r is not positive; using %s s.",
            ),
            VARIABLE_COOLDOWN,
            crudo,
            COOLDOWN_SEGUNDOS_DEFECTO,
        )
        return COOLDOWN_SEGUNDOS_DEFECTO
    return valor


COOLDOWN_SEGUNDOS = _cooldown_desde_entorno()


def configurar_cooldown(segundos: int | None) -> int:
    """Set the effective cooldown (seconds) and return the applied value.

    ``main.py`` calls it with the ``settings.json`` value so the panel overrides
    the environment. A non-numeric, ``None`` or non-positive value is ignored
    (the current one is kept) with a warning: the run never aborts for this.
    """
    global COOLDOWN_SEGUNDOS
    if segundos is None:
        return COOLDOWN_SEGUNDOS
    try:
        valor = int(str(segundos).strip())
    except (TypeError, ValueError):
        log.warning(
            t(
                "Cooldown de LinkedIn inválido (%r); se conserva %s s.",
                "Invalid LinkedIn cooldown (%r); keeping %s s.",
            ),
            segundos,
            COOLDOWN_SEGUNDOS,
        )
        return COOLDOWN_SEGUNDOS
    if valor <= 0:
        log.warning(
            t(
                "Cooldown de LinkedIn no positivo (%r); se conserva %s s.",
                "Non-positive LinkedIn cooldown (%r); keeping %s s.",
            ),
            segundos,
            COOLDOWN_SEGUNDOS,
        )
        return COOLDOWN_SEGUNDOS
    COOLDOWN_SEGUNDOS = valor
    return COOLDOWN_SEGUNDOS


# Consecutive empty queries that reveal a block. The first queries (generic
# titles) should always return postings; empty results mean the anti-bot wall,
# not a niche query.
UMBRAL_CONSULTAS_VACIAS = 2


def _ruta_cooldown() -> Path:
    """Resolve the cooldown file path (env override allowed)."""
    crudo = os.environ.get("LINKEDIN_COOLDOWN_PATH") or RUTA_COOLDOWN_DEFECTO
    ruta = Path(os.path.expanduser(crudo.strip()))
    if not ruta.is_absolute():
        ruta = (RAIZ_SERVICIO / ruta).resolve()
    return ruta


def _cooldown_restante() -> int:
    """Seconds left in the cooldown, or 0 if none is active."""
    try:
        with _ruta_cooldown().open("r", encoding="utf-8") as fh:
            datos = json.load(fh)
        hasta = float(datos.get("hasta") or 0)
    except (OSError, ValueError, TypeError):
        return 0
    return max(0, int(hasta - time.time()))


def _marcar_cooldown(segundos: int | None = None) -> None:
    """Persist a cooldown of ``segundos`` (``COOLDOWN_SEGUNDOS`` by default)."""
    ruta = _ruta_cooldown()
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    hasta = time.time() + (segundos if segundos is not None else COOLDOWN_SEGUNDOS)
    tmp = ruta.with_name(ruta.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump({"hasta": hasta}, fh)
        os.replace(tmp, ruta)
    except OSError as exc:
        log.warning(
            t(
                "No se pudo escribir el cooldown de LinkedIn: %s",
                "Could not write the LinkedIn cooldown: %s",
            ),
            exc,
        )


def _formato_duracion(segundos: int) -> str:
    """Format a duration in seconds as ``45s``, ``12m`` or ``6.0h``."""
    if segundos < 60:
        return f"{segundos}s"
    if segundos < 3600:
        return f"{segundos // 60}m"
    return f"{segundos / 3600:.1f}h"


# --------------------------------------------------------------------------- #
# Daily request budget
# --------------------------------------------------------------------------- #
# The cooldown only reacts *after* LinkedIn blocks. To avoid getting there, every
# page navigation (search, pagination, posting detail) increments a persistent
# daily counter; once it reaches ``MAX_PETICIONES_LINKEDIN_DIA`` the source is
# skipped for the rest of the day. The counter lives in a small JSON file, same
# idea as the cooldown, so several runs in one day add up.

RUTA_USO_DEFECTO = "../data/linkedin_usage.json"
MAX_PETICIONES_DEFECTO = 60
VARIABLE_MAX_PETICIONES = "MAX_PETICIONES_LINKEDIN_DIA"


def _entero_entorno(variable: str, defecto: int) -> int:
    """Read a positive integer from the environment, falling back to ``defecto``."""
    crudo = (os.environ.get(variable) or "").strip()
    if not crudo:
        return defecto
    try:
        valor = int(crudo)
    except ValueError:
        log.warning(
            t(
                "%s=%r no es un entero; se usa %s.",
                "%s=%r is not an integer; using %s.",
            ),
            variable,
            crudo,
            defecto,
        )
        return defecto
    if valor <= 0:
        log.warning(
            t(
                "%s=%r no es positivo; se usa %s.",
                "%s=%r is not positive; using %s.",
            ),
            variable,
            crudo,
            defecto,
        )
        return defecto
    return valor


def _max_peticiones_dia() -> int:
    """Daily request ceiling (``MAX_PETICIONES_LINKEDIN_DIA``, default 60)."""
    return _entero_entorno(VARIABLE_MAX_PETICIONES, MAX_PETICIONES_DEFECTO)


def _ruta_uso() -> Path:
    """Resolve the usage counter path (env override allowed)."""
    crudo = os.environ.get("LINKEDIN_USAGE_PATH") or RUTA_USO_DEFECTO
    ruta = Path(os.path.expanduser(crudo.strip()))
    if not ruta.is_absolute():
        ruta = (RAIZ_SERVICIO / ruta).resolve()
    return ruta


def _hoy() -> str:
    """Local date in ``YYYY-MM-DD`` (the counter's day boundary)."""
    return time.strftime("%Y-%m-%d")


def _leer_uso() -> dict[str, Any]:
    """Read today's counter; any other date (or a broken file) counts as 0."""
    try:
        with _ruta_uso().open("r", encoding="utf-8") as fh:
            datos = json.load(fh)
    except (OSError, ValueError, TypeError):
        return {"fecha": _hoy(), "peticiones": 0}
    if not isinstance(datos, dict) or datos.get("fecha") != _hoy():
        return {"fecha": _hoy(), "peticiones": 0}
    try:
        peticiones = max(0, int(datos.get("peticiones") or 0))
    except (TypeError, ValueError):
        peticiones = 0
    return {"fecha": _hoy(), "peticiones": peticiones}


def _registrar_peticion() -> int:
    """Add one request to today's counter and return the new total.

    A new day resets the counter. A write failure only warns: the budget must
    never take a run down.
    """
    datos = _leer_uso()
    datos["peticiones"] = int(datos["peticiones"]) + 1
    ruta = _ruta_uso()
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        tmp = ruta.with_name(ruta.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(datos, fh)
        os.replace(tmp, ruta)
    except OSError as exc:
        log.warning(
            t(
                "No se pudo escribir el contador de uso de LinkedIn: %s",
                "Could not write the LinkedIn usage counter: %s",
            ),
            exc,
        )
    log.debug(
        t(
            "Petición LinkedIn %s/%s (%s).",
            "LinkedIn request %s/%s (%s).",
        ),
        datos["peticiones"],
        _max_peticiones_dia(),
        datos["fecha"],
    )
    return int(datos["peticiones"])


def _presupuesto_agotado() -> bool:
    """True if today's requests already reached ``MAX_PETICIONES_LINKEDIN_DIA``."""
    return _leer_uso()["peticiones"] >= _max_peticiones_dia()


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #


def _pausa(rango: Sequence[float]) -> None:
    """Sleep a random time inside the range (anti-bot pacing)."""
    time.sleep(random.uniform(rango[0], rango[1]))


def _ruta_sesion() -> Path:
    """Resolve the session path against ``service/`` when it is relative.

    Precedence: ``SESSION_PATH`` -> ``LINKEDIN_SESSION_PATH`` ->
    ``data/linkedin_session.json``. This is the only path resolution in the
    module, so the scraper and ``sesion_estado()`` always look at the same file.
    """
    crudo = (
        os.environ.get("SESSION_PATH")
        or os.environ.get("LINKEDIN_SESSION_PATH")
        or RUTA_SESION_DEFECTO
    )
    ruta = Path(os.path.expanduser(crudo.strip()))
    if not ruta.is_absolute():
        ruta = (RAIZ_SERVICIO / ruta).resolve()
    return ruta


def sesion_estado() -> dict[str, Any]:
    """Diagnostics for the session saved on disk (does not open the browser).

    Returns::

        {"presente": bool, "antiguedad_dias": int|None,
         "ruta": str, "caducada": bool}

    ``antiguedad_dias`` is the whole number of days since the file's last
    modification (``None`` if it does not exist or cannot be read).
    ``caducada`` is ``True`` when there is no session or it exceeds
    ``DIAS_CADUCIDAD_SESION``; a present session whose ``stat`` fails is also
    marked as expired, since it cannot be proven fresh.

    The file contents are never read: cookies are never exposed.
    """
    ruta = _ruta_sesion()
    presente = ruta.is_file()
    antiguedad: int | None = None
    if presente:
        try:
            segundos = max(0.0, time.time() - ruta.stat().st_mtime)
            antiguedad = int(segundos // 86400)
        except OSError as exc:
            log.warning(
                t(
                    "No se pudo leer la fecha de la sesión %s: %s",
                    "Could not read the session date of %s: %s",
                ),
                ruta,
                exc,
            )
            antiguedad = None

    caducada = (not presente) or antiguedad is None or antiguedad > DIAS_CADUCIDAD_SESION
    return {
        "presente": presente,
        "antiguedad_dias": antiguedad,
        "ruta": str(ruta),
        "caducada": caducada,
    }


def _extraer_job_id(valor: Any) -> str:
    """Extract the numeric posting id from an id, a URL or a URN."""
    texto = str(valor or "").strip()
    encontrado = _RE_JOB_ID.search(texto)
    if not encontrado:
        raise LinkedInError(f"job_id no válido: {texto[:80]!r}")
    return encontrado.group(1)


def _limpiar(texto: str | None) -> str | None:
    """Normalize whitespace and blank lines of extracted text."""
    if not texto:
        return None
    lineas = [linea.strip() for linea in texto.replace("\r", "\n").split("\n")]
    # Collapse runs of blank lines and trim both ends.
    salida: list[str] = []
    for linea in lineas:
        if not linea and salida and not salida[-1]:
            continue
        salida.append(linea)
    limpio = "\n".join(salida).strip()
    return limpio or None


def _cargar_sesion(ruta: Path) -> dict[str, Any]:
    """Read and validate LinkedIn's ``storage_state``.

    Two formats are accepted:
      * ``{"cookies": [...], "origins": [...]}`` — full storage_state.
      * ``[{...cookie...}, ...]`` — bare list (what ``mcp-server-linkedin``
        writes to ``~/.linkedin-mcp/cookies.json``).

    Always returns a dict with a non-empty ``cookies``. Cookie contents are
    never logged: only how many there are and whether the session cookie is
    missing.
    """
    if not ruta.exists():
        raise LinkedInError(
            f"No existe la sesión de LinkedIn en {ruta}. "
            "Ejecuta `python scripts/export_session.py` para copiarla desde "
            "~/.linkedin-mcp/cookies.json, o define SESSION_PATH con la ruta "
            "correcta."
        )

    try:
        with ruta.open("r", encoding="utf-8") as fichero:
            datos = json.load(fichero)
    except json.JSONDecodeError as exc:
        raise LinkedInError(f"La sesión de LinkedIn en {ruta} no es JSON válido: {exc}") from exc
    except OSError as exc:
        raise LinkedInError(f"No se pudo leer la sesión de LinkedIn en {ruta}: {exc}") from exc

    if isinstance(datos, list):
        estado: dict[str, Any] = {"cookies": datos, "origins": []}
    elif isinstance(datos, dict):
        estado = datos
    else:
        raise LinkedInError(
            f"Formato de sesión no reconocido en {ruta} (se esperaba un objeto "
            "storage_state o una lista de cookies)."
        )

    cookies = estado.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise LinkedInError(
            f"La sesión de LinkedIn en {ruta} no contiene cookies; expórtala de "
            "nuevo con `python scripts/export_session.py`."
        )

    # Soft check: without `li_at` there is no authenticated session. Warn but do
    # not abort (other valid cookies may exist and LinkedIn will say so on load).
    nombres = {str(c.get("name") or "") for c in cookies if isinstance(c, dict)}
    if "li_at" not in nombres:
        log.warning(
            t(
                "La sesión de %s no incluye la cookie de sesión 'li_at'; "
                "es probable que LinkedIn pida iniciar sesión.",
                "The session at %s does not include the 'li_at' session cookie; "
                "LinkedIn will probably ask to sign in.",
            ),
            ruta,
        )
    log.info(
        t(
            "Sesión de LinkedIn cargada desde %s (%s cookies).",
            "LinkedIn session loaded from %s (%s cookies).",
        ),
        ruta,
        len(cookies),
    )

    estado.setdefault("origins", [])
    return estado


# --------------------------------------------------------------------------- #
# Client: one patchright browser reused for the whole run
# --------------------------------------------------------------------------- #


class ClienteLinkedIn:
    """patchright browser + context + page, created only once.

    Normal use (implicit through ``search_jobs`` / ``get_job_details``)::

        cliente = ClienteLinkedIn()
        tarjetas = cliente.buscar("Software Engineer", maximo=20)
        oferta = cliente.abrir_oferta("4012345678")
        cliente.cerrar()
    """

    def __init__(self, ruta_sesion: Path | None = None, headless: bool | None = None):
        self.ruta_sesion = ruta_sesion or _ruta_sesion()
        if headless is None:
            headless = os.environ.get("LINKEDIN_HEADLESS", "1").strip() not in (
                "0",
                "false",
                "False",
            )
        self.headless = headless
        self._playwright = None
        self._navegador = None
        self._contexto = None
        self._pagina = None
        self._lock = threading.RLock()
        self._cerrado = False

    # -- lifecycle --------------------------------------------------------- #

    def __enter__(self) -> ClienteLinkedIn:
        self._arrancar()
        return self

    def __exit__(self, *_) -> None:
        self.cerrar()

    def _arrancar(self) -> None:
        """Load the session and open Chromium (only once)."""
        if self._pagina is not None:
            return
        with self._lock:
            if self._pagina is not None:
                return

            estado = _cargar_sesion(self.ruta_sesion)

            try:
                from patchright.sync_api import sync_playwright
            except ImportError as exc:  # pragma: no cover - depends on environment
                raise LinkedInError(
                    "patchright no está instalado. Instálalo con "
                    "`pip install patchright` y luego `patchright install chromium`."
                ) from exc

            log.info(
                t(
                    "Abriendo Chromium (patchright, headless=%s)...",
                    "Opening Chromium (patchright, headless=%s)...",
                ),
                self.headless,
            )
            try:
                self._playwright = sync_playwright().start()
                self._navegador = self._playwright.chromium.launch(
                    headless=self.headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--lang=es-ES",
                    ],
                )
                self._contexto = self._navegador.new_context(
                    storage_state=cast(Any, estado),
                    locale="es-ES",
                    timezone_id=os.environ.get("LINKEDIN_TIMEZONE", "Europe/Madrid"),
                    viewport={"width": 1440, "height": 900},
                    user_agent=os.environ.get("LINKEDIN_USER_AGENT"),
                    extra_http_headers={"Referer": "https://www.linkedin.com/jobs/"},
                )
                self._contexto.set_default_timeout(TIMEOUT_SELECTOR)
                self._pagina = self._contexto.new_page()
            except LinkedInError:
                raise
            except Exception as exc:  # noqa: BLE001 — re-raised as typed error
                self._limpiar_recursos()
                raise LinkedInError(
                    f"No se pudo abrir el navegador con patchright: {exc}. "
                    "Comprueba `patchright install chromium` (y, en Docker, "
                    "que exista /dev/shm o esté activo --disable-dev-shm-usage)."
                ) from exc

    def _limpiar_recursos(self) -> None:
        """Close whatever was opened, ignoring close errors."""
        for recurso, metodo in (
            (self._contexto, "close"),
            (self._navegador, "close"),
            (self._playwright, "stop"),
        ):
            if recurso is None:
                continue
            try:
                getattr(recurso, metodo)()
            except Exception:  # noqa: BLE001
                pass
        self._contexto = self._navegador = self._playwright = self._pagina = None

    def cerrar(self) -> None:
        """Close browser and driver. Idempotent."""
        with self._lock:
            if self._cerrado:
                return
            self._limpiar_recursos()
            self._cerrado = True

    # -- navigation -------------------------------------------------------- #

    def _ir(self, url: str) -> None:
        """Navigate with one retry and check that no login wall appeared.

        Every page load (search, pagination or detail) counts against the
        daily budget: it is the exact unit LinkedIn rate-limits.
        """
        self._arrancar()
        assert self._pagina is not None
        _registrar_peticion()
        for intento in (1, 2):
            try:
                self._pagina.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_NAVEGACION)
                break
            except Exception as exc:  # noqa: BLE001
                if intento == 2:
                    raise LinkedInError(f"No se pudo cargar {url}: {exc}") from exc
                log.warning(
                    t(
                        "Navegación fallida (%s); reintentando...",
                        "Navigation failed (%s); retrying...",
                    ),
                    type(exc).__name__,
                )
                time.sleep(random.uniform(2.0, 4.0))
        _pausa(PAUSA_TRAS_NAVEGAR)
        self._comprobar_bloqueo()

    def _comprobar_bloqueo(self) -> None:
        """Raise LinkedInError if LinkedIn asks for login or blocked access."""
        assert self._pagina is not None
        url = (self._pagina.url or "").lower()
        if any(ruta in url for ruta in RUTAS_BLOQUEO):
            raise LinkedInError(
                f"LinkedIn ha redirigido a {url}: la sesión ha caducado o no es válida. "
                "Vuelve a exportarla con scripts/export_session.py."
            )
        try:
            cuerpo = (self._pagina.inner_text("body", timeout=5_000) or "").lower()
        except Exception:  # noqa: BLE001
            cuerpo = ""
        for marca in TEXTOS_BLOQUEO:
            if marca in cuerpo:
                raise LinkedInError(
                    f"LinkedIn ha mostrado un control anti-bot ({marca!r}). "
                    "Espera un rato antes de reintentar y baja el ritmo."
                )

    def _click_si_existe(self, selectores: Sequence[str]) -> bool:
        """Click the first element of the list that exists. True if clicked."""
        assert self._pagina is not None
        for selector in selectores:
            try:
                localizador = self._pagina.locator(selector).first
                if localizador.count() and localizador.is_visible():
                    localizador.click(timeout=3_000)
                    _pausa((0.4, 1.0))
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    # -- search ------------------------------------------------------------ #

    def _contar_tarjetas(self) -> int:
        assert self._pagina is not None
        try:
            return int(self._pagina.locator(SELECTOR_TARJETA).count())
        except Exception:  # noqa: BLE001
            return 0

    def _scroll_resultados(self) -> None:
        """Scroll the results column a bit to trigger lazy loading."""
        assert self._pagina is not None
        try:
            self._pagina.evaluate(
                """(selector) => {
                    const caja = document.querySelector(selector);
                    if (caja) { caja.scrollBy(0, 600 + Math.random() * 400); return; }
                    window.scrollBy(0, 600 + Math.random() * 400);
                }""",
                SELECTOR_LISTA_SCROLL,
            )
        except Exception:  # noqa: BLE001
            pass
        _pausa(PAUSA_SCROLL)

    JS_TARJETAS = """
    (args) => {
        const [selectorTarjeta, selTitulo, selEmpresa, selUbicacion] = args;
        const texto = (raiz, selectores) => {
            for (const s of selectores) {
                const el = raiz.querySelector(s);
                if (!el) continue;
                const t = (el.innerText || el.textContent || '').trim();
                if (t) return t;
            }
            return null;
        };
        const salida = [];
        for (const tarjeta of document.querySelectorAll(selectorTarjeta)) {
            // id: stable attribute; if missing, taken from the posting link.
            let jobId = tarjeta.getAttribute('data-occludable-job-id') || '';
            const enlace = tarjeta.querySelector("a[href*='/jobs/view/']");
            const href = enlace ? enlace.getAttribute('href') || '' : '';
            if (!jobId) {
                const coincidencia = href.match(/\\/jobs\\/view\\/(\\d+)/);
                if (coincidencia) jobId = coincidencia[1];
            }
            if (!jobId) continue;
            salida.push({
                job_id: String(jobId).trim(),
                titulo: texto(tarjeta, selTitulo),
                empresa: texto(tarjeta, selEmpresa),
                ubicacion: texto(tarjeta, selUbicacion),
                url: 'https://www.linkedin.com/jobs/view/' + String(jobId).trim() + '/'
            });
        }
        return salida;
    }
    """

    def _extraer_tarjetas(self) -> list[dict[str, Any]]:
        assert self._pagina is not None
        try:
            crudo = self._pagina.evaluate(
                self.JS_TARJETAS,
                [
                    SELECTOR_TARJETA,
                    list(SELECTORES_TITULO_TARJETA),
                    list(SELECTORES_EMPRESA_TARJETA),
                    list(SELECTORES_UBICACION_TARJETA),
                ],
            )
        except Exception as exc:  # noqa: BLE001
            raise LinkedInError(f"No se pudo leer la lista de resultados: {exc}") from exc
        return [t for t in (crudo or []) if isinstance(t, dict) and t.get("job_id")]

    def buscar(self, query: str, maximo: int) -> list[dict[str, Any]]:
        """Search ``query`` and return up to ``maximo`` cards.

        Pages 25 at a time and stops once it has enough postings or the list
        stops growing.
        """
        encontradas: dict[str, dict[str, Any]] = {}
        inicio = 0
        self._arrancar()
        assert self._pagina is not None
        for pagina in range(MAX_PAGINAS):
            if len(encontradas) >= maximo:
                break
            # Pagination costs a request too: stop as soon as the day's budget
            # is gone instead of finishing the list.
            if _presupuesto_agotado():
                log.warning(
                    t(
                        "Presupuesto diario de LinkedIn agotado (%s peticiones); "
                        "se corta la paginación de %r.",
                        "LinkedIn daily budget exhausted (%s requests); "
                        "stopping pagination for %r.",
                    ),
                    _max_peticiones_dia(),
                    query,
                )
                break
            url = URL_BUSQUEDA.format(query=quote_plus(query), inicio=inicio)
            log.info(
                t(
                    "Buscando %r (página %s)...",
                    "Searching %r (page %s)...",
                ),
                query,
                pagina + 1,
            )
            self._ir(url)

            # Close overlays that may cover the list (cookie notice, modal).
            self._click_si_existe(SELECTORES_CERRAR_MODAL)

            # Wait for at least one card (a page without results simply has
            # none: exit without error).
            try:
                self._pagina.wait_for_selector(SELECTOR_TARJETA, timeout=8_000)
            except Exception:  # noqa: BLE001
                log.info(
                    t(
                        "Sin tarjetas para %r en la página %s.",
                        "No cards for %r on page %s.",
                    ),
                    query,
                    pagina + 1,
                )
                break

            anterior = -1
            for _ in range(MAX_SCROLLS):
                actual = self._contar_tarjetas()
                if actual >= maximo or actual == anterior:
                    break
                anterior = actual
                self._scroll_resultados()

            tarjetas = self._extraer_tarjetas()
            nuevas = 0
            for tarjeta in tarjetas:
                job_id = str(tarjeta["job_id"])
                if job_id not in encontradas:
                    encontradas[job_id] = tarjeta
                    nuevas += 1

            log.info(
                t(
                    "%r: %s tarjetas en la página, %s nuevas, %s acumuladas.",
                    "%r: %s cards on the page, %s new, %s total.",
                ),
                query,
                len(tarjetas),
                nuevas,
                len(encontradas),
            )
            if not nuevas:
                break  # LinkedIn stopped returning distinct results.

            inicio += OFERTAS_POR_PAGINA
            if len(encontradas) < maximo:
                _pausa(PAUSA_ENTRE_PAGINAS)

        return list(encontradas.values())[:maximo]

    # -- detail ------------------------------------------------------------ #

    JS_DETALLE = """
    (args) => {
        const [selTitulo, selEmpresa, selUbicacion, selInsights, selDescripcion] = args;
        const primero = (selectores) => {
            for (const s of selectores) {
                const el = document.querySelector(s);
                if (!el) continue;
                const t = (el.innerText || el.textContent || '').trim();
                if (t) return t;
            }
            return null;
        };
        const insights = [];
        for (const el of document.querySelectorAll(selInsights)) {
            const t = (el.innerText || el.textContent || '').trim();
            if (t) insights.push(t);
        }
        return {
            titulo: primero(selTitulo),
            empresa: primero(selEmpresa),
            ubicacion: primero(selUbicacion),
            insights: insights,
            descripcion: primero(selDescripcion),
            url: window.location.href
        };
    }
    """

    def abrir_oferta(self, job_id: str) -> dict[str, Any]:
        """Open a posting page and return the raw extracted text.

        Nothing is interpreted: fields are returned as the DOM gives them
        (``titulo``, ``empresa``, ``ubicacion``, ``insights``, ``descripcion``).
        """
        job_id = _extraer_job_id(job_id)
        url = URL_OFERTA.format(job_id=job_id)
        self._arrancar()
        assert self._pagina is not None
        self._ir(url)

        # The description renders after the top card: wait for it to exist.
        try:
            self._pagina.wait_for_selector(
                ", ".join(SELECTORES_DESCRIPCION), timeout=TIMEOUT_DESCARGA_DETALLE
            )
        except Exception:  # noqa: BLE001
            log.warning(
                t(
                    "Oferta %s sin selector de descripción; se extrae lo disponible.",
                    "Posting %s has no description selector; extracting what is available.",
                ),
                job_id,
            )

        # Expand "ver más" and do a short scroll so everything renders.
        self._click_si_existe(SELECTORES_VER_MAS)
        try:
            self._pagina.evaluate("window.scrollBy(0, 500 + Math.random() * 400)")
        except Exception:  # noqa: BLE001
            pass
        _pausa((0.6, 1.4))

        try:
            crudo = self._pagina.evaluate(
                self.JS_DETALLE,
                [
                    list(SELECTORES_TITULO_DETALLE),
                    list(SELECTORES_EMPRESA_DETALLE),
                    list(SELECTORES_UBICACION_DETALLE),
                    SELECTOR_INSIGHTS_DETALLE,
                    list(SELECTORES_DESCRIPCION),
                ],
            )
        except Exception as exc:  # noqa: BLE001
            raise LinkedInError(f"No se pudo leer la oferta {job_id}: {exc}") from exc

        if not isinstance(crudo, dict):
            crudo = {}
        crudo["job_id"] = job_id
        crudo["url"] = URL_OFERTA.format(job_id=job_id)
        if not (crudo.get("titulo") or crudo.get("descripcion")):
            raise LinkedInError(
                f"La oferta {job_id} no tiene contenido legible (¿retirada o bloqueada?)."
            )
        return crudo


# --------------------------------------------------------------------------- #
# Shared instance (one browser per process)
# --------------------------------------------------------------------------- #

_CLIENTE: ClienteLinkedIn | None = None
_LOCK_CLIENTE = threading.Lock()


def _obtener_cliente() -> ClienteLinkedIn:
    """Return the process-wide client, creating it on first use."""
    global _CLIENTE
    with _LOCK_CLIENTE:
        if _CLIENTE is None:
            # Validate the session BEFORE touching the browser: the usual
            # failure (missing file) must be a clear, immediate LinkedInError.
            _cargar_sesion(_ruta_sesion())
            _CLIENTE = ClienteLinkedIn()
            atexit.register(_cerrar_silencioso)
        return _CLIENTE


def close() -> None:
    """Close the shared browser. Called only at the end of the run."""
    global _CLIENTE
    with _LOCK_CLIENTE:
        cliente, _CLIENTE = _CLIENTE, None
    if cliente is not None:
        cliente.cerrar()


def _cerrar_silencioso() -> None:
    try:
        close()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Detail normalization
# --------------------------------------------------------------------------- #


def _clasificar(texto: str | None, tabla: Sequence[Sequence[Any]]) -> str | None:
    """Return the normalized value of the first vocabulary match."""
    if not texto:
        return None
    minuscula = texto.lower()
    for agujas, valor in tabla:
        if any(aguja in minuscula for aguja in agujas):
            return valor
    return None


def _limpiar_ubicacion(texto: str | None) -> str | None:
    """Trim the location line to the part before the first ' · '."""
    if not texto:
        return None
    primera = next((linea.strip() for linea in texto.splitlines() if linea.strip()), "")
    if not primera:
        return None
    return primera.split(" · ")[0].strip() or None


def _extraer_requisitos(descripcion: str | None) -> str | None:
    """Trim the requirements block out of the description, if it has a heading.

    Pure extraction: a known heading is located and the text below it is
    returned. The content is neither summarized nor interpreted.
    """
    if not descripcion:
        return None
    lineas = descripcion.splitlines()
    for indice, linea in enumerate(lineas):
        limpia = linea.strip().lower().rstrip(":").strip()
        if not limpia or len(limpia) > 60:
            continue
        if any(
            limpia == enc or limpia.startswith(enc + " ") or limpia.startswith(enc + ":")
            for enc in ENCABEZADOS_REQUISITOS
        ):
            bloque = "\n".join(lineas[indice + 1 :]).strip()
            if bloque:
                return bloque
    return None


def _extraer_salario(textos: Iterable[str | None]) -> str | None:
    """Return the first text fragment that looks like a salary."""
    for texto in textos:
        if not texto:
            continue
        for linea in texto.splitlines():
            encontrado = _RE_SALARIO.search(linea)
            if encontrado:
                return encontrado.group(0).strip()
    return None


def normalizar_detalle(crudo: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Turn the raw page text into the contract dict.

    ``crudo`` comes from ``ClienteLinkedIn.abrir_oferta``. Every field comes
    from the DOM as is; the only processing is trimming (location,
    requirements, salary) and classifying work mode and contract with a closed
    vocabulary. Anything missing stays ``None``.
    """
    job_id = _extraer_job_id(job_id)
    insights = crudo.get("insights") or []
    if not isinstance(insights, list):
        insights = [str(insights)]
    texto_insights = " \n".join(str(i) for i in insights if i)

    descripcion = _limpiar(crudo.get("descripcion"))
    ubicacion = _limpiar_ubicacion(crudo.get("ubicacion"))

    return {
        "job_id": job_id,
        "titulo": _limpiar(crudo.get("titulo")),
        "empresa": _limpiar(crudo.get("empresa")),
        "url": crudo.get("url") or URL_OFERTA.format(job_id=job_id),
        "ubicacion": ubicacion,
        "descripcion": descripcion,
        "requisitos": _extraer_requisitos(descripcion),
        "salario": _extraer_salario([texto_insights, descripcion]),
        # Work mode: conditions block first, then the location.
        "remoto": _clasificar(texto_insights, _MODALIDADES) or _clasificar(ubicacion, _MODALIDADES),
        "contrato": _clasificar(texto_insights, _CONTRATOS)
        or _clasificar(descripcion[:1500] if descripcion else None, _CONTRATOS),
    }


# --------------------------------------------------------------------------- #
# Public API (synchronous)
# --------------------------------------------------------------------------- #


def search_jobs(queries: Iterable[str] | str, limit: int = 20) -> list[dict[str, Any]]:
    """Search LinkedIn postings for one or several queries.

    Args:
        queries: a string or a list of search strings.
        limit:   maximum postings to return (global, after deduplication).

    Returns a list of dicts with ``job_id``, ``titulo``, ``empresa`` and
    ``url``, deduplicated by ``job_id``, in order of appearance.

    Raises ``LinkedInError`` if the session is missing or if no query returns
    results because of a failure (a failed query does not take the others down).

    The cooldown is set through ``configurar_cooldown`` (``main.py`` does it
    with the ``settings.json`` value); the signature is kept stable because
    some scripts check it.
    """
    if isinstance(queries, str):
        lista_queries = [queries]
    else:
        lista_queries = [str(q) for q in (queries or [])]
    lista_queries = [q.strip() for q in lista_queries if isinstance(q, str) and q.strip()]

    if not lista_queries or limit <= 0:
        return []

    # With an active cooldown (previous block) LinkedIn is skipped without
    # opening the browser: retrying only re-triggers the block.
    restante = _cooldown_restante()
    if restante > 0:
        raise LinkedInError(
            f"LinkedIn en cooldown ({_formato_duracion(restante)} restantes por bloqueo previo): "
            "se omite este run."
            "este run."
        )

    # Daily request budget: never even start if today's pages are spent. The
    # counter file is shared across runs, so several runs cannot add up to a
    # block.
    if _presupuesto_agotado():
        log.warning(
            t(
                "Presupuesto diario de LinkedIn agotado (%s peticiones hoy); "
                "se omite LinkedIn en este run.",
                "LinkedIn daily budget exhausted (%s requests today); "
                "skipping LinkedIn in this run.",
            ),
            _max_peticiones_dia(),
        )
        return []

    cliente = _obtener_cliente()

    encontradas: dict[str, dict[str, Any]] = {}
    errores: list[str] = []
    consultas_vacias = 0

    for indice, query in enumerate(lista_queries):
        if len(encontradas) >= limit:
            break
        if _presupuesto_agotado():
            log.warning(
                t(
                    "Presupuesto diario de LinkedIn agotado (%s peticiones); "
                    "se dejan %s consultas sin buscar.",
                    "LinkedIn daily budget exhausted (%s requests); leaving %s queries unsearched.",
                ),
                _max_peticiones_dia(),
                len(lista_queries) - indice,
            )
            break
        try:
            tarjetas = cliente.buscar(query, maximo=limit)
        except LinkedInError as exc:
            # A session/block failure invalidates the whole run: propagate.
            if "sesión" in str(exc).lower() or "anti-bot" in str(exc).lower():
                raise
            errores.append(f"{query}: {exc}")
            log.warning(
                t(
                    "Búsqueda %r fallida: %s",
                    "Search %r failed: %s",
                ),
                query,
                exc,
            )
            continue

        # Circuit breaker: several consecutive queries with no results reveal
        # the anti-bot wall (the first ones should always return postings). Mark
        # the cooldown and abort so the block is not re-triggered.
        if not tarjetas:
            consultas_vacias += 1
            if consultas_vacias >= UMBRAL_CONSULTAS_VACIAS:
                _marcar_cooldown()
                raise LinkedInError(
                    f"LinkedIn devolvió 0 resultados en {consultas_vacias} consultas seguidas "
                    "(bloqueo anti-bot probable). Se detiene la búsqueda y se "
                    f"activa un cooldown de {_formato_duracion(COOLDOWN_SEGUNDOS)}."
                )
        else:
            consultas_vacias = 0

        for tarjeta in tarjetas:
            job_id = str(tarjeta.get("job_id") or "").strip()
            if not job_id or job_id in encontradas:
                continue
            encontradas[job_id] = {
                "job_id": job_id,
                "titulo": _limpiar(tarjeta.get("titulo")),
                "empresa": _limpiar(tarjeta.get("empresa")),
                "url": tarjeta.get("url") or URL_OFERTA.format(job_id=job_id),
            }
            if len(encontradas) >= limit:
                break

        # Pacing between queries: never two searches back to back.
        if indice < len(lista_queries) - 1 and len(encontradas) < limit:
            _pausa(PAUSA_ENTRE_QUERIES)

    if errores and not encontradas:
        raise LinkedInError("Todas las búsquedas fallaron: " + " | ".join(errores))

    return list(encontradas.values())[:limit]


def get_job_details(job_id: str) -> dict[str, Any] | None:
    """Return the normalized detail of a posting, or ``None`` when skipped.

    Reuses the same browser as ``search_jobs`` (does not open a new one). The
    resulting dict always contains ``job_id``, ``titulo``, ``empresa``,
    ``url``, ``ubicacion``, ``descripcion``, ``requisitos``, ``salario``,
    ``remoto`` and ``contrato``; missing fields are ``None``.

    ``None`` means LinkedIn was not touched: the daily budget
    (``MAX_PETICIONES_LINKEDIN_DIA``) is spent, so callers keep the card data.
    The cap of details per run (``MAX_DETALLES_LINKEDIN_RUN``) is applied by
    ``main.py`` before calling this function.
    """
    job_id = _extraer_job_id(job_id)
    if _presupuesto_agotado():
        log.warning(
            t(
                "Presupuesto diario de LinkedIn agotado (%s peticiones); sin detalle para %s.",
                "LinkedIn daily budget exhausted (%s requests); no details for %s.",
            ),
            _max_peticiones_dia(),
            job_id,
        )
        return None
    cliente = _obtener_cliente()
    crudo = cliente.abrir_oferta(job_id)
    detalle = normalizar_detalle(crudo, job_id)
    _pausa(PAUSA_ENTRE_OFERTAS)
    return detalle
