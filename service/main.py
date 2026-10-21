"""main.py — Daily orchestrator for the job search service.

Flow (phase 1: generate + notify + enqueue, never apply on its own):

1. Load configuration (``.env``), initialize SQLite and the profile.
2. Search offers on LinkedIn using the ``ROLES`` + ``TOPICS`` terms from the
   editable configuration (``QUERIES`` remains only as a fallback when none
   is configured).
3. Complement with two sessionless sources: public boards/ATS
   (``search.boards``) and Indeed/ZipRecruiter/Glassdoor (``search.jobspy``).
   Every source is optional: a failure is logged and the run continues.
4. For each unseen offer: fetch the detail (LinkedIn only), score it with
   ``scoring`` and, if it reaches ``SCORE_THRESHOLD``, generate cover letter,
   tailored CV and message.
5. Insert the opportunity with status ``para_confirmar`` and send the summary
   to Telegram with approve/reject buttons.

Startup: ``main`` loads the configuration in strict mode (fail-fast: a missing
``DEEPSEEK_API_KEY`` prints the error and exits with code 1) and, at the end
of the run, alerts via Telegram/webhook if the LinkedIn session expired
(``ALERTAR_SESION_LINKEDIN``).

The actual application submission is phase 2 (see ``apply.py``).

Usage:
    python main.py
"""

import argparse
import datetime
import hashlib
import json
import logging
import os
import re
import sys
import threading
import unicodedata

# POSIX file lock so two searches never overlap: the scheduler, a `run once`
# and the panel's "Buscar ahora" button share the same lock. Without fcntl
# (Windows) the service still works, just without a lock.
try:
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX platforms only.
    fcntl = None  # type: ignore[assignment]

# Lock file name inside the database directory.
NOMBRE_LOCK_BUSQUEDA = ".buscar.lock"

# Allow running `python service/main.py` from any cwd.
_DIR_SERVICIO = os.path.dirname(os.path.abspath(__file__))
if _DIR_SERVICIO not in sys.path:
    sys.path.insert(0, _DIR_SERVICIO)

import db  # noqa: E402
import notify  # noqa: E402
import scoring  # noqa: E402
from config import load_config  # noqa: E402

# Fallback search terms; empty means ROLES/TOPICS from settings.json drive the
# search.
QUERIES: list[str] = []

# Maximum results per query (search_jobs contract).
LIMITE_POR_QUERY = 20

# Maximum offers each alternative source (boards) contributes. LinkedIn is
# still the main source, but boards keep the run alive if it falls.
LIMITE_POR_FUENTE_BOARDS = 100

# Maximum results per query **and site** in JobSpy (Indeed/ZipRecruiter/
# Glassdoor). Intentionally low: each query hits three portals and this source
# is complementary, not the main one.
LIMITE_POR_QUERY_JOBSPY = 10

# Maximum age (hours) of the offers requested from JobSpy: the same 72 h the
# rest of the pipeline uses, to avoid reprocessing old offers.
HORAS_RECIENTES_JOBSPY = 72

# Maximum age (days) of any offer before scoring it. Priority:
# ``data/settings.json`` (``MAX_ANTIGUEDAD_DIAS`` or its alias
# ``MAX_ANTIGUEDAD``) > same-name environment variable > default value.
# Offers without ``fecha_publicacion`` are kept: a missing field is not
# penalized.
MAX_ANTIGUEDAD_DIAS = 30
VARIABLE_MAX_ANTIGUEDAD = "MAX_ANTIGUEDAD_DIAS"
CLAVES_CONFIG_ANTIGUEDAD = ("MAX_ANTIGUEDAD_DIAS", "MAX_ANTIGUEDAD")

# LinkedIn cooldown after detecting an anti-bot block. Same as age: editable
# config first, environment as fallback. ``main.py`` applies the value to the
# module with ``search.linkedin.configurar_cooldown``.
#
# NOTE: ``MAX_ANTIGUEDAD_DIAS``/``MAX_ANTIGUEDAD`` and
# ``LINKEDIN_COOLDOWN_SEGUNDOS`` are not in ``config._CONFIG_DEFS`` yet, so
# ``load_config`` drops them (with a warning) when written in
# ``data/settings.json``. To make them editable now, ``_entero_config`` also
# reads the raw JSON via ``config.leer_settings``: once added to the spec, the
# first path (the ``load_config`` dict) will pick them up on its own.
COOLDOWN_SEGUNDOS_DEFECTO = 6 * 3600
VARIABLE_COOLDOWN = "LINKEDIN_COOLDOWN_SEGUNDOS"
CLAVES_CONFIG_COOLDOWN = ("LINKEDIN_COOLDOWN_SEGUNDOS", "LINKEDIN_COOLDOWN")

# Origin of the offers (``source`` field of ``search.boards``).
ORIGEN_LINKEDIN = "linkedin"

# Status of the opportunities awaiting user confirmation.
ESTADO_PARA_CONFIRMAR = "para_confirmar"

# Number of threads scoring offers in parallel. Scoring is an LLM call (the
# bottleneck). 1 keeps a sweep fully serial (lowest CPU/RAM on small hosts);
# raise it only on beefier machines, and never far: DeepSeek limits concurrent
# requests and SQLite tolerates few simultaneous writers.
HILOS_SCORING = 1

# The LinkedIn browser (patchright) is not safe to use from several threads at
# once; this lock serializes detail requests.
_LOCK_DETALLE_LINKEDIN = threading.Lock()

log = logging.getLogger("jobautomat")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _ruta_perfil(config):
    """Resolve the perfil.json path relative to the service directory."""
    ruta = config.get("PERFIL_PATH") or "../data/perfil.json"
    if not os.path.isabs(ruta):
        ruta = os.path.normpath(os.path.join(_DIR_SERVICIO, ruta))
    return ruta


def cargar_perfil(config):
    """Read and return the candidate profile as a dict."""
    ruta = _ruta_perfil(config)
    with open(ruta, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Offer normalization
# ---------------------------------------------------------------------------


def _primer_valor(diccionario, claves, defecto=None):
    """Return the first non-empty value of ``claves`` in ``diccionario``."""
    for clave in claves:
        valor = diccionario.get(clave)
        if valor not in (None, "", [], {}):
            return valor
    return defecto


def _generar_id(job):
    """Return a stable id: offer id, URL or hash of title+company."""
    identificador = _primer_valor(job, ("id", "job_id", "jobId", "urn"))
    if identificador:
        return str(identificador)

    url = _primer_valor(job, ("url", "link", "job_url"))
    if url:
        return str(url)

    base = "{}|{}".format(
        _primer_valor(job, ("titulo", "title"), ""),
        _primer_valor(job, ("empresa", "company"), ""),
    )
    return "h-" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def _normalizar(job):
    """Convert an offer from ``search`` to the table schema.

    Tolerates English and Spanish keys so it is not coupled to the search
    implementation details.
    """
    normalizada = {
        "id": _generar_id(job),
        "url": _primer_valor(job, ("url", "link", "job_url")),
        "titulo": _primer_valor(job, ("titulo", "title", "position")),
        "empresa": _primer_valor(job, ("empresa", "company", "companyName")),
        "salario": _primer_valor(job, ("salario", "salary", "salary_range")),
        "ubicacion": _primer_valor(job, ("ubicacion", "location", "place")),
        "remoto": _primer_valor(job, ("remoto", "remote", "workplace")),
        "contrato": _primer_valor(job, ("contrato", "contract", "employment_type")),
        "descripcion": _primer_valor(job, ("descripcion", "description", "description_text")),
        "fecha_publicacion": _primer_valor(
            job,
            ("fecha_publicacion", "fecha", "date_posted", "publication_date", "published_at"),
        ),
    }
    return normalizada


def _fusionar_detalles(op, detalles):
    """Complete ``op`` with the offer detail without losing useful data.

    The description is always replaced by the longest available one; the other
    fields are only filled when missing.
    """
    if not isinstance(detalles, dict):
        return op

    detalle = _normalizar({**detalles, "id": op["id"]})
    for campo in ("url", "titulo", "empresa", "salario", "ubicacion", "remoto", "contrato"):
        if not op.get(campo) and detalle.get(campo):
            op[campo] = detalle[campo]

    descripcion_nueva = detalle.get("descripcion") or ""
    if len(descripcion_nueva) > len(op.get("descripcion") or ""):
        op["descripcion"] = descripcion_nueva

    # Keep extra fields useful for scoring/generation.
    for campo in ("requisitos", "requirements", "idioma", "language"):
        if detalles.get(campo) and not op.get(campo):
            op[campo] = detalles[campo]
    return op


def _ahora_iso():
    """UTC timestamp in ISO-8601 (as in db.py)."""
    return datetime.datetime.now(datetime.UTC).isoformat()


# ---------------------------------------------------------------------------
# Source merging (LinkedIn + boards + JobSpy)
# ---------------------------------------------------------------------------

# Corporate suffixes that do not distinguish one company from another when
# comparing portals ("Acme Inc", "Acme LLC" and "Acme" are the same company).
# Only trailing text is trimmed. ``s\.?\s?l`` covers "S.L.", "S. L.", "SL" and
# "S L".
_RE_SUFIJO_EMPRESA = re.compile(
    r"[\s,.\-]*(?:inc|llc|ltd|limited|gmbh|corp|corporation|co|bv|nv|ag|plc"
    r"|sarl|s\.?\s?l|s\.?\s?a|sl|sa)\b\.?$"
)

# Parentheses that only state modality or equal opportunity do not distinguish
# one offer from another ("Data Analyst (Remote)" and "Data Analyst"
# are the same role). Parentheses with any other content ARE kept, so "(UX)"
# is not merged with "(UI)".
_RE_PARENTESIS = re.compile(r"\(([^)]*)\)")
_MARCAS_PARENTESIS = (
    "remote",
    "remoto",
    "remota",
    "hybrid",
    "hibrido",
    "hibrida",
    "onsite",
    "on-site",
    "on site",
    "presencial",
    "teletrabajo",
    "full-time",
    "part-time",
    "full time",
    "part time",
    "m/f/d",
    "m/w/d",
    "f/m/d",
    "w/m/d",
    "m/f/x",
    "m f d",
    "m w d",
    "f m d",
    "w m d",
    "m f x",
)


def _sin_acentos(texto):
    """Lowercase and strip diacritics (NFKD): 'Café' -> 'cafe'."""
    descompuesto = unicodedata.normalize("NFKD", str(texto or "").lower())
    return "".join(c for c in descompuesto if not unicodedata.combining(c))


def _normalizar_texto_comparable(valor):
    """Lowercase, no accents, no punctuation, single spaces."""
    texto = re.sub(r"[^a-z0-9]+", " ", _sin_acentos(valor))
    return " ".join(texto.split())


def _quitar_parentesis_modalidad(texto):
    """Remove modality/EEO parentheses; keep the distinguishing ones."""

    def _sustituir(coincidencia):
        contenido = _normalizar_texto_comparable(coincidencia.group(1))
        if any(marca in contenido for marca in _MARCAS_PARENTESIS):
            return " "
        return coincidencia.group(0)

    return _RE_PARENTESIS.sub(_sustituir, str(texto or ""))


def _quitar_sufijo_empresa(texto):
    """Trim trailing corporate suffixes (``Acme Corp LLC`` -> ``Acme``)."""
    limpio = " " + str(texto or "").strip()
    for _ in range(4):  # "Acme Corp LLC" needs two trims
        recortado = _RE_SUFIJO_EMPRESA.sub("", limpio)
        if recortado == limpio:
            break
        limpio = recortado
    return limpio.strip()


def _normalizar_texto_clave(valor):
    """Normalize a text so it can be compared across sources.

    Lowercase, no accents, no punctuation, no modality parentheses
    (``(Remote)``, ``(Hybrid)``) and no corporate suffixes (``Inc``, ``LLC``,
    ``Ltd``, ``S.L.``, ``GmbH``). This way the same offer published on Indeed
    and Glassdoor with the company written differently is detected as a
    duplicate.
    """
    texto = str(valor or "").lower()
    texto = _quitar_parentesis_modalidad(texto)
    texto = _quitar_sufijo_empresa(texto)
    return _normalizar_texto_comparable(texto)


def _claves_deduplicacion(job):
    """Keys used to recognize the same offer across two sources.

    Compared by URL (without query or trailing slash) and by title+company:
    the same role can arrive from LinkedIn and from the ATS board with
    different URLs (or even the same role on two portals).
    """
    claves = []
    url = _primer_valor(job, ("url", "link", "job_url"))
    if url:
        limpia = str(url).split("?")[0].split("#")[0].strip().rstrip("/").lower()
        if limpia:
            claves.append("url:" + limpia)

    titulo = _normalizar_texto_clave(_primer_valor(job, ("titulo", "title", "position")))
    if titulo:
        empresa = _normalizar_texto_clave(_primer_valor(job, ("empresa", "company", "companyName")))
        # Without a company the separator is omitted, so the key of a bare
        # title does not end in "|" and matches the one from another source.
        claves.append(f"te:{titulo}|{empresa}" if empresa else "te:" + titulo)
    return claves


def _fusionar_fuentes(*listas):
    """Concatenate offer lists keeping the first occurrence.

    Argument order is priority: whatever comes first (LinkedIn) wins over a
    duplicate from an alternative source.
    """
    vistas = set()
    salida = []
    for lista in listas:
        for bruto in lista or []:
            if not isinstance(bruto, dict):
                continue
            claves = _claves_deduplicacion(bruto)
            if not claves:
                continue
            if any(clave in vistas for clave in claves):
                continue
            vistas.update(claves)
            salida.append(bruto)
    return salida


def _keywords_negativas(config):
    """``KEYWORDS_NEGATIVAS`` from the config, normalized and deduplicated."""
    valor = (config or {}).get("KEYWORDS_NEGATIVAS") or []
    if isinstance(valor, str):
        valor = [valor]
    salida = []
    for item in valor:
        limpia = _normalizar_texto_comparable(item)
        if limpia and limpia not in salida:
            salida.append(limpia)
    return salida


def _filtrar_keywords_negativas(ofertas, keywords=None, config=None):
    """Drop offers whose title or description contains a negative keyword.

    The comparison is loose (lowercase, no accents, no punctuation) and
    requires a whole word, so ``gaming`` does not reject ``gaming`` inside
    another word. Without configured keywords the list is returned intact.
    """
    palabras = (
        _keywords_negativas(config)
        if keywords is None
        else [_normalizar_texto_comparable(k) for k in keywords if _normalizar_texto_comparable(k)]
    )
    if not palabras:
        return list(ofertas or [])

    patrones = [
        (palabra, re.compile(rf"(?<![a-z0-9]){re.escape(palabra)}(?![a-z0-9])"))
        for palabra in palabras
    ]
    conservadas = []
    descartadas = 0
    for oferta in ofertas or []:
        if not isinstance(oferta, dict):
            continue
        texto = _normalizar_texto_comparable(
            "{} {}".format(
                _primer_valor(oferta, ("titulo", "title", "position"), "") or "",
                _primer_valor(oferta, ("descripcion", "description", "description_text"), "") or "",
            )
        )
        encontrada = next((palabra for palabra, patron in patrones if patron.search(texto)), None)
        if encontrada:
            descartadas += 1
            log.debug(
                "Descartada por keyword negativa %r: %s",
                encontrada,
                oferta.get("titulo") or oferta.get("url") or "(sin título)",
            )
            continue
        conservadas.append(oferta)

    log.info(
        "Filtro de keywords negativas (%s): %s descartadas, %s conservadas.",
        ", ".join(palabras),
        descartadas,
        len(conservadas),
    )
    return conservadas


_SETTINGS_CRUDO = None


def _settings_crudo():
    """Read ``data/settings.json`` once per process (keys outside the spec).

    ``load_config`` drops keys not present in ``_CONFIG_DEFS``, so age and
    cooldown are read here directly until they are added to the spec. Returns
    ``{}`` if the file is missing or corrupt.
    """
    global _SETTINGS_CRUDO
    if _SETTINGS_CRUDO is None:
        try:
            from config import leer_settings

            _SETTINGS_CRUDO = leer_settings() or {}
        except Exception as err:  # noqa: BLE001 — the JSON is optional
            log.warning("No se pudo leer settings.json para antigüedad/cooldown: %s", err)
            _SETTINGS_CRUDO = {}
    return _SETTINGS_CRUDO


def _entero_config(config, claves, variable, defecto):
    """Positive integer from the editable config, environment as fallback.

    Priority: ``load_config`` dict > raw ``settings.json`` (in case the key is
    not in the spec yet) > environment variable > ``defecto``. Returns
    ``defecto`` when there is no value, it is not an integer or it is not
    positive (with a warning), as the direct environment read used to do.
    """
    crudo = None
    for clave in claves:
        valor = (config or {}).get(clave)
        if valor not in (None, ""):
            crudo = valor
            break
    if crudo in (None, ""):
        crudo_json = _settings_crudo()
        for clave in claves:
            valor = crudo_json.get(clave)
            if valor not in (None, ""):
                crudo = valor
                break
    if crudo in (None, ""):
        crudo = os.environ.get(variable)
    if crudo in (None, ""):
        return defecto
    try:
        numero = int(str(crudo).strip())
    except (TypeError, ValueError):
        log.warning("%s=%r no es un entero; se usa %s.", variable, crudo, defecto)
        return defecto
    if numero <= 0:
        log.warning("%s=%r no es positivo; se usa %s.", variable, crudo, defecto)
        return defecto
    return numero


def _max_antiguedad_dias(config=None):
    """Maximum age in days: editable config > environment > 30.

    An invalid value is logged but never aborts the run.
    """
    return _entero_config(
        config,
        CLAVES_CONFIG_ANTIGUEDAD,
        VARIABLE_MAX_ANTIGUEDAD,
        MAX_ANTIGUEDAD_DIAS,
    )


def _cooldown_segundos(config=None):
    """LinkedIn cooldown in seconds: editable config > environment > 6 h."""
    return _entero_config(
        config, CLAVES_CONFIG_COOLDOWN, VARIABLE_COOLDOWN, COOLDOWN_SEGUNDOS_DEFECTO
    )


def _terminos_config(config):
    """Search terms from ``ROLES`` + ``TOPICS`` (deduplicated).

    Config order is respected: roles first (what the user looks for) and then
    topics (fintech, ecommerce...) as extra queries. Returns ``[]`` when
    nothing is configured.
    """
    terminos = []
    for clave in ("ROLES", "TOPICS"):
        valor = (config or {}).get(clave) or []
        if isinstance(valor, str):
            valor = [valor]
        if not isinstance(valor, (list, tuple)):
            log.warning("%s no es una lista; se ignora como término de búsqueda.", clave)
            continue
        for item in valor:
            limpio = " ".join(str(item or "").split())
            if not limpio:
                continue
            if limpio.lower() in (t.lower() for t in terminos):
                continue
            terminos.append(limpio)
    return terminos


def _fuente_activa(config, clave):
    """True if source ``clave`` (FUENTES_*) is enabled in the config.

    Accepts the boolean already converted by ``config.load_config`` and, for
    robustness, text such as ``"false"``/``"0"``/``"off"``. Absent = enabled.
    """
    valor = (config or {}).get(clave, True)
    if isinstance(valor, str):
        return valor.strip().lower() not in ("", "0", "false", "no", "off", "n")
    return bool(valor)


def _fecha_oferta(valor):
    """Return the ``date`` of a publication date, or ``None``.

    Accepts what ``search.boards``/``search.jobspy`` already normalize
    (``YYYY-MM-DD``) and also ``date``/``datetime`` in case a future source
    delivers them raw.
    """
    if not valor:
        return None
    if isinstance(valor, datetime.datetime):
        return valor.date()
    if isinstance(valor, datetime.date):
        return valor

    texto = str(valor).strip()
    if len(texto) < 10:
        return None
    try:
        return datetime.date.fromisoformat(texto[:10])
    except ValueError:
        return None


def _filtrar_por_antiguedad(ofertas, dias=None, config=None):
    """Drop offers published more than ``dias`` days ago.

    Offers **without** ``fecha_publicacion`` (or with an unreadable date) are
    kept: nothing is dropped for a missing field. ``dias`` and ``config`` allow
    using the editable value from ``settings.json``; by default it resolves
    with ``_max_antiguedad_dias(config)``. Returns the filtered list and logs
    how many were dropped.
    """
    dias = _max_antiguedad_dias(config) if dias is None else dias
    hoy = datetime.date.today()
    limite = hoy - datetime.timedelta(days=dias)

    conservadas = []
    descartadas = 0
    sin_fecha = 0
    for oferta in ofertas or []:
        if not isinstance(oferta, dict):
            continue
        fecha = _fecha_oferta(oferta.get("fecha_publicacion"))
        if fecha is None:
            sin_fecha += 1
            conservadas.append(oferta)
            continue
        if fecha < limite:
            descartadas += 1
            log.debug(
                "Descartada por antigüedad (%s < %s): %s",
                fecha.isoformat(),
                limite.isoformat(),
                oferta.get("titulo") or oferta.get("url") or "(sin título)",
            )
            continue
        conservadas.append(oferta)

    log.info(
        "Filtro de antigüedad (máx. %s días, desde %s): %s descartadas, "
        "%s conservadas (%s sin fecha, se conservan).",
        dias,
        limite.isoformat(),
        descartadas,
        len(conservadas),
        sin_fecha,
    )
    return conservadas


# ---------------------------------------------------------------------------
# Per-offer pipeline
# ---------------------------------------------------------------------------


def _procesar_oferta(bruto, perfil, umbral, idioma="en"):
    """Process one offer and return the op ready to notify, or None.

    ``idioma`` ("en"/"es") is the language of the descriptive texts returned
    by scoring; it does not affect the score cache.
    """
    op = _normalizar(bruto)

    if not db.is_nueva(op):
        log.debug("Ya vista, se omite: %s", op["id"])
        return None

    # The browser detail only makes sense for LinkedIn: board offers already
    # carry the full description (and launching Chromium for each would be
    # slow and pointless).
    origen = str(bruto.get("source") or ORIGEN_LINKEDIN).lower()
    if origen == ORIGEN_LINKEDIN:
        job_id = bruto.get("id") or bruto.get("job_id") or op["id"]
        try:
            from search.linkedin import get_job_details

            with _LOCK_DETALLE_LINKEDIN:
                detalles = get_job_details(job_id)
            _fusionar_detalles(op, detalles)
        except Exception as err:  # noqa: BLE001
            log.warning("Sin detalle para %s (%s); se evalúa con lo disponible.", op["id"], err)

    resultado = scoring.score(op, perfil, idioma=idioma)
    op["score"] = resultado["score"]
    op["motivos"] = resultado["motivos"]
    op["gaps"] = resultado["gaps"]
    # Justification (pros and cons) persisted for the web panel.
    op["justificacion"] = json.dumps(
        {
            "motivos": resultado.get("motivos") or [],
            "gaps": resultado.get("gaps") or [],
            "recomendacion": resultado.get("recomendacion") or "",
        },
        ensure_ascii=False,
    )

    log.info(
        "Score %s/100 — %s @ %s",
        op["score"],
        op.get("titulo") or "(sin título)",
        op.get("empresa") or "(sin empresa)",
    )
    for motivo in resultado["motivos"]:
        log.info("  + %s", motivo)
    for gap in resultado["gaps"]:
        log.info("  - %s", gap)

    if op["score"] < umbral:
        log.info("Descartada por score (< %s): %s", umbral, op["id"])
        # Still stored as rejected so the panel can show why and let the user
        # reconsider the decision.
        op["estado"] = "descartada"
        op["created_at"] = _ahora_iso()
        op["updated_at"] = op["created_at"]
        db.insertar(op)
        return None

    # Materials (cover letter, CV, message and PDF) are NOT generated here:
    # they are generated when the user approves the offer, to avoid spending
    # tokens on offers that are never confirmed. The description is stored in
    # the database so they can be generated at that moment.

    op["estado"] = ESTADO_PARA_CONFIRMAR
    op["created_at"] = _ahora_iso()
    op["updated_at"] = op["created_at"]

    if db.insertar(op):
        log.info("Insertada para confirmar: %s", op["id"])
    else:
        log.warning("No se insertó (ya existía): %s", op["id"])
        return None

    return op


def _configurar_log_fichero(config):
    """Add a FileHandler so the web panel shows live activity.

    Writes to ``JOB_LOG_PATH`` in addition to stderr (docker logs), so both the
    02:00 scheduler and a ``run once`` leave a visible trace in the panel.
    """
    import logging.handlers

    ruta = (config or {}).get("JOB_LOG_PATH") or os.environ.get("JOB_LOG_PATH")
    if not ruta:
        return
    try:
        os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
        manejador = logging.handlers.RotatingFileHandler(
            ruta, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        manejador.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(manejador)
    except Exception as err:  # noqa: BLE001 — the file is an extra, not critical.
        log.warning("No se pudo abrir el log de fichero %s: %s", ruta, err)


def _alertar_sesion_linkedin(config):
    """Alert (Telegram + webhook) if the LinkedIn session expired or is missing.

    Only acts when ``ALERTAR_SESION_LINKEDIN`` is enabled. The
    ``search.linkedin`` import is local on purpose: that module pulls in
    patchright and must not be loaded when importing ``main`` (nor when opening
    the panel), besides avoiding circular imports.

    ``SESION_LINKEDIN_MAX_DIAS`` overrides the module's own threshold before
    asking, so the panel wins over the default. This alert is best-effort: any
    failure is logged and the run still finishes.
    """
    if not _fuente_activa(config, "ALERTAR_SESION_LINKEDIN"):
        return
    try:
        import search.linkedin as linkedin_mod

        dias = (config or {}).get("SESION_LINKEDIN_MAX_DIAS")
        if isinstance(dias, int) and dias > 0:
            linkedin_mod.DIAS_CADUCIDAD_SESION = dias
        estado = linkedin_mod.sesion_estado() or {}
    except Exception as err:  # noqa: BLE001 — optional diagnostics.
        log.warning("No se pudo comprobar la sesión de LinkedIn: %s", err)
        return

    if not estado.get("caducada"):
        return

    log.warning(
        "Sesión de LinkedIn caducada o ausente (antigüedad: %s días, ruta: %s).",
        estado.get("antiguedad_dias"),
        estado.get("ruta"),
    )
    try:
        notify.enviar_aviso(
            "La sesión de LinkedIn está caducada o ausente; exporta una nueva desde el Mac"
        )
    except Exception as err:  # noqa: BLE001 — the alert must not kill the run.
        log.error("No se pudo enviar el aviso de sesión de LinkedIn: %s", err)


def _ruta_lock_busqueda():
    """Path of the lock shared with the panel: ``<state.db dir>/.buscar.lock``.

    Resolved with ``db._resolver_path`` so the scheduler and the panel always
    target the same file. If the config cannot be read, the service directory
    is used (where ``state.db`` lives by default).
    """
    try:
        ruta_db = db._resolver_path(None)
    except Exception as err:  # noqa: BLE001 — the lock must never kill the run.
        log.warning("No se pudo resolver la ruta de la base para el lock: %s", err)
        ruta_db = os.path.join(_DIR_SERVICIO, "state.db")
    return os.path.join(os.path.dirname(os.path.abspath(ruta_db)), NOMBRE_LOCK_BUSQUEDA)


def _adquirir_lock_busqueda():
    """Take the non-blocking exclusive search lock.

    Returns:
        ``(handler, available)``:
          * ``(handler, True)`` — lock acquired; release it when done.
          * ``(None, False)`` — another search holds it: this run is skipped.
          * ``(None, True)`` — no ``fcntl`` (non-POSIX platform): continue
            without a lock, as before.
    """
    if fcntl is None:
        return None, True

    ruta = _ruta_lock_busqueda()
    try:
        os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
        fichero = open(ruta, "a+")
    except OSError as err:
        log.warning("No se pudo abrir el lock %s (%s); se sigue sin lock.", ruta, err)
        return None, True

    try:
        fcntl.flock(fichero.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fichero.close()
        return None, False
    return fichero, True


def _liberar_lock_busqueda(fichero):
    """Release the lock and close the file (idempotent if ``fichero`` is None)."""
    if not fichero or fcntl is None:
        return
    try:
        fcntl.flock(fichero.fileno(), fcntl.LOCK_UN)
    except OSError as err:  # noqa: BLE001 — the process ends anyway.
        log.warning("No se pudo liberar el lock de búsqueda: %s", err)
    finally:
        try:
            fichero.close()
        except OSError:
            pass


def _ejecutar(config, args):
    """Full search cycle, guarded by the anti-concurrency lock.

    If a search is already running (scheduler, ``run once`` or the panel's
    "Buscar ahora" button), this run is skipped instead of duplicating the work
    against LinkedIn and Telegram.
    """
    fichero_lock, disponible = _adquirir_lock_busqueda()
    if not disponible:
        log.warning("ya hay una búsqueda en curso; se omite este run")
        return 0
    try:
        return _ejecutar_run(config, args)
    finally:
        _liberar_lock_busqueda(fichero_lock)


def _ejecutar_run(config, args):
    """Full search, scoring, merge and notification cycle.

    Receives the already loaded (and strictly validated) configuration so the
    fail-fast can be isolated in ``main``.
    """
    _configurar_log_fichero(config)
    db.init_db()
    perfil = cargar_perfil(config)
    umbral = int(config.get("SCORE_THRESHOLD") or 80)
    # Language of the scoring descriptive texts (English by default). The config
    # arrives already loaded from ``main``; the score cache key does not depend
    # on the language, so changing it does not re-score evaluated offers.
    idioma_scoring = (config or {}).get("IDIOMA_SCORING") or "en"

    log.info("Perfil cargado: %s", perfil.get("headline", "(sin headline)"))

    # Search terms: ROLES + TOPICS from the editable config (settings.json).
    # QUERIES is an empty-by-default fallback, so the configured roles decide
    # what is searched; with nothing configured the run searches nothing.
    terminos = _terminos_config(config)
    consultas = terminos or list(QUERIES)
    # No queries at all: nothing to search for. Every source below is skipped
    # and the run ends with 0 offers (no hardcoded role is assumed).
    if not consultas:
        log.warning(
            "Sin consultas: define ROLES o TOPICS en data/settings.json "
            "(panel → Configuración). No se lanza ninguna búsqueda."
        )
    dias_maximos = _max_antiguedad_dias(config)

    # FUENTES_* from the config: a source switched off in the panel is skipped.
    usar_linkedin = _fuente_activa(config, "FUENTES_LINKEDIN")
    usar_boards = _fuente_activa(config, "FUENTES_BOARDS")
    usar_jobspy = _fuente_activa(config, "FUENTES_JOBSPY")

    log.info(
        "Umbral de score: %s · consultas: %s (%s) · antigüedad máx.: %s días",
        umbral,
        len(consultas),
        ", ".join(consultas[:6]) or "-",
        dias_maximos,
    )
    log.info(
        "Fuentes activas — LinkedIn: %s · boards: %s · JobSpy: %s",
        "sí" if usar_linkedin else "no",
        "sí" if usar_boards else "no",
        "sí" if usar_jobspy else "no",
    )
    log.info("Idioma del scoring: %s", idioma_scoring)

    # LinkedIn is the main source but no longer essential: if it falls
    # (expired session, ERR_TOO_MANY_REDIRECTS, rate-limit) the run continues
    # with the alternative sources instead of aborting.
    ofertas_linkedin = []
    linkedin_disponible = usar_linkedin
    LinkedInError = None  # type: ignore[assignment]
    search_jobs = None
    if not consultas:
        log.info("LinkedIn omitido: no hay consultas configuradas (ROLES/TOPICS).")
    elif not usar_linkedin:
        log.info("LinkedIn desactivado en la configuración (FUENTES_LINKEDIN=false); se omite.")
    else:
        try:
            from search.linkedin import LinkedInError, search_jobs  # noqa: F811
        except ImportError as err:
            log.error(
                "No se pudo importar search.linkedin (%s); se sigue con las demás fuentes.",
                err,
            )
            linkedin_disponible = False
        else:
            # Editable cooldown (settings.json) applied to the module before
            # searching. Isolated: an old version without
            # ``configurar_cooldown`` must not take LinkedIn down.
            try:
                from search.linkedin import configurar_cooldown

                log.info(
                    "Cooldown de LinkedIn: %s s", configurar_cooldown(_cooldown_segundos(config))
                )
            except ImportError:
                log.warning(
                    "search.linkedin sin configurar_cooldown(); se usa el cooldown del entorno."
                )

    if search_jobs is not None:
        try:
            ofertas_linkedin = (
                search_jobs(
                    consultas,
                    limit=LIMITE_POR_QUERY,
                )
                or []
            )
        except Exception as err:  # noqa: BLE001 — a failure does not abort the run.
            es_linkedin = LinkedInError is not None and isinstance(err, LinkedInError)
            log.error(
                "La búsqueda en LinkedIn falló (%s): %s. Se continúa con las "
                "fuentes alternativas (ATS y portales remotos).",
                "LinkedInError" if es_linkedin else type(err).__name__,
                err,
            )
            ofertas_linkedin = []
            linkedin_disponible = False

    for bruto in ofertas_linkedin:
        if isinstance(bruto, dict):
            bruto.setdefault("source", ORIGEN_LINKEDIN)

    # Alternative sources: public ATS (Greenhouse/Lever/Ashby) and remote
    # portals (Remotive/WWR). They need no session, so they do not share
    # LinkedIn's rate limit. Their failure does not kill the run either.
    ofertas_boards = []
    if not consultas:
        log.info("Boards omitidos: no hay consultas configuradas (ROLES/TOPICS).")
    elif not usar_boards:
        log.info("Boards desactivados en la configuración (FUENTES_BOARDS=false); se omite.")
    else:
        try:
            from search.boards import buscar_ofertas_boards

            # ROLES + TOPICS also filter the board titles and feed their
            # searches (Remotive/EURES); the editable age avoids fetching
            # offers main would drop afterwards.
            ofertas_boards = (
                buscar_ofertas_boards(
                    limite_por_fuente=LIMITE_POR_FUENTE_BOARDS,
                    terminos=terminos,
                    max_antiguedad_dias=dias_maximos,
                )
                or []
            )
        except Exception as err:  # noqa: BLE001
            log.error("Las fuentes alternativas fallaron: %s", err)

    # Third source: Indeed/ZipRecruiter/Glassdoor via JobSpy (python-jobspy
    # library, no browser or session). Complements LinkedIn and the boards
    # without sharing their rate limit. Its failure does not kill the run
    # either: the function isolates each site and each query.
    ofertas_jobspy = []
    if not consultas:
        log.info("JobSpy omitido: no hay consultas configuradas (ROLES/TOPICS).")
    elif not usar_jobspy:
        log.info("JobSpy desactivado en la configuración (FUENTES_JOBSPY=false); se omite.")
    else:
        try:
            from search.jobspy import UBICACION_POR_DEFECTO, buscar_ofertas_jobspy

            # JobSpy takes hours, not days: capped at the usual 72 h and never
            # more than the configured age allows.
            horas_jobspy = max(24, min(HORAS_RECIENTES_JOBSPY, dias_maximos * 24))
            ubicaciones = config.get("UBICACIONES") or []
            if isinstance(ubicaciones, str):
                ubicaciones = [ubicaciones]
            ubicacion = next(
                (str(u).strip() for u in ubicaciones if str(u or "").strip()),
                UBICACION_POR_DEFECTO,
            )
            ofertas_jobspy = (
                buscar_ofertas_jobspy(
                    consultas,
                    limite=LIMITE_POR_QUERY_JOBSPY,
                    horas_recientes=horas_jobspy,
                    ubicacion=ubicacion,
                )
                or []
            )
        except Exception as err:  # noqa: BLE001
            log.error("La fuente JobSpy (Indeed/ZipRecruiter/Glassdoor) falló: %s", err)

    # Merge with priority for LinkedIn (same offer: LinkedIn's one wins).
    ofertas = _fusionar_fuentes(ofertas_linkedin, ofertas_boards, ofertas_jobspy)

    # Age filter: drop offers published more than MAX_ANTIGUEDAD_DIAS days ago
    # (30 by default, editable in settings.json). Those without a publication
    # date are kept.
    ofertas = _filtrar_por_antiguedad(ofertas, dias=dias_maximos)

    # Panel negative keywords: applied to the already merged set, looking at
    # title + description (so what comes from JobSpy is dropped too).
    ofertas = _filtrar_keywords_negativas(ofertas, config=config)

    log.info(
        "Ofertas recibidas: %s (LinkedIn: %s%s, boards: %s, jobspy: %s).",
        len(ofertas),
        len(ofertas_linkedin),
        "" if linkedin_disponible or not usar_linkedin else ", caído",
        len(ofertas_boards),
        len(ofertas_jobspy),
    )
    if not ofertas:
        log.error(
            "Ninguna oferta quedó tras la fusión y los filtros (antigüedad/keywords); "
            "se termina sin procesar nada."
        )
        return 0

    para_confirmar = []
    enviadas = 0
    vistas = set()
    pendientes = []
    for bruto in ofertas:
        if not isinstance(bruto, dict):
            continue
        try:
            clave = _generar_id(bruto)
        except Exception:  # noqa: BLE001
            continue
        if clave in vistas:
            continue
        vistas.add(clave)
        pendientes.append((clave, bruto))

    # Parallel scoring: the LLM call is the bottleneck and shares no state
    # (each offer is normalized and scored on its own). The Telegram
    # notification is sent afterwards, sequentially, so the bot is not flooded
    # and messages stay ordered.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    log.info("Puntuando %d ofertas nuevas con %d hilos…", len(pendientes), HILOS_SCORING)
    resueltas = []
    if pendientes:
        with ThreadPoolExecutor(max_workers=HILOS_SCORING) as pool:
            futuros = {
                pool.submit(_procesar_oferta, bruto, perfil, umbral, idioma_scoring): clave
                for clave, bruto in pendientes
            }
            for futuro in as_completed(futuros):
                clave = futuros[futuro]
                try:
                    resueltas.append((clave, futuro.result()))
                except Exception as err:  # noqa: BLE001 — isolate each offer.
                    log.exception("Error procesando %s: %s", clave, err)

    for _clave, op in resueltas:
        if not op:
            continue
        if args.modo == "manual":
            try:
                notify.enviar_oferta(op)
                enviadas += 1
            except Exception as err:  # noqa: BLE001
                log.error("No se pudo enviar %s a Telegram: %s", op["id"], err)
        else:
            para_confirmar.append(op)

    confirmadas = enviadas if args.modo == "manual" else len(para_confirmar)
    log.info(
        "Run terminado: %s nuevas evaluadas, %s %s.",
        len(vistas),
        confirmadas,
        "enviadas al bot" if args.modo == "manual" else "para confirmar",
    )

    if args.modo == "manual":
        if enviadas == 0:
            try:
                notify.enviar_resumen([])  # "no offers" alert
            except Exception as err:  # noqa: BLE001
                log.error("No se pudo enviar el aviso por Telegram: %s", err)
                return 1
        return 0

    try:
        notify.enviar_resumen(para_confirmar)
        log.info("Resumen enviado (Telegram si está habilitado + webhook).")
    except Exception as err:  # noqa: BLE001
        log.error("No se pudo enviar el resumen por Telegram: %s", err)
        return 1

    return 0


def main():
    """Entry point: config fail-fast, run and session alert at the end."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Jobautomat — búsqueda de empleo")
    parser.add_argument(
        "--modo",
        choices=["cron", "manual"],
        default="cron",
        help="cron: resumen al final (de golpe); manual: un mensaje por oferta en tiempo real.",
    )
    args = parser.parse_args()

    # Fail-fast: without DEEPSEEK_API_KEY nothing can be scored or generated,
    # so print the actionable message and exit with code 1 instead of starting
    # a run doomed to fail offer after offer.
    try:
        config = load_config(estricto=True)
    except RuntimeError as err:
        print(f"[main] Error de configuración: {err}", file=sys.stderr)
        sys.exit(1)

    try:
        return _ejecutar(config, args)
    finally:
        # At the end of the run (however it ends): alert if the LinkedIn
        # session expired, so the user exports a new one from the Mac.
        _alertar_sesion_linkedin(config)


if __name__ == "__main__":
    sys.exit(main())
