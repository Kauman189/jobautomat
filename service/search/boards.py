"""search/boards.py — Job search across public sources (no LinkedIn).

LinkedIn (``search/linkedin.py``) is the main searcher, but it ends up hitting
rate limits and killing the run. This module adds an **alternative source**
that depends neither on LinkedIn nor on any session: public ATS boards
(Greenhouse, Lever, Ashby) and remote job portals (Remotive, We Work Remotely).
They are open APIs/RSS feeds: free, no auth, no relevant request limits.

Sources and endpoints (the response shapes parsed here are the ones those
services return today):

  * **Remotive** — ``https://remotive.com/api/remote-jobs`` with ``category``
    and/or ``search``. Returns ``{"jobs": [...]}`` with the description in HTML.
    *Legal note:* Remotive requires attribution and a link to the original
    posting; hence every offer carries ``source = "remotive"`` and ``url`` is
    Remotive's (which points to the original posting).
  * **We Work Remotely** — RSS feed of the design category, parsed with
    ``xml.etree.ElementTree`` (stdlib).
  * **Ashby** — ``https://api.ashbyhq.com/posting-api/job-board/{slug}``.
    Returns everything in a single call: title, location, workplace type,
    salary (``compensation.compensationTierSummary``) and plain description.
  * **Greenhouse** — list at ``/v1/boards/{slug}/jobs``; the description
    (``content``, HTML) only exists in the ``/jobs/{id}`` detail, so it is
    requested only for postings passing the title filter.
  * **Lever** — ``https://api.lever.co/v0/postings/{slug}?mode=json``.
    Returns the description in the list itself.
  * **Recruitee** — ``https://{company}.recruitee.com/api/offers/``. Public
    board JSON (no key). Carries title, HTML description, city, country,
    ``remote``/``hybrid``/``on_site``, ``employment_type_code``, the salary
    range (almost always empty) and ``published_at`` formatted as
    ``"2026-10-15 09:31:36 UTC"`` (normalized by ``_fecha_publicacion``).
  * **Workable** — ``https://apply.workable.com/api/v1/widget/accounts/{company}``
    with ``?details=true``: the same JSON feeding its public widget, no key.
    Carries description, ``published_on``, ``telecommuting`` and
    ``employment_type``.
  * **BambooHR** — ``https://{company}.bamboohr.com/careers/list`` (index, no
    description or date) and ``/careers/{id}/detail`` (HTML description,
    ``datePosted``, ``compensation``). The detail is requested only for
    postings passing the title filter.
  * **EURES** — ``POST https://europa.eu/eures/api/jv-searchengine/public/jv-search/search``.
    The portal's public search API: no key, no registration. Returns
    ``{"numberRecords", "jvs": [...]}`` with ``creationDate`` as epoch (ms)
    and the ``id`` used to build the public detail URL.
  * **InfoJobs** — **requires a key**: its API needs ``client_id`` and
    ``client_secret`` (app registered at developer.infojobs.net) and
    authenticates with Basic. Without ``INFOJOBS_CLIENT_ID`` /
    ``INFOJOBS_CLIENT_SECRET`` in the environment the source returns ``[]``
    without issuing any request (see ``buscar_infojobs``).

Workable needs no key: it uses the company's public widget endpoint
(``apply.workable.com/api/v1/widget/accounts/{company}``), not the
management API (``spi/v3``), which does require one.

ATS auto-detection
------------------
Which company uses which ATS is not maintained by hand: for each slug in
``EMPRESAS_SLUGS``, Greenhouse -> Lever -> Ashby are tried in order and the
first one returning postings wins. Unresponsive boards are dropped silently
(with a log notice). The company list therefore only needs to grow, never to
be classified.

Output contract (identical to ``search/linkedin.py`` so ``scoring.py`` and
``main.py`` work unchanged)::

    {"job_id", "titulo", "empresa", "url", "ubicacion",
     "remoto", "salario", "contrato", "descripcion", "fecha_publicacion"}

Plus ``source`` (source name; required to credit Remotive).
``fecha_publicacion`` is normalized to ``YYYY-MM-DD`` (or ``None`` when the
source does not publish it): ``main.py`` uses it to drop postings that are too
old. Anything unknown stays ``None``: no data is invented.

``remoto`` is normalized to the ``scoring.py`` vocabulary (``Remoto`` /
``Híbrido`` / ``Presencial``) and ``contrato`` to
``Jornada completa`` / ``Media jornada`` / ``Temporal`` / ``Prácticas`` /
``Freelance`` / ``Indefinido``.

``job_id`` values are prefixed by source (``remotive-…``, ``gh-…``,
``lever-…``, ``ashby-…``, ``wwr-…``) to avoid clashing with LinkedIn's numeric
ids in the SQLite table.

Design
------
* Stdlib only: ``urllib`` for HTTP, ``json`` for the APIs and
  ``ElementTree`` for the RSS feed. No new dependencies.
* **Per-source isolation**: each source runs in its own ``try/except``, so a
  down, slow or reshaped source never takes the others down.
* **Bounded parallelism** (``ThreadPoolExecutor``) in ATS auto-detection:
  ~50 companies x 1-3 sequential requests would take minutes.
* No secrets: these APIs are public and no auth header is ever sent.
"""

from __future__ import annotations

import base64
import datetime
import email.utils
import hashlib
import html
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

TIMEOUT_HTTP = 20  # seconds per request
MAX_HILOS = 8  # max concurrent requests (courtesy towards the APIs)

# Headers: identify as a regular HTTP client. No cookies, no auth.
CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/xml, application/xml, text/html;q=0.9, */*;q=0.8",
}

URL_REMOTIVE = "https://remotive.com/api/remote-jobs"
URL_WWR_DESIGN = "https://weworkremotely.com/categories/remote-design-jobs.rss"
URL_ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
URL_GREENHOUSE_LISTA = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
URL_GREENHOUSE_DETALLE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
URL_LEVER = "https://api.lever.co/v0/postings/{slug}?mode=json&limit=200"

# --- Additional sources (all unauthenticated except InfoJobs) --------------- #
URL_RECRUITEE = "https://{slug}.recruitee.com/api/offers/"
URL_WORKABLE = "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
URL_BAMBOOHR_LISTA = "https://{slug}.bamboohr.com/careers/list"
URL_BAMBOOHR_DETALLE = "https://{slug}.bamboohr.com/careers/{job_id}/detail"
URL_EURES_BUSQUEDA = "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
# The public EURES detail page is a SPA: the URL is enough to open the posting.
URL_EURES_DETALLE = "https://europa.eu/eures/portal/jv-se/jv-details/{job_id}?lang=en"
# InfoJobs requires a registered app (client_id + client_secret). Without those
# variables the source issues no request and returns [].
URL_INFOJOBS = "https://api.infojobs.net/api/9/offer"
VARIABLE_INFOJOBS_ID = "INFOJOBS_CLIENT_ID"
VARIABLE_INFOJOBS_SECRET = "INFOJOBS_CLIENT_SECRET"

# Companies with a Recruitee board. The slug is the subdomain: just add the
# ones of interest; missing ones are dropped silently (404).
RECRUITEE_EMPRESAS: tuple[str, ...] = (
    "mayflower",  # verified: 51 postings, 3 design
    "nacardesign",  # verified: 16 postings, 13 design
    "snapp",  # verified: 23 postings, 2 design
    "eleken",  # verified: 4 postings, 1 design
    "mailerlite",  # verified: 3 postings, 1 design
    "up42gmbh",  # verified: 1 design posting
    "bunq",  # verified: board with postings (no design ones today)
)

# Companies with a Workable board (public widget endpoint). Huge boards
# (1,000+ openings) are skipped: the payload is not worth it.
WORKABLE_EMPRESAS: tuple[str, ...] = (
    "qodea",  # verified: 20 postings, 3 design
    "devsu",  # verified: 29 postings, 1 design (UX, Spanish)
    "ndax",  # verified: 11 postings, 1 design
    "zealthy",  # verified: 10 postings, 1 design
    "bakery-agency",  # verified: 6 postings, 1 design
    "huggingface",  # verified: responds, no design ones today
)

# Companies with a BambooHR board. The slug is the *.bamboohr.com subdomain.
BAMBOOHR_EMPRESAS: tuple[str, ...] = (
    "konfio",  # verified: board with postings
    "greenparkcontent",  # verified: board with design postings
    "studios",  # verified: board with design postings
    "nprc",  # verified: board with postings
)

# Queries issued against the EURES public search. Empty by default: with no
# configured terms there is nothing to search for and the source returns [].
BUSQUEDAS_EURES: tuple[str, ...] = ()

# Maximum age in days, mirroring ``main.MAX_ANTIGUEDAD_DIAS``: the new sources
# already drop at the origin what ``main`` would drop again. ``main.py`` may set
# the editable ``settings.json`` value through ``configurar_antiguedad`` (or the
# ``max_antiguedad_dias`` argument).
MAX_ANTIGUEDAD_DIAS = 30
VARIABLE_MAX_ANTIGUEDAD = "MAX_ANTIGUEDAD_DIAS"
_DIAS_ANTIGUEDAD_CONFIGURADOS: int | None = None


def configurar_antiguedad(dias: int | None) -> int | None:
    """Set the maximum age in days applied by the board sources.

    Args:
        dias: Days to apply. ``None`` or an invalid value restores the
            previous behaviour (environment variable or 30).

    Returns:
        The applied value, or ``None`` when falling back to the default.
    """
    global _DIAS_ANTIGUEDAD_CONFIGURADOS
    if dias is None:
        _DIAS_ANTIGUEDAD_CONFIGURADOS = None
        return None
    try:
        valor = int(str(dias).strip())
    except (TypeError, ValueError):
        log.warning("Antigüedad de boards inválida (%r); se usa el respaldo.", dias)
        _DIAS_ANTIGUEDAD_CONFIGURADOS = None
        return None
    if valor <= 0:
        log.warning("Antigüedad de boards no positiva (%r); se usa el respaldo.", dias)
        _DIAS_ANTIGUEDAD_CONFIGURADOS = None
        return None
    _DIAS_ANTIGUEDAD_CONFIGURADOS = valor
    return valor


# Remotive text queries besides the configured terms. Empty by default: the
# searches come from ROLES + TOPICS; without them only the neutral category
# request would run, so the source returns [].
BUSQUEDAS_REMOTIVE: tuple[str, ...] = ()

# Candidate companies. Not all of them need to exist: auto-detection drops the
# ones returning no postings.
EMPRESAS_SLUGS: tuple[str, ...] = (
    "stripe",
    "airbnb",
    "shopify",
    "spotify",
    "duolingo",
    "reddit",
    "cloudflare",
    "datadog",
    "dropbox",
    "block",
    "twilio",
    "zapier",
    "doordash",
    "instacart",
    "robinhood",
    "chime",
    "affirm",
    "plaid",
    "wise",
    "revolut",
    "klarna",
    "monzo",
    "n26",
    "glovo",
    "cabify",
    "wallapop",
    "jobandtalent",
    "travelperk",
    "factorial",
    "typeform",
    "deepl",
    "linear",
    "vercel",
    "ramp",
    "notion",
    "figma",
    "framer",
    "deel",
    "openai",
    "anthropic",
    "scale",
    "mercury",
    "brex",
    "rippling",
    "retool",
    "vanta",
    "posthog",
    "supabase",
    "gitlab",
    "netflix",
)

# Title filter: default hints for design-like roles. 'ui'/'ux' are matched as
# whole words (\b) to avoid catching "building", "guide", "quit"… The real
# roles come from ROLES + TOPICS and are appended in _TERMINOS_TITULO_EXTRA.
PALABRAS_TITULO = (
    "interface",
    "design",
    "diseño",
    "producto",
    "ux",
    "ui",
)

# Extra terms from the editable config (ROLES + TOPICS). Added as title
# substrings, like PALABRAS_TITULO, so a new role ("Service Designer",
# "Content Designer"…) works without touching code.
_TERMINOS_TITULO_EXTRA: tuple[str, ...] = ()


def configurar_terminos(terminos: Iterable[str] | None) -> tuple[str, ...]:
    """Extend the title filter with ``ROLES`` + ``TOPICS`` from the config.

    Args:
        terminos: Candidate terms; compared lowercase and accent-free.

    Returns:
        The applied tuple (empty when nothing is passed, which keeps the
        historical filter).
    """
    global _TERMINOS_TITULO_EXTRA
    limpios: list[str] = []
    for termino in terminos or []:
        texto = _sin_acentos(str(termino or "")).strip()
        if len(texto) < 3 or texto in limpios:
            continue
        limpios.append(texto)
    _TERMINOS_TITULO_EXTRA = tuple(limpios)
    return _TERMINOS_TITULO_EXTRA


# Proper casing of known brands (the slug does not carry it). Cosmetic only:
# the company name is always derived from its board slug.
_MARCAS = {
    "openai": "OpenAI",
    "deepl": "DeepL",
    "gitlab": "GitLab",
    "posthog": "PostHog",
    "n26": "N26",
    "jobandtalent": "JobandTalent",
    "weworkremotely": "We Work Remotely",
    "wwr": "We Work Remotely",
    "ui": "UI",
    "ux": "UX",
}

# Workplace type -> scoring.py vocabulary.
_MODALIDADES = (
    (("remote", "remoto", "teletrabajo", "100% remote", "fully remote"), "Remoto"),
    (("hybrid", "híbrido", "hibrido"), "Híbrido"),
    (
        ("onsite", "on-site", "on site", "in-office", "in office", "presencial", "office"),
        "Presencial",
    ),
)

# Contract type -> scoring.py vocabulary.
_CONTRATOS = (
    (
        (
            "full-time",
            "full time",
            "fulltime",
            "permanent",
            "indefinido",
            "jornada completa",
            "tiempo completo",
        ),
        "Jornada completa",
    ),
    (("part-time", "part time", "parttime", "media jornada", "tiempo parcial"), "Media jornada"),
    (("internship", "intern", "becario", "beca", "prácticas", "practicas"), "Prácticas"),
    (("freelance", "contractor", "autónomo", "autonomo", "contract"), "Freelance"),
    (("temporary", "temporal", "fixed term", "fixed-term"), "Temporal"),
)

_RE_ETIQUETAS = re.compile(r"<[^>]+>")
_RE_ESPACIOS = re.compile(r"[ \t\u00a0]+")
_RE_SALTO = re.compile(r"(?i)<br\s*/?>|</(?:p|li|div|h[1-6]|tr)>")

# Maximum stored description length (cost control in scoring).
_MAX_DESCRIPCION = 12000


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def _descargar(url: str, timeout: int = TIMEOUT_HTTP) -> bytes:
    """Download ``url`` and return the raw body.

    Args:
        url: Absolute URL to fetch.
        timeout: Socket timeout in seconds.

    Returns:
        The response body as bytes.

    Raises:
        urllib.error.URLError: On network failure or HTTP error; each source
            decides how to handle it.
    """
    peticion = urllib.request.Request(url, headers=CABECERAS)
    with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
        return respuesta.read()


def _json(url: str) -> Any:
    """Download ``url`` and parse it as JSON."""
    return json.loads(_descargar(url).decode("utf-8", errors="replace"))


def _json_post(url: str, cuerpo: dict[str, Any], timeout: int = TIMEOUT_HTTP) -> Any:
    """POST ``cuerpo`` as JSON and return the parsed response.

    Used by EURES, whose search is a POST with filters in the body. No
    credentials are sent: the API is public.

    Args:
        url: Absolute endpoint URL.
        cuerpo: Payload serialized as JSON.
        timeout: Socket timeout in seconds.

    Returns:
        The parsed JSON response.
    """
    datos = json.dumps(cuerpo).encode("utf-8")
    cabeceras = dict(CABECERAS)
    cabeceras["Content-Type"] = "application/json"
    peticion = urllib.request.Request(url, data=datos, headers=cabeceras, method="POST")
    with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
        return json.loads(respuesta.read().decode("utf-8", errors="replace"))


# --------------------------------------------------------------------------- #
# Normalization helpers
# --------------------------------------------------------------------------- #


def _sin_acentos(texto: str) -> str:
    """Lowercase and strip accents, for loose text comparison."""
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


def _limpio(valor: Any) -> str | None:
    """Return the trimmed text, or ``None`` when nothing useful is left."""
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def _fecha_desde_epoch(numero: Any) -> str | None:
    """Convert an epoch (seconds or milliseconds) to ``YYYY-MM-DD``.

    Lever publishes ``createdAt`` in milliseconds; the unit is told apart by
    magnitude (an epoch in seconds does not reach 1e11 until year 5138).

    Args:
        numero: Epoch value, numeric or numeric string.

    Returns:
        The date as ``YYYY-MM-DD``, or ``None`` when it cannot be parsed.
    """
    try:
        valor = float(numero)
    except (TypeError, ValueError):
        return None
    if valor != valor or valor in (float("inf"), float("-inf")):
        return None
    if abs(valor) >= 1e11:  # milliseconds
        valor /= 1000.0
    try:
        momento = datetime.datetime.fromtimestamp(valor, tz=datetime.UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return momento.date().isoformat()


def _fecha_publicacion(valor: Any) -> str | None:
    """Normalize a posting's publication date to ``YYYY-MM-DD``.

    Each source provides it differently: ISO 8601 (Greenhouse ``updated_at``,
    Ashby ``publishedAt``/``updatedAt``, Remotive ``publication_date``), epoch
    in milliseconds (Lever ``createdAt``) or RFC 822 (WWR RSS).

    Args:
        valor: Raw date value from the source.

    Returns:
        The normalized date, or ``None`` when missing or unparseable: no date
        is invented.
    """
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, datetime.datetime):
        return valor.date().isoformat()
    if isinstance(valor, datetime.date):
        return valor.isoformat()
    if isinstance(valor, (int, float)):
        return _fecha_desde_epoch(valor)

    texto = str(valor).strip()
    if not texto:
        return None
    # Recruitee publishes "2026-10-15 09:31:36 UTC": turn it into an ISO offset.
    if texto.upper().endswith(" UTC"):
        texto = texto[:-4].rstrip() + "+00:00"
    # Epoch serialized as text ("1700000000000").
    if re.fullmatch(r"-?\d+(?:[.,]\d+)?", texto):
        return _fecha_desde_epoch(texto.replace(",", "."))

    candidato = texto[:-1] + "+00:00" if texto.endswith(("Z", "z")) else texto
    try:
        return datetime.datetime.fromisoformat(candidato).date().isoformat()
    except ValueError:
        pass
    try:  # RFC 822 from RSS: "Mon, 21 Oct 2026 00:00:00 +0000"
        momento = email.utils.parsedate_to_datetime(texto)
    except (TypeError, ValueError):
        return None
    return momento.date().isoformat() if momento else None


def _texto_plano(valor: Any, maximo: int = _MAX_DESCRIPCION) -> str | None:
    """Turn HTML (or text with tags) into readable plain text.

    Only text is extracted: nothing in the content is interpreted or executed.

    Args:
        valor: HTML or tagged text.
        maximo: Maximum returned length.

    Returns:
        The plain text, or ``None`` when nothing is left.
    """
    if not valor:
        return None
    texto = str(valor)
    # Some APIs (Greenhouse) return escaped HTML: unescape before stripping
    # tags, or the tags would remain visible as text.
    texto = html.unescape(texto)
    texto = _RE_SALTO.sub("\n", texto)
    texto = _RE_ETIQUETAS.sub(" ", texto)
    texto = html.unescape(texto)

    lineas = [
        _RE_ESPACIOS.sub(" ", linea).strip() for linea in texto.replace("\r", "\n").split("\n")
    ]
    salida: list[str] = []
    for linea in lineas:
        if not linea and (not salida or not salida[-1]):
            continue  # collapse consecutive empty lines
        salida.append(linea)
    limpio = "\n".join(salida).strip()
    if not limpio:
        return None
    return limpio[:maximo]


def _clasificar(texto: str | None, tabla: Sequence[Sequence[Any]]) -> str | None:
    """Return the normalized value of the first match, or ``None``."""
    if not texto:
        return None
    buscado = _sin_acentos(str(texto))
    for agujas, valor in tabla:
        if any(aguja in buscado for aguja in agujas):
            return valor
    return None


def _normalizar_remoto(*pistas: str | None) -> str | None:
    """Infer the workplace type from several hints (workplaceType, location…).

    Hints are tried in order; the first one yielding a known type wins. If none
    declares it, ``None`` is returned (remote is never assumed).

    Args:
        *pistas: Raw hints from the source.

    Returns:
        ``Remoto`` / ``Híbrido`` / ``Presencial``, or ``None``.
    """
    for pista in pistas:
        modalidad = _clasificar(pista, _MODALIDADES)
        if modalidad:
            return modalidad
    return None


def _empresa_desde_slug(slug: str) -> str:
    """Derive a readable company name from the board slug."""
    if slug.lower() in _MARCAS:
        return _MARCAS[slug.lower()]
    partes = [p for p in re.split(r"[-_]", slug) if p]
    return " ".join(_MARCAS.get(p.lower(), p.capitalize()) for p in partes) or slug


def _titulo_relevante(titulo: str | None) -> bool:
    """True if the title looks like a design role or a configured role/topic."""
    if not titulo:
        return False
    buscado = _sin_acentos(titulo)
    for palabra in tuple(PALABRAS_TITULO) + _TERMINOS_TITULO_EXTRA:
        if len(palabra) <= 2:
            # 'ui'/'ux': whole word, so "building" or "guide" do not match.
            if re.search(rf"\b{re.escape(palabra)}\b", titulo, re.IGNORECASE):
                return True
        elif palabra in buscado:
            return True
    return False


def _job_id(prefijo: str, identificador: Any, respaldo: str = "") -> str:
    """Build a stable ``job_id`` that is unique across sources.

    Args:
        prefijo: Source prefix (``gh``, ``lever``…).
        identificador: Source-side id; a hash of ``respaldo`` is used when empty.
        respaldo: Fallback text to hash.

    Returns:
        The prefixed identifier.
    """
    valor = str(identificador or "").strip()
    if not valor:
        valor = hashlib.sha1(respaldo.encode("utf-8")).hexdigest()[:16]
    # Lever/Ashby ids are hyphenated UUIDs: kept verbatim.
    return f"{prefijo}-{valor}"


def _oferta(
    *,
    job_id: str,
    titulo: Any,
    empresa: Any,
    url: Any,
    ubicacion: Any = None,
    remoto: str | None = None,
    salario: Any = None,
    contrato: Any = None,
    descripcion: Any = None,
    fecha: Any = None,
    source: str,
) -> dict[str, Any] | None:
    """Build the normalized contract dict.

    Returns:
        The dict, or ``None`` when the essential fields (title, url) are missing.
    """
    titulo_limpio = _limpio(titulo)
    url_limpia = _limpio(url)
    if not titulo_limpio or not url_limpia:
        return None

    return {
        "job_id": job_id,
        "titulo": titulo_limpio,
        "empresa": _limpio(empresa),
        "url": url_limpia,
        "ubicacion": _limpio(ubicacion),
        "remoto": remoto,
        "salario": _limpio(salario),
        "contrato": contrato,
        "descripcion": _texto_plano(descripcion),
        "fecha_publicacion": _fecha_publicacion(fecha),
        "source": source,
    }


# --------------------------------------------------------------------------- #
# Source: Remotive
# --------------------------------------------------------------------------- #


def _remotive_una_llamada(parametros: dict[str, Any], limite: int) -> list[dict[str, Any]]:
    """Run one Remotive query and return normalized postings.

    Remotive's legal note: the source is credited in ``source`` and its URL is
    always linked (it forwards to the original posting).

    Args:
        parametros: Query parameters (``category`` and/or ``search``).
        limite: Maximum results requested.

    Returns:
        Normalized postings passing the title filter.
    """
    consulta = dict(parametros)
    consulta["limit"] = limite
    url = URL_REMOTIVE + "?" + urllib.parse.urlencode(consulta)
    datos = _json(url)
    trabajos = datos.get("jobs") if isinstance(datos, dict) else None
    if not isinstance(trabajos, list):
        return []

    ofertas: list[dict[str, Any]] = []
    for trabajo in trabajos:
        if not isinstance(trabajo, dict):
            continue
        titulo = trabajo.get("title")
        if not _titulo_relevante(titulo):
            continue
        # Remotive only lists remote jobs: the workplace type is a fact.
        oferta = _oferta(
            job_id=_job_id("remotive", trabajo.get("id"), str(trabajo.get("url") or titulo)),
            titulo=titulo,
            empresa=trabajo.get("company_name"),
            url=trabajo.get("url"),
            ubicacion=trabajo.get("candidate_required_location"),
            remoto="Remoto",
            salario=trabajo.get("salary"),
            contrato=_clasificar(trabajo.get("job_type"), _CONTRATOS),
            descripcion=trabajo.get("description"),
            fecha=trabajo.get("publication_date"),
            source="remotive",
        )
        if oferta:
            ofertas.append(oferta)
    return ofertas


def buscar_remotive(
    limite: int = 100,
    terminos: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Design postings on Remotive (design category + specific searches).

    ``terminos`` (ROLES + TOPICS from the config) replaces the fixed
    ``BUSQUEDAS_REMOTIVE`` list. Text searches are capped at 3 to respect the
    request budget Remotive asks for; the ``design`` category always runs.

    Args:
        limite: Maximum postings to return.
        terminos: Optional search terms overriding the defaults.

    Returns:
        Deduplicated postings by ``job_id``.
    """
    buscados = [str(t).strip() for t in (terminos or BUSQUEDAS_REMOTIVE) if str(t).strip()][:3]
    if not buscados:
        log.info("Remotive omitido: sin términos de búsqueda configurados (ROLES/TOPICS).")
        return []
    consultas: list[dict[str, Any]] = [{"category": "design"}]
    consultas.extend({"search": termino} for termino in buscados)

    encontradas: dict[str, dict[str, Any]] = {}
    for parametros in consultas:
        try:
            for oferta in _remotive_una_llamada(parametros, limite):
                encontradas.setdefault(oferta["job_id"], oferta)
        except Exception as err:  # noqa: BLE001 — one query must not kill the source
            log.warning("Remotive %s falló: %s", parametros, err)

    ofertas = list(encontradas.values())[:limite]
    log.info("Remotive: %s ofertas de diseño.", len(ofertas))
    return ofertas


# --------------------------------------------------------------------------- #
# Source: We Work Remotely (RSS)
# --------------------------------------------------------------------------- #


def _wwr_empresa_y_titulo(titulo: str) -> tuple[str | None, str]:
    """Split 'Company: Role' from the RSS title (WWR uses that format).

    Returns:
        ``(company, title)``; company is ``None`` when the format does not match.
    """
    titulo = (titulo or "").strip()
    if ":" in titulo:
        empresa, _, resto = titulo.partition(":")
        empresa, resto = empresa.strip(), resto.strip()
        if empresa and resto and len(empresa) <= 60:
            return empresa, resto
    return None, titulo


def buscar_wwr(limite: int = 100) -> list[dict[str, Any]]:
    """Design postings from the We Work Remotely RSS feed.

    Args:
        limite: Maximum postings to return.

    Returns:
        Normalized postings; ``[]`` if the feed cannot be fetched or parsed.
    """
    try:
        crudo = _descargar(URL_WWR_DESIGN)
        raiz = ET.fromstring(crudo)
    except Exception as err:  # noqa: BLE001
        log.warning("WWR RSS falló: %s", err)
        return []

    ofertas: list[dict[str, Any]] = []
    # The RSS feed uses no namespaces: searching 'item' at any level is enough.
    for item in raiz.iter("item"):
        titulo_bruto = (item.findtext("title") or "").strip()
        enlace = (item.findtext("link") or "").strip()
        if not titulo_bruto or not enlace:
            continue
        empresa, titulo = _wwr_empresa_y_titulo(titulo_bruto)
        if not _titulo_relevante(titulo):
            continue
        descripcion = item.findtext("description") or ""
        region = _texto_plano(item.findtext("region"), maximo=200)
        oferta = _oferta(
            job_id=_job_id("wwr", "", respaldo=enlace),
            titulo=titulo,
            empresa=empresa,
            url=enlace,
            ubicacion=region,
            # WWR only lists remote jobs.
            remoto="Remoto",
            salario=None,
            contrato=None,
            descripcion=descripcion,
            fecha=item.findtext("pubDate"),
            source="weworkremotely",
        )
        if oferta:
            ofertas.append(oferta)
        if len(ofertas) >= limite:
            break

    log.info("WWR: %s ofertas de diseño.", len(ofertas))
    return ofertas


# --------------------------------------------------------------------------- #
# Source: Ashby
# --------------------------------------------------------------------------- #


def buscar_ashby(slug: str, limite: int = 100) -> list[dict[str, Any]]:
    """Design postings from Ashby's public board for ``slug``.

    Ashby returns everything in the list call: description, workplace type and
    compensation summary.

    Args:
        slug: Board slug.
        limite: Maximum postings to return.

    Returns:
        Normalized postings.

    Raises:
        ValueError: When the response has no ``jobs`` list.
    """
    datos = _json(URL_ASHBY.format(slug=urllib.parse.quote(slug)))
    trabajos = datos.get("jobs") if isinstance(datos, dict) else None
    if not isinstance(trabajos, list):
        raise ValueError("respuesta sin 'jobs'")

    empresa = _empresa_desde_slug(slug)
    ofertas: list[dict[str, Any]] = []
    for trabajo in trabajos:
        if not isinstance(trabajo, dict):
            continue
        titulo = trabajo.get("title")
        if not _titulo_relevante(titulo):
            continue

        compensacion = trabajo.get("compensation") or {}
        salario = None
        if isinstance(compensacion, dict):
            salario = compensacion.get("compensationTierSummary") or compensacion.get(
                "scrapeableCompensationSalarySummary"
            )

        # workplaceType: "Remote" | "Hybrid" | "OnSite". isRemote is a fallback.
        remoto = _normalizar_remoto(
            trabajo.get("workplaceType"),
            "Remote" if trabajo.get("isRemote") is True else None,
            trabajo.get("location"),
        )

        oferta = _oferta(
            job_id=_job_id("ashby", trabajo.get("id"), str(trabajo.get("jobUrl") or titulo)),
            titulo=titulo,
            empresa=empresa,
            url=trabajo.get("jobUrl") or trabajo.get("applyUrl"),
            ubicacion=trabajo.get("location"),
            remoto=remoto,
            salario=salario,
            contrato=_clasificar(trabajo.get("employmentType"), _CONTRATOS),
            descripcion=trabajo.get("descriptionPlain") or trabajo.get("descriptionHtml"),
            fecha=trabajo.get("publishedAt") or trabajo.get("updatedAt"),
            source="ashby",
        )
        if oferta:
            ofertas.append(oferta)
        if len(ofertas) >= limite:
            break
    return ofertas


# --------------------------------------------------------------------------- #
# Source: Greenhouse
# --------------------------------------------------------------------------- #


def _importe(valor: Any) -> float | None:
    """Convert a Greenhouse amount (str/num) to float when possible."""
    if valor in (None, ""):
        return None
    try:
        return float(str(valor).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _formatear_rango(valor: dict[str, Any]) -> str | None:
    """Format a Greenhouse ``currency_range`` as text.

    Only yearly ranges are published: an hourly range (40-60 USD) would be read
    by ``scoring.py`` as an annual salary and the posting would be dropped for
    "salario bajo". Non-yearly ranges return ``None`` (nothing is invented nor
    annualized).

    Args:
        valor: ``currency_range`` block (``min_value``/``max_value``/``unit``).

    Returns:
        Formatted range such as ``"USD 60.000 – 80.000"``, or ``None``.
    """
    minimo = _importe(valor.get("min_value"))
    maximo = _importe(valor.get("max_value"))
    unidad = str(valor.get("unit") or "").strip()

    if minimo is None and maximo is None:
        return None
    if minimo is not None and minimo < 10000:
        log.debug("Rango salarial no anual ignorado: %s", valor)
        return None

    def _fmt(cifra: float | None) -> str:
        return f"{cifra:,.0f}".replace(",", ".")

    if minimo is not None and maximo is not None:
        texto = f"{_fmt(minimo)} – {_fmt(maximo)}"
    else:
        texto = _fmt(minimo if minimo is not None else maximo)
    return (f"{unidad} {texto}").strip()


def _greenhouse_salario(metadata: Any) -> str | None:
    """Look for a published salary in Greenhouse's ``metadata`` block."""
    if not isinstance(metadata, list):
        return None
    for entrada in metadata:
        if not isinstance(entrada, dict):
            continue
        nombre = _sin_acentos(str(entrada.get("name") or ""))
        if not any(clave in nombre for clave in ("salary", "compensation", "salario", "pay range")):
            continue
        valor = entrada.get("value")
        if isinstance(valor, dict):
            texto = _formatear_rango(valor)
        else:
            texto = _limpio(valor)
        if texto:
            return texto
    return None


def _greenhouse_remoto(metadata: Any, ubicacion: str | None) -> str | None:
    """Infer the workplace type of a Greenhouse posting.

    Many boards declare remote openness in ``metadata``
    (``"Position open to remote": true``); otherwise the location is checked.

    Args:
        metadata: Board metadata list.
        ubicacion: Posting location.

    Returns:
        ``Remoto`` / ``Híbrido`` / ``Presencial``, or ``None``.
    """
    if isinstance(metadata, list):
        for entrada in metadata:
            if not isinstance(entrada, dict):
                continue
            nombre = _sin_acentos(str(entrada.get("name") or ""))
            if "remote" in nombre or "remoto" in nombre:
                valor = entrada.get("value")
                if valor is True or str(valor).strip().lower() in ("true", "yes", "sí"):
                    return "Remoto"
    return _normalizar_remoto(ubicacion)


def buscar_greenhouse(slug: str, limite: int = 100) -> list[dict[str, Any]]:
    """Design postings from Greenhouse's public board for ``slug``.

    The list carries no description: the detail is requested only for postings
    passing the title filter (fetching it for all 700 of a large board would be
    wasteful). If a detail fails, that posting keeps no description instead of
    being lost.

    Args:
        slug: Board slug.
        limite: Maximum postings to return.

    Returns:
        Normalized postings.

    Raises:
        ValueError: When the response has no ``jobs`` list.
    """
    datos = _json(URL_GREENHOUSE_LISTA.format(slug=urllib.parse.quote(slug)))
    trabajos = datos.get("jobs") if isinstance(datos, dict) else None
    if not isinstance(trabajos, list):
        raise ValueError("respuesta sin 'jobs'")

    candidatas = [t for t in trabajos if isinstance(t, dict) and _titulo_relevante(t.get("title"))][
        :limite
    ]
    if not candidatas:
        return []

    empresa = None
    ofertas: list[dict[str, Any]] = []
    for trabajo in candidatas:
        ubicacion = trabajo.get("location")
        if isinstance(ubicacion, dict):
            ubicacion = ubicacion.get("name")

        descripcion = None
        detalle: dict[str, Any] = {}
        try:
            detalle = _json(
                URL_GREENHOUSE_DETALLE.format(
                    slug=urllib.parse.quote(slug), job_id=trabajo.get("id")
                )
            )
            if not isinstance(detalle, dict):
                detalle = {}
            descripcion = detalle.get("content")
        except Exception as err:  # noqa: BLE001 — carry on without the detail
            log.debug("Greenhouse %s/%s sin detalle: %s", slug, trabajo.get("id"), err)

        if not empresa:
            empresa = _limpio(detalle.get("company_name")) or _empresa_desde_slug(slug)

        oferta = _oferta(
            job_id=_job_id("gh", trabajo.get("id"), str(trabajo.get("absolute_url") or "")),
            titulo=trabajo.get("title"),
            empresa=empresa,
            url=trabajo.get("absolute_url") or detalle.get("absolute_url"),
            ubicacion=ubicacion,
            remoto=_greenhouse_remoto(trabajo.get("metadata"), ubicacion),
            salario=_greenhouse_salario(trabajo.get("metadata")),
            contrato=None,
            descripcion=descripcion,
            fecha=trabajo.get("updated_at") or detalle.get("updated_at"),
            source="greenhouse",
        )
        if oferta:
            ofertas.append(oferta)
    return ofertas


# --------------------------------------------------------------------------- #
# Source: Lever
# --------------------------------------------------------------------------- #


def buscar_lever(slug: str, limite: int = 100) -> list[dict[str, Any]]:
    """Design postings from Lever's public board for ``slug``.

    Lever returns a flat array with the description already included.

    Args:
        slug: Board slug.
        limite: Maximum postings to return.

    Returns:
        Normalized postings.

    Raises:
        ValueError: When the response is not a list.
    """
    datos = _json(URL_LEVER.format(slug=urllib.parse.quote(slug)))
    if not isinstance(datos, list):
        raise ValueError("respuesta inesperada (se esperaba una lista)")

    empresa = _empresa_desde_slug(slug)
    ofertas: list[dict[str, Any]] = []
    for trabajo in datos:
        if not isinstance(trabajo, dict):
            continue
        titulo = trabajo.get("text")
        if not _titulo_relevante(titulo):
            continue

        categorias = trabajo.get("categories") or {}
        if not isinstance(categorias, dict):
            categorias = {}
        ubicacion = categorias.get("location")

        oferta = _oferta(
            job_id=_job_id("lever", trabajo.get("id"), str(trabajo.get("hostedUrl") or titulo)),
            titulo=titulo,
            empresa=empresa,
            url=trabajo.get("hostedUrl") or trabajo.get("applyUrl"),
            ubicacion=ubicacion,
            remoto=_normalizar_remoto(trabajo.get("workplaceType"), ubicacion),
            salario=None,
            contrato=_clasificar(categorias.get("commitment"), _CONTRATOS),
            descripcion=trabajo.get("descriptionPlain")
            or trabajo.get("description")
            or trabajo.get("descriptionBody"),
            fecha=trabajo.get("createdAt"),
            source="lever",
        )
        if oferta:
            ofertas.append(oferta)
        if len(ofertas) >= limite:
            break
    return ofertas


# --------------------------------------------------------------------------- #
# Age filter (mirror of the one in main.py)
# --------------------------------------------------------------------------- #


def _dias_maximos_antiguedad() -> int:
    """Maximum age in days, read from ``MAX_ANTIGUEDAD_DIAS``.

    Exact replica of ``main._max_antiguedad_dias`` (same variable, same
    default). Duplicated on purpose: ``boards.py`` cannot import ``main``
    (``main`` imports this module) and the new sources drop at the origin what
    ``main`` would drop again. A value that is not a positive integer falls back
    to the default instead of aborting the run. The editable ``settings.json``
    value arrives via ``configurar_antiguedad`` and takes precedence over the
    environment.

    Returns:
        The effective maximum age in days.
    """
    if _DIAS_ANTIGUEDAD_CONFIGURADOS:
        return _DIAS_ANTIGUEDAD_CONFIGURADOS
    crudo = (os.environ.get(VARIABLE_MAX_ANTIGUEDAD) or "").strip()
    if not crudo:
        return MAX_ANTIGUEDAD_DIAS
    try:
        dias = int(crudo)
    except ValueError:
        log.warning(
            "%s=%r no es un entero; se usan %s días.",
            VARIABLE_MAX_ANTIGUEDAD,
            crudo,
            MAX_ANTIGUEDAD_DIAS,
        )
        return MAX_ANTIGUEDAD_DIAS
    if dias <= 0:
        return MAX_ANTIGUEDAD_DIAS
    return dias


def _reciente(oferta: dict[str, Any], dias: int | None = None) -> bool:
    """True if the posting is not older than the maximum age.

    Postings without ``fecha_publicacion`` (or with an unreadable date) are
    kept: same criterion as ``main._filtrar_por_antiguedad`` (a missing value is
    not penalized). ``fecha_publicacion`` already arrives as ``YYYY-MM-DD``.

    Args:
        oferta: Normalized posting.
        dias: Override for the maximum age; defaults to
            ``_dias_maximos_antiguedad``.

    Returns:
        Whether the posting is recent enough.
    """
    texto = oferta.get("fecha_publicacion")
    if not texto:
        return True
    try:
        fecha = datetime.date.fromisoformat(str(texto)[:10])
    except ValueError:
        return True
    limite = datetime.date.today() - datetime.timedelta(
        days=_dias_maximos_antiguedad() if dias is None else dias
    )
    return fecha >= limite


# --------------------------------------------------------------------------- #
# Source: Recruitee
# --------------------------------------------------------------------------- #


def _salario_recruitee(salario: Any) -> str | None:
    """Format Recruitee's ``salary`` block (almost always empty)."""
    if not isinstance(salario, dict):
        return None
    minimo = _importe(salario.get("min"))
    maximo = _importe(salario.get("max"))
    if minimo is None and maximo is None:
        return None
    moneda = _limpio(salario.get("currency")) or ""
    periodo = _limpio(salario.get("period")) or ""

    def _fmt(cifra: float) -> str:
        if float(cifra).is_integer():
            return f"{cifra:,.0f}".replace(",", ".")
        return f"{cifra:,.2f}".replace(",", ".")

    if minimo is not None and maximo is not None:
        texto = f"{_fmt(minimo)} – {_fmt(maximo)}"
    elif minimo is not None:
        texto = _fmt(minimo)
    elif maximo is not None:
        texto = _fmt(maximo)
    else:  # pragma: no cover — already excluded by the early return above
        return None
    return " ".join(parte for parte in (moneda, texto, periodo) if parte)


def _remoto_recruitee(trabajo: dict[str, Any], ubicacion: str | None) -> str | None:
    """Translate Recruitee's ``remote``/``hybrid``/``on_site`` booleans."""
    if trabajo.get("remote") is True:
        return "Remoto"
    if trabajo.get("hybrid") is True:
        return "Híbrido"
    if trabajo.get("on_site") is True:
        return "Presencial"
    return _normalizar_remoto(ubicacion)


def buscar_recruitee(slug: str, limite: int = 100) -> list[dict[str, Any]]:
    """Design postings from Recruitee's public board for ``slug``.

    ``GET https://{slug}.recruitee.com/api/offers/`` returns ``{"offers": […]}``
    with the HTML description included.

    Args:
        slug: Board slug (company subdomain).
        limite: Maximum postings to return.

    Returns:
        Recent normalized postings.

    Raises:
        ValueError: When the response has no ``offers`` list.
    """
    datos = _json(URL_RECRUITEE.format(slug=urllib.parse.quote(slug)))
    ofertas_crudas = datos.get("offers") if isinstance(datos, dict) else None
    if not isinstance(ofertas_crudas, list):
        raise ValueError("respuesta sin 'offers'")

    empresa = _empresa_desde_slug(slug)
    ofertas: list[dict[str, Any]] = []
    for trabajo in ofertas_crudas:
        if not isinstance(trabajo, dict):
            continue
        titulo = trabajo.get("title")
        if not _titulo_relevante(titulo):
            continue

        ubicacion = (
            _limpio(trabajo.get("location"))
            or ", ".join(
                parte
                for parte in (
                    _limpio(trabajo.get("city")),
                    _limpio(trabajo.get("country")),
                )
                if parte
            )
            or None
        )

        oferta = _oferta(
            job_id=_job_id(
                "recruitee", trabajo.get("id"), str(trabajo.get("careers_url") or titulo)
            ),
            titulo=titulo,
            empresa=_limpio(trabajo.get("company_name")) or empresa,
            url=trabajo.get("careers_url") or trabajo.get("careers_apply_url"),
            ubicacion=ubicacion,
            remoto=_remoto_recruitee(trabajo, ubicacion),
            salario=_salario_recruitee(trabajo.get("salary")),
            contrato=_clasificar(
                str(trabajo.get("employment_type_code") or "").replace("_", " "),
                _CONTRATOS,
            ),
            descripcion=trabajo.get("description"),
            fecha=trabajo.get("published_at") or trabajo.get("created_at"),
            source="recruitee",
        )
        if oferta and _reciente(oferta):
            ofertas.append(oferta)
        if len(ofertas) >= limite:
            break
    return ofertas


def buscar_recruitee_empresas(
    limite: int = 100,
    empresas: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Walk the Recruitee boards in parallel and aggregate their postings.

    Args:
        limite: Maximum postings to return.
        empresas: Slugs to scan; defaults to ``RECRUITEE_EMPRESAS``.

    Returns:
        Aggregated postings; boards that fail are skipped.
    """
    lista = [e for e in (empresas if empresas is not None else RECRUITEE_EMPRESAS) if e]
    if not lista:
        return []

    ofertas: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=MAX_HILOS) as pool:
        futuros = {slug: pool.submit(buscar_recruitee, slug, limite) for slug in lista}
        for slug, futuro in futuros.items():
            try:
                ofertas.extend(futuro.result())
            except Exception as err:  # noqa: BLE001 — one board must not kill the source
                log.debug("Recruitee %s no disponible: %s", slug, err)

    log.info("Recruitee: %s ofertas de diseño (%s boards).", len(ofertas), len(lista))
    return ofertas[:limite]


# --------------------------------------------------------------------------- #
# Source: Workable
# --------------------------------------------------------------------------- #


def buscar_workable(slug: str, limite: int = 100) -> list[dict[str, Any]]:
    """Design postings from Workable's public board for ``slug``.

    Uses the **public widget** endpoint (``apply.workable.com/api/v1/
    widget/accounts/{slug}?details=true``), which needs no key: it returns
    ``{"name", "description", "jobs": […]}`` with the HTML description included.
    The management API (``spi/v3``) requires a token and is not used.

    Args:
        slug: Board slug.
        limite: Maximum postings to return.

    Returns:
        Recent normalized postings.

    Raises:
        ValueError: When the response is not a dict or has no ``jobs`` list.
    """
    datos = _json(URL_WORKABLE.format(slug=urllib.parse.quote(slug)))
    if not isinstance(datos, dict):
        raise ValueError("respuesta inesperada")
    trabajos = datos.get("jobs")
    if not isinstance(trabajos, list):
        raise ValueError("respuesta sin 'jobs'")

    empresa = _limpio(datos.get("name")) or _empresa_desde_slug(slug)
    ofertas: list[dict[str, Any]] = []
    for trabajo in trabajos:
        if not isinstance(trabajo, dict):
            continue
        titulo = trabajo.get("title")
        if not _titulo_relevante(titulo):
            continue

        ubicacion = (
            ", ".join(
                parte
                for parte in (
                    _limpio(trabajo.get("city")),
                    _limpio(trabajo.get("state")),
                    _limpio(trabajo.get("country")),
                )
                if parte
            )
            or None
        )

        oferta = _oferta(
            job_id=_job_id("workable", trabajo.get("shortcode"), str(trabajo.get("url") or titulo)),
            titulo=titulo,
            empresa=empresa,
            url=trabajo.get("url") or trabajo.get("shortlink") or trabajo.get("application_url"),
            ubicacion=ubicacion,
            remoto=(
                "Remoto" if trabajo.get("telecommuting") is True else _normalizar_remoto(ubicacion)
            ),
            salario=None,  # the public widget publishes no salary
            contrato=_clasificar(trabajo.get("employment_type"), _CONTRATOS),
            descripcion=trabajo.get("description"),
            fecha=trabajo.get("published_on") or trabajo.get("created_at"),
            source="workable",
        )
        if oferta and _reciente(oferta):
            ofertas.append(oferta)
        if len(ofertas) >= limite:
            break
    return ofertas


def buscar_workable_empresas(
    limite: int = 100,
    empresas: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Walk the Workable boards in parallel and aggregate their postings.

    Args:
        limite: Maximum postings to return.
        empresas: Slugs to scan; defaults to ``WORKABLE_EMPRESAS``.

    Returns:
        Aggregated postings; boards that fail are skipped.
    """
    lista = [e for e in (empresas if empresas is not None else WORKABLE_EMPRESAS) if e]
    if not lista:
        return []

    ofertas: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=MAX_HILOS) as pool:
        futuros = {slug: pool.submit(buscar_workable, slug, limite) for slug in lista}
        for slug, futuro in futuros.items():
            try:
                ofertas.extend(futuro.result())
            except Exception as err:  # noqa: BLE001
                log.debug("Workable %s no disponible: %s", slug, err)

    log.info("Workable: %s ofertas de diseño (%s boards).", len(ofertas), len(lista))
    return ofertas[:limite]


# --------------------------------------------------------------------------- #
# Source: BambooHR
# --------------------------------------------------------------------------- #


def _ubicacion_bamboohr(*bloques: Any) -> str | None:
    """Compose a readable location from BambooHR's blocks.

    Args:
        *bloques: Candidate location dicts, tried in order.

    Returns:
        Comma-joined location, or ``None`` when no block carries one.
    """
    for bloque in bloques:
        if not isinstance(bloque, dict):
            continue
        partes = [
            _limpio(bloque.get(clave))
            for clave in ("city", "state", "province", "addressCountry", "country")
        ]
        texto = ", ".join(p for p in partes if p)
        if texto:
            return texto
    return None


def buscar_bamboohr(slug: str, limite: int = 100) -> list[dict[str, Any]]:
    """Design postings from BambooHR's public board for ``slug``.

    The index (``/careers/list``) carries no description, date or salary: the
    detail (``/careers/{id}/detail``) is requested only for postings passing the
    title filter. If a detail fails, the posting is kept without description or
    date instead of being lost.

    Args:
        slug: Board slug (company subdomain).
        limite: Maximum postings to return.

    Returns:
        Recent normalized postings.

    Raises:
        ValueError: When the response has no ``result`` list.
    """
    datos = _json(URL_BAMBOOHR_LISTA.format(slug=urllib.parse.quote(slug)))
    trabajos = datos.get("result") if isinstance(datos, dict) else None
    if not isinstance(trabajos, list):
        raise ValueError("respuesta sin 'result'")

    empresa = _empresa_desde_slug(slug)
    ofertas: list[dict[str, Any]] = []
    for trabajo in trabajos:
        if not isinstance(trabajo, dict):
            continue
        titulo = trabajo.get("jobOpeningName")
        if not _titulo_relevante(titulo):
            continue

        detalle: dict[str, Any] = {}
        try:
            crudo = _json(
                URL_BAMBOOHR_DETALLE.format(slug=urllib.parse.quote(slug), job_id=trabajo.get("id"))
            )
            if isinstance(crudo, dict):
                resultado = crudo.get("result")
                if isinstance(resultado, dict) and isinstance(resultado.get("jobOpening"), dict):
                    detalle = resultado["jobOpening"]
        except Exception as err:  # noqa: BLE001 — carry on without the detail
            log.debug("BambooHR %s/%s sin detalle: %s", slug, trabajo.get("id"), err)

        ubicacion = _ubicacion_bamboohr(
            detalle.get("location"),
            detalle.get("atsLocation"),
            trabajo.get("location"),
            trabajo.get("atsLocation"),
        )
        es_remoto = detalle.get("isRemote")
        if es_remoto is None:
            es_remoto = trabajo.get("isRemote")

        oferta = _oferta(
            job_id=_job_id(
                "bamboohr",
                "{}-{}".format(slug, trabajo.get("id")),
                str(trabajo.get("jobOpeningShareUrl") or titulo),
            ),
            titulo=titulo,
            empresa=empresa,
            url=detalle.get("jobOpeningShareUrl")
            or "https://{}.bamboohr.com/careers/{}".format(slug, trabajo.get("id")),
            ubicacion=ubicacion,
            remoto="Remoto" if es_remoto is True else _normalizar_remoto(ubicacion),
            salario=detalle.get("compensation"),
            contrato=_clasificar(
                detalle.get("employmentStatusLabel") or trabajo.get("employmentStatusLabel"),
                _CONTRATOS,
            ),
            descripcion=detalle.get("description"),
            fecha=detalle.get("datePosted"),
            source="bamboohr",
        )
        if oferta and _reciente(oferta):
            ofertas.append(oferta)
        if len(ofertas) >= limite:
            break
    return ofertas


def buscar_bamboohr_empresas(
    limite: int = 100,
    empresas: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Walk the BambooHR boards in parallel and aggregate their postings.

    Args:
        limite: Maximum postings to return.
        empresas: Slugs to scan; defaults to ``BAMBOOHR_EMPRESAS``.

    Returns:
        Aggregated postings; boards that fail are skipped.
    """
    lista = [e for e in (empresas if empresas is not None else BAMBOOHR_EMPRESAS) if e]
    if not lista:
        return []

    ofertas: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=MAX_HILOS) as pool:
        futuros = {slug: pool.submit(buscar_bamboohr, slug, limite) for slug in lista}
        for slug, futuro in futuros.items():
            try:
                ofertas.extend(futuro.result())
            except Exception as err:  # noqa: BLE001
                log.debug("BambooHR %s no disponible: %s", slug, err)

    log.info("BambooHR: %s ofertas de diseño (%s boards).", len(ofertas), len(lista))
    return ofertas[:limite]


# --------------------------------------------------------------------------- #
# Source: EURES (public search API, no key)
# --------------------------------------------------------------------------- #


def _cuerpo_eures(termino: str, resultados: int) -> dict[str, Any]:
    """Build the EURES search POST body.

    All filters are empty except the term and the language; the design filter is
    applied locally.

    Args:
        termino: Keyword searched in titles.
        resultados: Results per page.

    Returns:
        The request payload.
    """
    return {
        "resultsPerPage": resultados,
        "page": 1,
        "sortSearch": "MOST_RECENT",
        "keywords": [{"keyword": termino, "specificSearchCode": "TITLE"}],
        "publicationPeriod": None,
        "occupationUris": [],
        "skillUris": [],
        "requiredExperienceCodes": [],
        "positionScheduleCodes": [],
        "sectorCodes": [],
        "educationAndQualificationLevelCodes": [],
        "positionOfferingCodes": [],
        "locationCodes": [],
        "euresFlagCodes": [],
        "otherBenefitsCodes": [],
        "requiredLanguages": [],
        "minNumberPost": None,
        "sessionId": "jobautomat",
        "requestLanguage": "en",
    }


def _ubicacion_eures(trabajo: dict[str, Any]) -> str | None:
    """Countries of the posting, taken from ``locationMap`` (ISO country codes)."""
    mapa = trabajo.get("locationMap")
    if isinstance(mapa, dict) and mapa:
        return ", ".join(sorted(str(clave) for clave in mapa))
    return None


# EURES prefixes the description with a front-matter-like metadata block
# ("---base: /meta-keywords:… title: …---"): noise, not part of the posting.
_RE_EURES_FRONTMATTER = re.compile(r"\A\s*---.*?---\s*", re.DOTALL)


def _descripcion_eures(valor: Any) -> str | None:
    """Strip the front-matter EURES puts before the description."""
    if not valor:
        return None
    return _RE_EURES_FRONTMATTER.sub("", str(valor), count=1)


def buscar_eures(
    limite: int = 100,
    terminos: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Design postings from the EURES public search API.

    EURES requires **no** key or registration to search: it is a JSON POST to
    ``/jv-searchengine/public/jv-search/search``. It returns
    ``{"numberRecords", "jvs": […], "facets": …}``; each vacancy carries ``id``
    (base64, used for the public URL), ``title``, ``description``,
    ``creationDate`` as epoch ms and ``employer.name``. The city is not included
    in the search results, only the country (``locationMap``), so the location
    stays at the country code instead of inventing a city.

    Args:
        limite: Maximum postings to return.
        terminos: Optional search terms overriding ``BUSQUEDAS_EURES``.

    Returns:
        Recent normalized postings, deduplicated by EURES id.
    """
    encontradas: dict[str, dict[str, Any]] = {}
    # EURES rejects (400) more than 50 results per page.
    por_pagina = max(1, min(limite, 50))
    buscados = [str(t).strip() for t in (terminos or BUSQUEDAS_EURES) if str(t).strip()][:3]
    for termino in buscados:
        try:
            datos = _json_post(URL_EURES_BUSQUEDA, _cuerpo_eures(termino, por_pagina))
        except Exception as err:  # noqa: BLE001 — one query must not kill the source
            log.warning("EURES «%s» falló: %s", termino, err)
            continue
        vacantes = datos.get("jvs") if isinstance(datos, dict) else None
        if not isinstance(vacantes, list):
            continue
        for trabajo in vacantes:
            if not isinstance(trabajo, dict):
                continue
            identificador = _limpio(trabajo.get("id"))
            if not identificador or identificador in encontradas:
                continue
            titulo = trabajo.get("title")
            if not _titulo_relevante(titulo):
                continue

            empleador = trabajo.get("employer")
            empresa = _limpio(empleador.get("name")) if isinstance(empleador, dict) else None

            oferta = _oferta(
                job_id=_job_id("eures", identificador, str(titulo)),
                titulo=titulo,
                empresa=empresa,
                url=URL_EURES_DETALLE.format(job_id=urllib.parse.quote(identificador)),
                ubicacion=_ubicacion_eures(trabajo),
                remoto=None,
                salario=None,  # the search API publishes no salary
                contrato=_clasificar(
                    " ".join(
                        [str(trabajo.get("positionOfferingCode") or "")]
                        + [str(c) for c in (trabajo.get("positionScheduleCodes") or [])]
                    ),
                    _CONTRATOS,
                ),
                descripcion=_descripcion_eures(trabajo.get("description")),
                fecha=trabajo.get("creationDate"),
                source="eures",
            )
            if oferta and _reciente(oferta):
                encontradas[identificador] = oferta

    ofertas = list(encontradas.values())[:limite]
    log.info("EURES: %s ofertas de diseño.", len(ofertas))
    return ofertas


# --------------------------------------------------------------------------- #
# Source: InfoJobs (requires a key)
# --------------------------------------------------------------------------- #


def buscar_infojobs(
    limite: int = 100,
    consulta: str = "",
) -> list[dict[str, Any]]:
    """Design postings on InfoJobs. **Credentialed source.**

    The InfoJobs API is not open: it requires registering an app at
    ``developer.infojobs.net`` and authenticating with Basic
    (``client_id:client_secret``). Credentials are read from
    ``INFOJOBS_CLIENT_ID`` and ``INFOJOBS_CLIENT_SECRET``. If either is missing,
    the function issues **no request** and returns ``[]`` (with a log notice), so
    the remaining sources are unaffected.

    Without credentials the real response shape could not be verified, so
    parsing is defensive: fields are read by their documented names (``items``,
    ``title``, ``company.name``, ``city``, ``published``,
    ``salaryMin``/``salaryMax``, ``contractType``) and anything missing stays
    ``None``.

    Args:
        limite: Maximum postings to return (capped at 50 by the API).
        consulta: Search term.

    Returns:
        Recent normalized postings; ``[]`` without credentials or on failure.
    """
    cliente = (os.environ.get(VARIABLE_INFOJOBS_ID) or "").strip()
    secreto = (os.environ.get(VARIABLE_INFOJOBS_SECRET) or "").strip()
    consulta = (consulta or "").strip()
    if not consulta:
        log.info("InfoJobs omitido: sin término de búsqueda configurado (ROLES/TOPICS).")
        return []
    if not cliente or not secreto:
        log.info(
            "InfoJobs omitido: faltan %s/%s (su API exige key).",
            VARIABLE_INFOJOBS_ID,
            VARIABLE_INFOJOBS_SECRET,
        )
        return []

    credenciales = base64.b64encode((f"{cliente}:{secreto}").encode()).decode("ascii")
    url = (
        URL_INFOJOBS + "?" + urllib.parse.urlencode({"q": consulta, "maxResults": min(limite, 50)})
    )
    peticion = urllib.request.Request(
        url, headers=dict(CABECERAS, Authorization="Basic " + credenciales)
    )
    try:
        with urllib.request.urlopen(peticion, timeout=TIMEOUT_HTTP) as respuesta:
            datos = json.loads(respuesta.read().decode("utf-8", errors="replace"))
    except Exception as err:  # noqa: BLE001 — no valid key means no postings
        log.warning("InfoJobs falló: %s", err)
        return []

    elementos = datos.get("items") if isinstance(datos, dict) else None
    if not isinstance(elementos, list):
        return []

    ofertas: list[dict[str, Any]] = []
    for elemento in elementos:
        if not isinstance(elemento, dict):
            continue
        titulo = elemento.get("title")
        if not _titulo_relevante(titulo):
            continue
        empresa = elemento.get("company")
        empresa = _limpio(empresa.get("name")) if isinstance(empresa, dict) else _limpio(empresa)
        identificador = _limpio(elemento.get("id"))
        salario = None
        if elemento.get("salaryMin") or elemento.get("salaryMax"):
            salario = "{} – {} {}".format(
                elemento.get("salaryMin") or "",
                elemento.get("salaryMax") or "",
                _limpio(elemento.get("salaryPeriod")) or "",
            )
            salario = " ".join(salario.split())

        oferta = _oferta(
            job_id=_job_id("infojobs", identificador, str(titulo)),
            titulo=titulo,
            empresa=empresa,
            url=elemento.get("link")
            or (f"https://www.infojobs.net/oferta/{identificador}" if identificador else None),
            ubicacion=_limpio(elemento.get("city")),
            remoto=_normalizar_remoto(
                _limpio(elemento.get("teleworking")), _limpio(elemento.get("city"))
            ),
            salario=salario,
            contrato=_clasificar(elemento.get("contractType"), _CONTRATOS),
            descripcion=elemento.get("description"),
            fecha=elemento.get("published"),
            source="infojobs",
        )
        if oferta and _reciente(oferta):
            ofertas.append(oferta)
        if len(ofertas) >= limite:
            break
    return ofertas


# --------------------------------------------------------------------------- #
# ATS auto-detection per company
# --------------------------------------------------------------------------- #

# Probe order: the one answering fastest with fewest requests goes first.
_DETECTORES = (
    ("greenhouse", buscar_greenhouse),
    ("lever", buscar_lever),
    ("ashby", buscar_ashby),
)


def buscar_empresa(slug: str, limite: int = 100) -> tuple[str | None, list[dict[str, Any]]]:
    """Auto-detect the ATS of ``slug`` and return ``(ats, postings)``.

    Greenhouse -> Lever -> Ashby are tried in order and the first one returning
    design postings wins. A failing ATS does not prevent trying the next one.

    Args:
        slug: Company board slug.
        limite: Maximum postings per ATS attempt.

    Returns:
        ``(ats_name, postings)``, or ``(None, [])`` when none works.
    """
    for nombre, buscador in _DETECTORES:
        try:
            ofertas = buscador(slug, limite=limite)
        except Exception as err:  # noqa: BLE001 — try the next ATS
            log.debug("ATS %s/%s no disponible: %s", nombre, slug, err)
            continue
        if ofertas:
            log.info("%s: board %s (%s ofertas de diseño).", nombre, slug, len(ofertas))
            return nombre, ofertas
    return None, []


def buscar_ats(limite: int = 100, slugs: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Walk the companies in parallel and aggregate their design postings.

    Args:
        limite: Maximum postings per company.
        slugs: Slugs to scan; defaults to ``EMPRESAS_SLUGS``.

    Returns:
        Aggregated postings from every detected ATS.
    """
    lista_slugs = [s for s in (slugs if slugs is not None else EMPRESAS_SLUGS) if s]
    if not lista_slugs:
        return []

    ofertas: list[dict[str, Any]] = []
    detectadas: list[str] = []
    with ThreadPoolExecutor(max_workers=MAX_HILOS) as pool:
        futuros = {slug: pool.submit(buscar_empresa, slug, limite) for slug in lista_slugs}
        for slug, futuro in futuros.items():
            try:
                ats, encontradas = futuro.result()
            except Exception as err:  # noqa: BLE001 — never propagates
                log.warning("Empresa %s falló: %s", slug, err)
                continue
            if ats:
                detectadas.append(f"{slug}:{ats}")
                ofertas.extend(encontradas)

    log.info(
        "ATS: %s/%s empresas con ofertas de diseño (%s).",
        len(detectadas),
        len(lista_slugs),
        ", ".join(sorted(detectadas)) or "ninguna",
    )
    return ofertas


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #


def _clave_url(url: str | None) -> str | None:
    """Deduplication key from the URL (no query string, no trailing slash)."""
    if not url:
        return None
    limpia = str(url).split("?")[0].split("#")[0].strip().rstrip("/").lower()
    return limpia or None


def _clave_titulo_empresa(titulo: str | None, empresa: str | None) -> str | None:
    """Deduplication key from normalized title + company."""
    if not titulo:
        return None
    return "{}|{}".format(
        _sin_acentos(str(titulo)).strip(), _sin_acentos(str(empresa or "")).strip()
    )


def _deduplicar(ofertas: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicates by ``job_id``, URL or title+company (in that order).

    Args:
        ofertas: Raw postings from every source.

    Returns:
        Postings with the first occurrence of each key kept.
    """
    vistas: set = set()
    salida: list[dict[str, Any]] = []
    for oferta in ofertas:
        claves = (
            "id:" + str(oferta.get("job_id") or ""),
            "url:" + str(_clave_url(oferta.get("url"))),
            "te:" + str(_clave_titulo_empresa(oferta.get("titulo"), oferta.get("empresa"))),
        )
        if any(clave in vistas for clave in claves):
            continue
        vistas.update(claves)
        salida.append(oferta)
    return salida


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def buscar_ofertas_boards(
    limite_por_fuente: int = 100,
    slugs: Iterable[str] | None = None,
    terminos: Iterable[str] | None = None,
    max_antiguedad_dias: int | None = None,
) -> list[dict[str, Any]]:
    """Aggregate design postings from every public source (no LinkedIn).

    Args:
        limite_por_fuente: Maximum postings each source contributes.
        slugs: Companies to probe; defaults to ``EMPRESAS_SLUGS``.
        terminos: ``ROLES`` + ``TOPICS`` from the editable config. They extend
            the title filter (``_titulo_relevante``) and are used as queries in
            Remotive/EURES/InfoJobs instead of the fixed lists.
        max_antiguedad_dias: Editable maximum age (``settings.json``). It
            replaces the environment value in the sources that filter at the
            origin (Recruitee, Workable, BambooHR, EURES, InfoJobs).

    Returns:
        A deduplicated list of dicts with ``job_id``, ``titulo``, ``empresa``,
        ``url``, ``ubicacion``, ``remoto``, ``salario``, ``contrato``,
        ``descripcion`` and ``source``. Fields that are not published stay
        ``None``. Sources: Remotive, We Work Remotely, auto-detected ATS
        (Greenhouse/Lever/Ashby), Recruitee, Workable, BambooHR, EURES and
        InfoJobs. The last four already drop at the origin the postings older
        than ``MAX_ANTIGUEDAD_DIAS``; ``main`` applies the filter again on the
        merged set. A failing source never raises: each one is isolated, and if
        all fail the result is ``[]``.
    """
    if limite_por_fuente <= 0:
        return []

    # Editable config: set once and used by every source below.
    if max_antiguedad_dias is not None:
        configurar_antiguedad(max_antiguedad_dias)
    terminos_fuente = configurar_terminos(terminos) or None

    ofertas: list[dict[str, Any]] = []

    # Remotive (legal note: the source is credited on every posting).
    try:
        ofertas.extend(buscar_remotive(limite=limite_por_fuente, terminos=terminos_fuente))
    except Exception as err:  # noqa: BLE001
        log.warning("Remotive falló: %s", err)

    # We Work Remotely (RSS).
    try:
        ofertas.extend(buscar_wwr(limite=limite_por_fuente))
    except Exception as err:  # noqa: BLE001
        log.warning("WWR falló: %s", err)

    # Public ATS boards (Greenhouse / Lever / Ashby), auto-detected per company.
    try:
        ofertas.extend(buscar_ats(limite=limite_por_fuente, slugs=slugs))
    except Exception as err:  # noqa: BLE001
        log.warning("ATS fallaron: %s", err)

    # Recruitee: public boards per company subdomain.
    try:
        ofertas.extend(buscar_recruitee_empresas(limite=limite_por_fuente))
    except Exception as err:  # noqa: BLE001
        log.warning("Recruitee falló: %s", err)

    # Workable: public widget endpoint (no key).
    try:
        ofertas.extend(buscar_workable_empresas(limite=limite_por_fuente))
    except Exception as err:  # noqa: BLE001
        log.warning("Workable falló: %s", err)

    # BambooHR: index + detail for each relevant posting.
    try:
        ofertas.extend(buscar_bamboohr_empresas(limite=limite_por_fuente))
    except Exception as err:  # noqa: BLE001
        log.warning("BambooHR falló: %s", err)

    # EURES: public search API (JSON POST, no key).
    try:
        ofertas.extend(buscar_eures(limite=limite_por_fuente, terminos=terminos_fuente))
    except Exception as err:  # noqa: BLE001
        log.warning("EURES falló: %s", err)

    # InfoJobs: returns [] without INFOJOBS_CLIENT_ID/INFOJOBS_CLIENT_SECRET.
    # Its API accepts one term per call: the first one from the config is used.
    try:
        ofertas.extend(
            buscar_infojobs(
                limite=limite_por_fuente,
                consulta=(terminos_fuente or ("",))[0],
            )
        )
    except Exception as err:  # noqa: BLE001
        log.warning("InfoJobs falló: %s", err)

    resultado = _deduplicar(ofertas)
    log.info(
        "Boards: %s ofertas de diseño tras deduplicar (%s antes).",
        len(resultado),
        len(ofertas),
    )
    return resultado


# --------------------------------------------------------------------------- #
# Source diagnostics (self-host)
# --------------------------------------------------------------------------- #

# Short timeout: this is a manual smoke test, not a real run. If a source takes
# longer than this, the diagnostic must say so instead of hanging.
TIMEOUT_PROBAR_FUENTE = 8

# Lightweight endpoints: the same ones the scraper uses but without
# `?details=true` or `includeCompensation=true`, so the probe does not pull a
# large board's full payload.
URL_WORKABLE_WIDGET = "https://apply.workable.com/api/v1/widget/accounts/{slug}"
URL_ASHBY_LIGERO = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def _detalle_error(err: Exception) -> str:
    """Summarize a diagnostic request failure in one line."""
    if isinstance(err, urllib.error.HTTPError):
        razones = {
            "Not Found": "no encontrado",
            "Forbidden": "prohibido",
            "Unauthorized": "no autorizado",
            "Bad Request": "petición incorrecta",
            "Too Many Requests": "demasiadas peticiones",
            "Internal Server Error": "error interno del servidor",
            "Service Unavailable": "servicio no disponible",
            "Gateway Timeout": "timeout de la pasarela",
        }
        razon = razones.get(err.reason or "", err.reason or "")
        return f"HTTP {err.code} {razon}"
    if isinstance(err, urllib.error.URLError):
        return f"error de red: {err.reason}"
    if isinstance(err, TimeoutError):
        return f"timeout tras {TIMEOUT_PROBAR_FUENTE}s"
    return f"{type(err).__name__}: {err}"


def probar_fuente(nombre: str, parametros: dict[str, Any] | None = None) -> dict[str, Any]:
    """Probe a source with **one** real request and return ``{ok, detalle}``.

    Used by the self-host diagnostics: checks that a slug is spelled correctly
    and that the board responds, without running the full scrape.

    Args:
        nombre: Lowercase source name: ``workable``, ``bamboohr``,
            ``recruitee``, ``greenhouse``, ``ashby``, ``lever``, ``remotive`` or
            ``weworkremotely`` (``wwr`` is also accepted).
        parametros: Dict that may carry ``slug`` (Workable, BambooHR,
            Recruitee, Greenhouse, Ashby and Lever need it; the other sources do
            not). BambooHR and Recruitee use the slug as subdomain, as the
            scraper does.

    Returns:
        Always a dict; never raises. ``ok=False`` with
        ``detalle='fuente desconocida'`` when the name is unsupported.
    """
    clave = str(nombre or "").strip().lower()
    params = parametros if isinstance(parametros, dict) else {}
    slug = str(
        params.get("slug") or params.get("empresa") or params.get("subdominio") or ""
    ).strip()

    conocidas = (
        "workable",
        "bamboohr",
        "recruitee",
        "greenhouse",
        "ashby",
        "lever",
        "remotive",
        "weworkremotely",
        "wwr",
    )
    if clave not in conocidas:
        return {"ok": False, "detalle": "fuente desconocida"}

    necesita_slug = clave in (
        "workable",
        "bamboohr",
        "recruitee",
        "greenhouse",
        "ashby",
        "lever",
    )
    if necesita_slug and not slug:
        return {"ok": False, "detalle": "falta 'slug' en parametros"}

    try:
        if clave == "workable":
            url = URL_WORKABLE_WIDGET.format(slug=urllib.parse.quote(slug))
            datos = json.loads(_descargar(url, TIMEOUT_PROBAR_FUENTE).decode("utf-8", "replace"))
            trabajos = datos.get("jobs") if isinstance(datos, dict) else None
            if not isinstance(trabajos, list):
                return {"ok": False, "detalle": "respuesta sin 'jobs'"}
            nombre_board = _limpio(datos.get("name")) or slug
            return {
                "ok": True,
                "detalle": f"workable:{slug} ({nombre_board}) -> {len(trabajos)} ofertas",
            }

        if clave == "bamboohr":
            url = URL_BAMBOOHR_LISTA.format(slug=urllib.parse.quote(slug))
            datos = json.loads(_descargar(url, TIMEOUT_PROBAR_FUENTE).decode("utf-8", "replace"))
            trabajos = datos.get("result") if isinstance(datos, dict) else None
            if not isinstance(trabajos, list):
                return {"ok": False, "detalle": "respuesta sin 'result'"}
            return {
                "ok": True,
                "detalle": f"bamboohr:{slug}.bamboohr.com -> {len(trabajos)} ofertas",
            }

        if clave == "recruitee":
            url = URL_RECRUITEE.format(slug=urllib.parse.quote(slug))
            datos = json.loads(_descargar(url, TIMEOUT_PROBAR_FUENTE).decode("utf-8", "replace"))
            ofertas = datos.get("offers") if isinstance(datos, dict) else None
            if not isinstance(ofertas, list):
                return {"ok": False, "detalle": "respuesta sin 'offers'"}
            return {"ok": True, "detalle": f"recruitee:{slug} -> {len(ofertas)} ofertas"}

        if clave == "greenhouse":
            url = URL_GREENHOUSE_LISTA.format(slug=urllib.parse.quote(slug))
            datos = json.loads(_descargar(url, TIMEOUT_PROBAR_FUENTE).decode("utf-8", "replace"))
            trabajos = datos.get("jobs") if isinstance(datos, dict) else None
            if not isinstance(trabajos, list):
                return {"ok": False, "detalle": "respuesta sin 'jobs'"}
            return {"ok": True, "detalle": f"greenhouse:{slug} -> {len(trabajos)} ofertas"}

        if clave == "ashby":
            url = URL_ASHBY_LIGERO.format(slug=urllib.parse.quote(slug))
            datos = json.loads(_descargar(url, TIMEOUT_PROBAR_FUENTE).decode("utf-8", "replace"))
            trabajos = datos.get("jobs") if isinstance(datos, dict) else None
            if not isinstance(trabajos, list):
                return {"ok": False, "detalle": "respuesta sin 'jobs'"}
            return {"ok": True, "detalle": f"ashby:{slug} -> {len(trabajos)} ofertas"}

        if clave == "lever":
            url = URL_LEVER.format(slug=urllib.parse.quote(slug))
            datos = json.loads(_descargar(url, TIMEOUT_PROBAR_FUENTE).decode("utf-8", "replace"))
            if not isinstance(datos, list):
                return {"ok": False, "detalle": "respuesta inesperada (se esperaba una lista)"}
            return {"ok": True, "detalle": f"lever:{slug} -> {len(datos)} ofertas"}

        if clave == "remotive":
            datos = json.loads(
                _descargar(URL_REMOTIVE + "?limit=1", TIMEOUT_PROBAR_FUENTE).decode(
                    "utf-8", "replace"
                )
            )
            trabajos = datos.get("jobs") if isinstance(datos, dict) else None
            if not isinstance(trabajos, list):
                return {"ok": False, "detalle": "respuesta sin 'jobs'"}
            total = datos.get("job-count") or datos.get("total-job-count")
            return {
                "ok": True,
                "detalle": "remotive -> {} ofertas{}".format(
                    len(trabajos), f" (total {total})" if total else ""
                ),
            }

        # weworkremotely (alias wwr): RSS, counted with ElementTree.
        crudo = _descargar(URL_WWR_DESIGN, TIMEOUT_PROBAR_FUENTE)
        raiz = ET.fromstring(crudo)
        items = sum(1 for _ in raiz.iter("item"))
        return {"ok": True, "detalle": f"weworkremotely -> {items} items en el RSS"}

    except Exception as err:  # noqa: BLE001 — the diagnostic never propagates
        return {"ok": False, "detalle": _detalle_error(err)}


if __name__ == "__main__":  # pragma: no cover — manual utility
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for oferta in buscar_ofertas_boards(limite_por_fuente=5)[:10]:
        print(
            f"{oferta['source']:<22} {(oferta['titulo'] or '')[:45]:<45} "
            f"{(oferta['empresa'] or '')[:22]:<22} {str(oferta['remoto'] or '-'):<12} "
            f"{oferta['salario'] or '-'}"
        )
