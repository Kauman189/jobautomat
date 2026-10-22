"""web.py — Flask web panel to review and act on job offers.

Lists opportunities from ``state.db`` with their score and lets the user
approve or discard them from the browser. Approving reuses the same logic as
the Telegram bot (``notify._enviar_pack``): missing materials are generated,
stored in the database and returned so the user applies for them himself.
**This service never submits an application.**

Endpoints
---------
GET  /                              single-page HTML panel.
GET  /api/ofertas?estado=           list (id, titulo, empresa, score, ...).
GET  /api/oferta/<id>               detail with generated materials.
POST /api/oferta/<id>/aprobar       approve and generate the pack.
POST /api/oferta/<id>/descartar     discard.
GET  /api/perfil                    full profile (data/perfil.json).
POST /api/perfil/agregar            merge a new fact into the profile (LLM).
POST /api/perfil/borrar             delete a profile key or index.
PUT  /api/perfil                    replace the full profile (technical JSON mode).
POST /api/preguntar                 answer application questions with the LLM.
GET  /api/config                    effective config (secrets masked).
PUT  /api/config                    update data/settings.json (atomic).
GET  /api/metrics                   counts by status and by source (read-only).
GET  /api/salud                     self-host diagnostics (LLM, Telegram,
                                    LinkedIn, /data, Chromium).
GET  /api/export                    profile + settings (secrets masked).
POST /api/import                    restore profile and/or settings from an export.
POST /api/fuente/probar             probe a board with one real request.
POST /api/busquedas/limpiar         delete search results, keep approved ones.
GET  /candidaturas/<path>           serve cv.pdf, cover.pdf, cover.txt.

Authentication: HTTP Basic, user ``casa`` and the ``WEB_PASSWORD`` password
(environment or ``service/.env``). Without a configured password no auth is
enforced: the panel must only be exposed on the LAN.

Hardening for distributed use:
- In-memory per-IP limit (``RATE_LIMIT_PREGUNTAS`` / ``RATE_LIMIT_ACCIONES``
  per ``RATE_LIMIT_VENTANA`` s) on actions that spend LLM calls or write;
  exceeding it returns 429 with ``Retry-After``.
- CSRF/same-origin: POST/PUT with an ``Origin``/``Referer`` from another host
  get 403. Missing headers (curl, scripts) are allowed, since Basic auth is
  the real authorization. ``ORIGENES_PERMITIDOS`` adds origins.

Startup: ``python web.py`` (port 8085, configurable with ``WEB_PORT``).
"""

import copy
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

# Lock shared with main.py so the "Buscar ahora" button cannot overlap two
# searches. Without fcntl (Windows) the panel still works, just without a lock.
try:
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX platforms only.
    fcntl = None  # type: ignore[assignment]

import config
import db
import generate as generate_mod
import health
import llm
import notify as notify_mod
from flask import Flask, Response, jsonify, request, send_from_directory
from i18n import t

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
INDEX_HTML = os.path.join(WEB_DIR, "index.html")

# --- Request limits (in memory, per IP) ------------------------------------
# Single-process self-hosted service: no Redis, no new dependencies. State is
# lost on restart, which is acceptable for this use case (30 LLM questions per
# hour and IP).
RATE_LIMIT_PREGUNTAS = 30  # max POST /api/preguntar per window and IP
RATE_LIMIT_ACCIONES = 120  # max approve/discard calls per window and IP
RATE_LIMIT_VENTANA = 3600  # sliding window length in seconds

# Extra origins allowed for POST (besides the request Host itself).
# Comma-separated list in ``ORIGENES_PERMITIDOS``, e.g.
#   ORIGENES_PERMITIDOS="https://panel.casa.lan,http://192.168.1.20:8085"
_ORIGENES_PERMITIDOS = tuple(
    o.strip().rstrip("/").lower()
    for o in (os.environ.get("ORIGENES_PERMITIDOS") or "").split(",")
    if o.strip()
)

ESTADO_APROBADA = "aprobada"
ESTADO_DESCARTADA = "descartada"

# Database states that count as "pending" in the panel.
_ESTADOS_PENDIENTES = ("para_confirmar", "pendiente", "nueva", None, "")

# URL prefixes whose forms this service can parse and answer.
_DOMINIOS_FORMULARIO = ("greenhouse", "lever.co", "ashbyhq")

# In-memory cache of form answers (avoids reopening the browser when the same
# offer is approved twice in the same session).
_CACHE_RESPUESTAS = {}
_LOCK_RESPUESTAS = threading.Lock()

# generate.py writes to ../candidaturas outside the container; in Docker that
# is redirected to the persistent volume /data/candidaturas via CANDIDATURAS_DIR.
_DIR_CANDIDATURAS_ENV = os.environ.get("CANDIDATURAS_DIR", "").strip()
if _DIR_CANDIDATURAS_ENV:
    generate_mod._DIR_CANDIDATURAS = _DIR_CANDIDATURAS_ENV

app = Flask(__name__, static_folder=WEB_DIR, static_url_path="/static")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def _password():
    """Return the panel password from ``WEB_PASSWORD`` or ``service/.env``.

    Returns:
        The configured password, or ``""`` when there is none.
    """
    pwd = (os.environ.get("WEB_PASSWORD") or "").strip()
    if pwd:
        return pwd
    try:
        # .env + data/settings.json (settings.json wins over the environment).
        return (config.load_config(estricto=False).get("WEB_PASSWORD") or "").strip()
    except Exception:  # noqa: BLE001 — no .env simply means no auth.
        return ""


@app.before_request
def _exigir_auth():
    """Require HTTP Basic auth when a password is configured.

    Returns:
        ``None`` when the request may proceed, otherwise the 401 ``Response``.
    """
    pwd = _password()
    if not pwd:
        return None
    auth = request.authorization
    valida = (
        auth is not None
        and (auth.type or "").lower() == "basic"
        and auth.username == "casa"
        and hmac.compare_digest(str(auth.password or ""), pwd)
    )
    if valida:
        return None
    return Response(
        "Autenticación requerida",
        401,
        {"WWW-Authenticate": 'Basic realm="jobautomat"'},
    )


# ---------------------------------------------------------------------------
# Rate limiting (in memory, per IP)
# ---------------------------------------------------------------------------

# ip -> [monotonic timestamps of the calls inside the window]
_LIMITES = {}
_LOCK_LIMITES = threading.Lock()

# Flask endpoint -> (max calls, window in seconds)
_LIMITES_POR_ENDPOINT = {
    "api_preguntar": (RATE_LIMIT_PREGUNTAS, RATE_LIMIT_VENTANA),
    "api_perfil_agregar": (RATE_LIMIT_PREGUNTAS, RATE_LIMIT_VENTANA),
    "api_perfil_borrar": (RATE_LIMIT_ACCIONES, RATE_LIMIT_VENTANA),
    "api_perfil_put": (RATE_LIMIT_ACCIONES, RATE_LIMIT_VENTANA),
    "api_aprobar": (RATE_LIMIT_ACCIONES, RATE_LIMIT_VENTANA),
    "api_descartar": (RATE_LIMIT_ACCIONES, RATE_LIMIT_VENTANA),
    # Health diagnostics make network calls (up to 5s per block), export reads
    # two files, and import/probar write or reach the internet.
    "api_salud": (RATE_LIMIT_ACCIONES, RATE_LIMIT_VENTANA),
    "api_export": (RATE_LIMIT_ACCIONES, RATE_LIMIT_VENTANA),
    "api_import": (RATE_LIMIT_ACCIONES, RATE_LIMIT_VENTANA),
    "api_fuente_probar": (RATE_LIMIT_ACCIONES, RATE_LIMIT_VENTANA),
    # A manual search starts a full run (LinkedIn, LLM, Telegram): same ceiling
    # as the other actions.
    "api_buscar": (RATE_LIMIT_ACCIONES, RATE_LIMIT_VENTANA),
    # Destructive: wipes the search results (only the approved ones survive).
    "api_limpiar_busquedas": (RATE_LIMIT_ACCIONES, RATE_LIMIT_VENTANA),
    # NOTE: ``api_buscar_estado`` (GET) is deliberately absent: the panel polls
    # it every ~10 s to render the button, just like /api/log every 3 s; at
    # 120/h per IP the button would lock within minutes. It is a side-effect
    # free read.
}


def _ip_cliente():
    """Return the client IP used as the rate-limit key.

    Uses ``remote_addr`` (the real connection IP) and never ``X-Forwarded-For``,
    which the client can forge. Behind a trusted proxy, the proxy must rewrite
    ``remote_addr`` or ``ORIGENES_PERMITIDOS`` must be set.

    Returns:
        The client IP, or ``"desconocida"`` when unavailable.
    """
    return (request.remote_addr or "desconocida").strip()


def _consumir_limite(clave, maximo, ventana):
    """Record a call and return the wait time when the limit is exceeded.

    Args:
        clave: Rate-limit key (endpoint and IP).
        maximo: Maximum number of calls allowed in the window.
        ventana: Window length in seconds.

    Returns:
        ``0`` if the call is allowed (and recorded), otherwise the seconds left
        until the window frees a slot.
    """
    ahora = time.monotonic()
    with _LOCK_LIMITES:
        # Memory bound: with many IPs, drop the entries that are already empty.
        if len(_LIMITES) > 5000:
            for k in [k for k, v in _LIMITES.items() if not v]:
                del _LIMITES[k]
        marcas = _LIMITES.setdefault(clave, [])
        limite = ahora - ventana
        # Drop marks outside the window (keeps the list from growing).
        marcas[:] = [t for t in marcas if t > limite]
        if len(marcas) >= maximo:
            # The oldest slot frees up at marcas[0] + ventana.
            espera = int(marcas[0] + ventana - ahora) + 1
            return max(1, espera)
        marcas.append(ahora)
        return 0


@app.before_request
def _aplicar_limites():
    """Apply the per-IP limit to endpoints that spend LLM calls or write.

    Returns:
        ``None`` when the request may proceed, otherwise the 429 ``Response``.
    """
    regla = _LIMITES_POR_ENDPOINT.get(request.endpoint or "")
    if not regla:
        return None
    # Do not spend quota on requests the origin check is going to reject.
    if not _origen_permitido()[0]:
        return None
    maximo, ventana = regla
    espera = _consumir_limite(f"{request.endpoint}|{_ip_cliente()}", maximo, ventana)
    if not espera:
        return None
    print(
        t(
            f"[web] límite alcanzado en {request.endpoint} desde {_ip_cliente()} ({espera}s)",
            f"[web] rate limit reached on {request.endpoint} from {_ip_cliente()} ({espera}s)",
        ),
    )
    respuesta = jsonify(
        {
            "error": f"demasiadas solicitudes; espera {espera}s",
            "reintenta_en": espera,
        }
    )
    respuesta.status_code = 429
    respuesta.headers["Retry-After"] = str(espera)
    return respuesta


# ---------------------------------------------------------------------------
# CSRF / same-origin on state-changing POSTs
# ---------------------------------------------------------------------------


def _host_de(url):
    """Return the normalized host (with port) of a URL.

    Args:
        url: URL to parse.

    Returns:
        The lowercase netloc, or ``""`` for missing or invalid URLs.
    """
    try:
        return (urlsplit(url).netloc or "").lower()
    except Exception:  # noqa: BLE001 — garbage URL means no match.
        return ""


def _origen_permitido():
    """Validate the ``Origin``/``Referer`` headers of the request.

    Missing headers (curl, scripts) count as allowed: Basic auth is still the
    real authorization. ``Origin: null`` (sandbox/iframe) is rejected.

    Returns:
        A ``(ok, normalized_origin)`` tuple.
    """
    origen = (request.headers.get("Origin") or "").strip()
    if not origen:
        referer = (request.headers.get("Referer") or "").strip()
        if referer:
            partes = urlsplit(referer)
            origen = f"{partes.scheme}://{partes.netloc}" if partes.netloc else ""
    if not origen:
        return True, ""
    origen_norm = origen.rstrip("/").lower()
    if origen_norm == "null":
        return False, origen_norm
    if origen_norm in _ORIGENES_PERMITIDOS:
        return True, origen_norm
    return _host_de(origen) == (request.host or "").lower(), origen_norm


@app.before_request
def _exigir_same_origin():
    """Reject POST/PUT/DELETE whose ``Origin``/``Referer`` points elsewhere.

    Browsers always send ``Origin`` on cross-origin requests; a host that does
    not match the panel is a CSRF attempt against the user's session.
    Non-browser clients (no header) pass.

    Returns:
        ``None`` when the request may proceed, otherwise the 403 ``Response``.
    """
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    ok, origen = _origen_permitido()
    if ok:
        return None
    print(
        t(
            f"[web] origen rechazado: {origen} (host {request.host})",
            f"[web] origin rejected: {origen} (host {request.host})",
        ),
    )
    return jsonify(
        {
            "error": f"origen no permitido (se esperaba {request.host})",
        }
    ), 403


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _directorio_candidaturas():
    """Return the directory where generate.py drops the deliverables.

    Returns:
        The absolute path of the materials directory.
    """
    return generate_mod._DIR_CANDIDATURAS


def _rutas_materiales(id_):
    """Return the existing paths of cv.pdf / cover.pdf / cover.txt.

    Args:
        id_: Offer id.

    Returns:
        A dict mapping ``cv_pdf``, ``cover_pdf`` and ``cover_txt`` to a path or
        ``None`` when the file does not exist.
    """
    directorio = generate_mod._directorio_candidatura({"id": id_})
    rutas = {}
    for clave, nombre in (
        ("cv_pdf", "cv.pdf"),
        ("cover_pdf", "cover.pdf"),
        ("cover_txt", "cover.txt"),
    ):
        ruta = os.path.join(directorio, nombre)
        rutas[clave] = ruta if os.path.isfile(ruta) else None
    return rutas


def _url_formulario(url):
    """Check whether the URL points to an ATS form this service can parse.

    Args:
        url: Offer URL.

    Returns:
        ``True`` when the URL matches one of ``_DOMINIOS_FORMULARIO``.
    """
    u = (url or "").lower()
    return bool(u) and any(d in u for d in _DOMINIOS_FORMULARIO)


def _respuestas_formulario(op):
    """Parse the ATS form and draft answers for it. Best-effort.

    Args:
        op: Offer dict with at least ``id`` and ``url``.

    Returns:
        A ``(detalle, error)`` tuple, where ``detalle`` is a list of
        ``{"campo", "pregunta", "respuesta"}`` dicts. Never raises: a browser
        failure must not break the approval.
    """
    if not _url_formulario(op.get("url")):
        return {}, None

    clave = "{}|{}".format(op.get("id"), op.get("url"))
    with _LOCK_RESPUESTAS:
        if clave in _CACHE_RESPUESTAS:
            return _CACHE_RESPUESTAS[clave], None

    try:
        import apply_form as af

        formulario = af.analizar_formulario(op["url"])
        if not formulario:
            return {}, None
        perfil = notify_mod._cargar_perfil()
        respuestas = af.redactar_respuestas(formulario, perfil, op) or {}

        # key -> question text, so the panel can show a readable label.
        pregunta_de = {}
        for campo in formulario:
            k = (campo.get("nombre") or campo.get("pregunta") or "").strip()
            if k:
                pregunta_de[k] = (campo.get("pregunta") or campo.get("etiqueta") or "").strip()
        detalle = [
            {"campo": k, "pregunta": pregunta_de.get(k) or k, "respuesta": v}
            for k, v in respuestas.items()
        ]
    except Exception as err:  # noqa: BLE001 — best-effort.
        print(
            t(
                "[web] formulario fallido para {}: {}".format(op.get("id"), err),
                "[web] form failed for {}: {}".format(op.get("id"), err),
            )
        )
        return {}, str(err)

    with _LOCK_RESPUESTAS:
        _CACHE_RESPUESTAS[clave] = detalle
    return detalle, None


def _generar_pack(op):
    """Generate the missing materials and persist them (same as notify).

    Reuses ``notify._cargar_perfil`` and ``generate.*``. ``generate`` stores the
    PDF paths in ``op['cv_pdf']`` / ``op['cover_pdf']``.

    Args:
        op: Offer dict; ``id`` and any already generated material are reused.

    Returns:
        A dict with the texts, the paths and the list of errors.
    """
    perfil = notify_mod._cargar_perfil()
    errores = []

    def _gen(func, etiqueta):
        try:
            return func(op, perfil)
        except Exception as err:  # noqa: BLE001 — one material does not sink the rest.
            print(
                t(
                    f"[web] generación fallida ({etiqueta}): {err}",
                    f"[web] generation failed ({etiqueta}): {err}",
                ),
            )
            errores.append(f"{etiqueta}: {err}")
            return ""

    cover = op.get("cover_letter") or _gen(generate_mod.generar_cover_letter, "cover_letter")
    cv = op.get("cv_adaptado") or _gen(generate_mod.generar_cv_adaptado, "cv")
    mensaje = op.get("mensaje") or _gen(generate_mod.generar_mensaje, "mensaje")

    try:
        db.guardar_materiales(op["id"], cover, cv, mensaje)
    except Exception as err:  # noqa: BLE001 — persisting is secondary.
        print(
            t(
                "[web] no se guardaron los materiales de {}: {}".format(op["id"], err),
                "[web] materials for {} were not saved: {}".format(op["id"], err),
            )
        )
        errores.append(f"guardar_materiales: {err}")

    # cover.txt next to the PDFs (the Telegram flow attaches it).
    cover_txt = None
    if cover:
        try:
            directorio = generate_mod._directorio_candidatura(op)
            os.makedirs(directorio, exist_ok=True)
            cover_txt = os.path.join(directorio, "cover.txt")
            with open(cover_txt, "w", encoding="utf-8") as fh:
                fh.write(cover)
        except Exception as err:  # noqa: BLE001 — the .txt is a nice-to-have.
            print(
                t(
                    f"[web] no se escribió cover.txt: {err}",
                    f"[web] cover.txt was not written: {err}",
                ),
            )
            errores.append(f"cover.txt: {err}")
            cover_txt = None

    rutas = _rutas_materiales(op["id"])
    return {
        "cover_letter": cover,
        "cv_adaptado": cv,
        "mensaje": mensaje,
        "cv_pdf": rutas["cv_pdf"] or op.get("cv_pdf"),
        "cover_pdf": rutas["cover_pdf"] or op.get("cover_pdf"),
        "cover_txt": rutas["cover_txt"] or cover_txt,
        "errores": errores,
    }


def _enlaces(id_, rutas):
    """Build panel URLs to download the existing deliverables.

    Args:
        id_: Offer id.
        rutas: Output of ``_rutas_materiales``.

    Returns:
        A dict mapping each material key to its download URL.
    """
    enlaces = {}
    for clave in ("cv_pdf", "cover_pdf", "cover_txt"):
        ruta = rutas.get(clave)
        if ruta:
            enlaces[clave] = f"/candidaturas/{_id_url(id_)}/{os.path.basename(ruta)}"
    return enlaces


def _id_url(id_):
    """Sanitize the id the same way ``generate._directorio_candidatura`` does.

    Args:
        id_: Raw offer id.

    Returns:
        A filesystem-safe id, never empty.
    """
    ident = str(id_ or "").strip() or "sin-id"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", ident).strip("._") or "sin-id"


def _filtro_estado(valor):
    """Translate the panel filter into a database state.

    Args:
        valor: Raw ``?estado=`` value.

    Returns:
        ``None`` for all offers, ``"pendientes"``, a concrete state, or the
        trimmed value when it is not a known alias.
    """
    v = (valor or "").strip().lower()
    if not v or v in ("todas", "todos", "all"):
        return None
    if v in ("pendiente", "pendientes", "para_confirmar"):
        return "pendientes"
    if v in ("aprobada", "aprobadas"):
        return ESTADO_APROBADA
    if v in ("descartada", "descartadas"):
        return ESTADO_DESCARTADA
    return v


def _resumen(op):
    """Build the lightweight row used by the panel list.

    Args:
        op: Full offer dict.

    Returns:
        A dict with the fields the list needs.
    """
    return {
        "id": op.get("id"),
        "titulo": op.get("titulo"),
        "empresa": op.get("empresa"),
        "score": op.get("score"),
        "estado": op.get("estado"),
        "ubicacion": op.get("ubicacion"),
        "salario": op.get("salario"),
        "remoto": op.get("remoto"),
        "fecha": op.get("created_at"),
        "url": op.get("url"),
        "tiene_materiales": bool(op.get("cover_letter") or op.get("mensaje")),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.after_request
def _no_cache_api(resp):
    """Mark API responses as non-cacheable so the panel sees changes at once.

    Args:
        resp: Flask response being returned.

    Returns:
        The same response, with ``Cache-Control: no-store`` on ``/api/``.
    """
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def panel():
    """Serve the single-page panel.

    Returns:
        The panel HTML, or a 500 ``Response`` when index.html is missing.
    """
    if not os.path.isfile(INDEX_HTML):
        return Response("Falta service/web/index.html", 500, mimetype="text/plain")
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/api/ofertas")
def api_ofertas():
    """List offers, optionally filtered by state.

    Query params:
        estado: ``pendientes`` | ``aprobadas`` | ``descartadas`` | ``todas``.

    Returns:
        JSON ``{"ofertas": [...], "total": n}``, or 500 on database errors.
    """
    filtro = _filtro_estado(request.args.get("estado"))
    try:
        if filtro == "pendientes":
            ofertas = [op for op in db.listar() if (op.get("estado") or "") in _ESTADOS_PENDIENTES]
        else:
            ofertas = db.listar(filtro)
    except Exception as err:  # noqa: BLE001 — surface a readable DB error.
        return jsonify({"error": f"no se pudo leer la base: {err}"}), 500

    # Highest score first; ties keep the created_at order from db.listar.
    ofertas.sort(key=lambda o: (-(o.get("score") or 0),))
    return jsonify({"ofertas": [_resumen(o) for o in ofertas], "total": len(ofertas)})


def _justificacion(op):
    """Split the JSON justification into reasons and gaps.

    Args:
        op: Offer dict with a ``justificacion`` JSON string.

    Returns:
        A ``(motivos, gaps)`` tuple, empty lists when unparsable.
    """
    try:
        datos = json.loads(op.get("justificacion") or "{}")
    except Exception:  # noqa: BLE001
        datos = {}
    return datos.get("motivos") or [], datos.get("gaps") or []


@app.get("/api/oferta/<path:id_>")
def api_oferta(id_):
    """Return the full detail of one offer with its materials.

    Args:
        id_: Offer id.

    Returns:
        The offer as JSON, or 404 when it does not exist.
    """
    op = db.get(id_)
    if not op:
        return jsonify({"error": "oferta no encontrada"}), 404
    rutas = _rutas_materiales(id_)
    detalle = dict(op)
    detalle["requisitos"] = op.get("requisitos")
    detalle["motivos"], detalle["gaps"] = _justificacion(op)
    detalle["cv_pdf"] = rutas["cv_pdf"]
    detalle["cover_pdf"] = rutas["cover_pdf"]
    detalle["cover_txt"] = rutas["cover_txt"]
    detalle["enlaces"] = _enlaces(id_, rutas)
    detalle["formulario_ats"] = _url_formulario(op.get("url"))
    return jsonify(detalle)


@app.post("/api/oferta/<path:id_>/aprobar")
def api_aprobar(id_):
    """Approve the offer and return the materials pack.

    Never applies on the user's behalf: it only delivers the cover letter, CV,
    message and, for a known ATS, the drafted form answers.

    Args:
        id_: Offer id.

    Query params:
        formulario: ``0`` skips the ATS form analysis.

    Returns:
        The pack as JSON, 404 when the offer does not exist, or 500 when the
        state could not be updated.
    """
    op = db.get(id_)
    if not op:
        return jsonify({"error": "oferta no encontrada"}), 404

    if not db.set_estado(id_, ESTADO_APROBADA):
        return jsonify({"error": "no se pudo actualizar el estado"}), 500

    pack = _generar_pack(op)

    respuestas, error_formulario = ({}, None)
    if request.args.get("formulario", "1") != "0":
        respuestas, error_formulario = _respuestas_formulario(op)
    if error_formulario:
        pack["errores"].append(f"formulario: {error_formulario}")

    rutas = {
        "cv_pdf": pack.get("cv_pdf"),
        "cover_pdf": pack.get("cover_pdf"),
        "cover_txt": pack.get("cover_txt"),
    }
    return jsonify(
        {
            "id": id_,
            "estado": ESTADO_APROBADA,
            "cover_letter": pack.get("cover_letter") or "",
            "cv_adaptado": pack.get("cv_adaptado") or "",
            "mensaje": pack.get("mensaje") or "",
            "cv_pdf": pack.get("cv_pdf"),
            "cover_pdf": pack.get("cover_pdf"),
            "cover_txt": pack.get("cover_txt"),
            "enlaces": _enlaces(id_, rutas),
            "respuestas": respuestas,
            "errores": pack.get("errores") or [],
            "url": op.get("url"),
        }
    )


@app.post("/api/oferta/<path:id_>/descartar")
def api_descartar(id_):
    """Mark the offer as discarded.

    Args:
        id_: Offer id.

    Returns:
        JSON with the new state, 404 when missing, 500 on update failure.
    """
    if not db.get(id_):
        return jsonify({"error": "oferta no encontrada"}), 404
    if not db.set_estado(id_, ESTADO_DESCARTADA):
        return jsonify({"error": "no se pudo actualizar el estado"}), 500
    return jsonify({"id": id_, "estado": ESTADO_DESCARTADA})


# ---------------------------------------------------------------------------
# Profile and questions (application assistant)
# ---------------------------------------------------------------------------

# Fixed assistant instructions: the profile is the only source of truth, human
# tone, no AI clichés. The language follows each question.
SYSTEM_PREGUNTAR = (
    "Eres el asistente de candidatura del candidato. Respondes preguntas de "
    "formularios de candidatura "
    "con DOS fuentes y nada más: (1) el perfil (data/perfil.json) y su CV base, "
    "para todo lo relativo al candidato; (2) el contexto de la empresa y la "
    "oferta que se te pase, para todo lo relativo a la empresa o al puesto. "
    "NO inventes experiencia, empresas, métricas, fechas ni idiomas del "
    "candidato, y NO inventes nada sobre la empresa o el puesto: si una "
    "pregunta se refiere a la empresa o al rol y NO hay contexto de oferta, "
    "dilo claramente ('necesito el contexto de la oferta para responder') en "
    "vez de inventar. Escribe como una persona real y directa: frases cortas, "
    "cero clichés de IA (nada de 'I am excited', 'passionate', 'delve', "
    "'leveraging', 'dive into', 'As a professional...'). Si un dato del "
    "candidato no está en el perfil, dilo o márcalo PEND. Responde en el MISMO "
    "idioma de cada pregunta."
)

# Token budget per question (reasoning models spend part of it before
# answering; generate._chat retries with double the budget when exhausted).
_MAX_TOKENS_PREGUNTA = 1500
_MAX_PREGUNTAS = 12


def _perfil_texto(perfil):
    """Render the profile as readable JSON for the prompt.

    Args:
        perfil: Profile dict.

    Returns:
        The profile serialized with indentation.
    """
    return json.dumps(perfil, ensure_ascii=False, indent=1)


def _responder_pregunta(pregunta, contexto):
    """Answer a single question with one LLM call.

    Retries once with double the budget when the model ran out of
    ``max_tokens`` while reasoning, as ``generate._chat`` does.

    Args:
        pregunta: Question text.
        contexto: Shared context (offer/company and profile).

    Returns:
        The stripped answer, or ``""`` when the model returned nothing.
    """
    mensajes = [
        {"role": "system", "content": SYSTEM_PREGUNTAR},
        {
            "role": "user",
            "content": f"{contexto}\n\nPregunta a responder:\n{pregunta}",
        },
    ]
    max_tokens = _MAX_TOKENS_PREGUNTA
    try:
        respuesta = llm.chat(mensajes, temperature=0.6, max_tokens=max_tokens)
    except llm.LLMError as err:
        if "max_tokens" not in str(err) and "vacío" not in str(err):
            raise
        respuesta = llm.chat(mensajes, temperature=0.6, max_tokens=max_tokens * 2)
    return (respuesta or "").strip()


def _normalizar_preguntas(datos):
    """Accept ``{preguntas: [...]}`` or ``{pregunta: "..."}`` (with newlines).

    Args:
        datos: Request body.

    Returns:
        A list of at most ``_MAX_PREGUNTAS`` non-empty questions.
    """
    crudo = datos.get("preguntas")
    if crudo is None:
        crudo = datos.get("pregunta")

    if isinstance(crudo, str):
        piezas = crudo.splitlines()
    elif isinstance(crudo, (list, tuple)):
        piezas = []
        for item in crudo:
            if isinstance(item, str):
                piezas.extend(item.splitlines())
    else:
        piezas = []

    preguntas = [p.strip() for p in piezas if (p or "").strip()]
    return preguntas[:_MAX_PREGUNTAS]


@app.get("/api/perfil")
def api_perfil():
    """Return the full profile from ``data/perfil.json``.

    Returns:
        The profile as JSON, or 500 when it cannot be loaded or is not an
        object.
    """
    try:
        perfil = notify_mod._cargar_perfil()
    except Exception as err:  # noqa: BLE001 — surface a readable error.
        return jsonify({"error": f"no se pudo cargar el perfil: {err}"}), 500
    if not isinstance(perfil, dict):
        return jsonify({"error": "el perfil no es un objeto JSON"}), 500
    return jsonify(perfil)


# ---------------------------------------------------------------------------
# Profile editing (free text -> validated changes, and deletion)
# ---------------------------------------------------------------------------

# Top-level keys the editor may touch. ``fuentes`` is excluded on purpose: it is
# provenance metadata, not a fact about the candidate.
CLAVES_PERFIL = (
    "identity",
    "headline",
    "positioning_emphasis",
    "experience",
    "projects",
    "education",
    "languages",
    "certifications",
    "skills",
    "compensation",
    "search_criteria",
    "pendientes",
)

# Serializes the read-modify-write of perfil.json: concurrent edits must not
# overwrite each other or leave a half-written file.
_LOCK_PERFIL = threading.Lock()

SYSTEM_PERFIL = (
    "Eres el curador del perfil del candidato. "
    "Recibes el perfil actual (JSON) y un texto nuevo del usuario. "
    "Devuelve SOLO JSON, sin comentarios: "
    '{"cambios": [{"tipo": "set"|"agregar"|"quitar", '
    '"ruta": "identity.telefono"|"experience"|"skills.frontend"|..., '
    '"valor": <JSON>, "evidencia": "USR"}], "nota": "explicación breve"}. '
    "ruta usa puntos para dicts e índice numérico para listas (p.ej. experience.2). "
    "'set' cambia un valor existente, 'agregar' añade un elemento a una lista "
    "(valor = item completo), 'quitar' elimina. NO inventes nada que no esté en "
    "el texto del usuario; evidencia siempre 'USR'. Si el texto no aporta un dato "
    'concreto, devuelve {"cambios": [], "nota": \'...\'}.'
)


def _ruta_perfil():
    """Return the absolute path of ``data/perfil.json``.

    Resolved the same way notify does.

    Returns:
        The absolute path of the profile file.
    """
    cfg = config.load_config(estricto=False)
    ruta = cfg.get("PERFIL_PATH") or "../data/perfil.json"
    if not os.path.isabs(ruta):
        ruta = os.path.normpath(os.path.join(BASE_DIR, ruta))
    return ruta


def _texto_ruta(tokens):
    """Render a path from its tokens.

    Args:
        tokens: Path tokens, e.g. ``["experience", 2]``.

    Returns:
        The dotted path.
    """
    return ".".join(str(t) for t in tokens)


def _tokens_ruta(ruta):
    """Split ``identity.telefono`` / ``experience.2`` into keys and indices.

    Args:
        ruta: Dotted path.

    Returns:
        A list of string keys and integer indices.

    Raises:
        ValueError: If the path is not a string, is empty, has an empty
            segment, or starts with an unknown top-level key.
    """
    if not isinstance(ruta, str):
        raise ValueError("'ruta' debe ser texto")
    limpia = ruta.strip()
    if not limpia:
        raise ValueError("'ruta' está vacía")
    tokens = []
    for parte in limpia.split("."):
        pieza = parte.strip()
        if not pieza:
            raise ValueError(f"la ruta '{limpia}' tiene un tramo vacío")
        tokens.append(int(pieza) if re.fullmatch(r"\d+", pieza) else pieza)
    if tokens[0] not in CLAVES_PERFIL:
        raise ValueError(
            "clave desconocida '{}'; válidas: {}".format(tokens[0], ", ".join(CLAVES_PERFIL))
        )
    return tokens


def _padre_de(perfil, tokens):
    """Return the container (dict or list) that holds the path.

    The last token is not checked here: each change type decides that.

    Args:
        perfil: Profile dict.
        tokens: Path tokens.

    Returns:
        The container of the last token.

    Raises:
        ValueError: If an intermediate segment is missing, out of range, or
            does not hold more keys.
    """
    actual = perfil
    for i, token in enumerate(tokens[:-1]):
        previa = _texto_ruta(tokens[: i + 1])
        contenedor = _texto_ruta(tokens[:i]) or "el perfil"
        if isinstance(token, int):
            if not isinstance(actual, list):
                raise ValueError(f"'{contenedor}' no es una lista")
            if not 0 <= token < len(actual):
                raise ValueError(f"índice fuera de rango en '{previa}'")
            actual = actual[token]
        else:
            if not isinstance(actual, dict):
                raise ValueError(f"'{contenedor}' no es un objeto")
            if token not in actual:
                raise ValueError(f"no existe '{previa}'")
            actual = actual[token]
    if not isinstance(actual, (dict, list)):
        raise ValueError("'%s' no contiene más claves" % (_texto_ruta(tokens[:-1]) or "el perfil"))
    return actual


def _resolver_ruta(perfil, tokens):
    """Return the value at the path.

    Args:
        perfil: Profile dict.
        tokens: Path tokens.

    Returns:
        The existing value at the path.

    Raises:
        ValueError: If the path does not exist or has a key/index mismatch.
    """
    padre = _padre_de(perfil, tokens)
    clave = tokens[-1]
    if isinstance(padre, list):
        if not isinstance(clave, int) or not 0 <= clave < len(padre):
            raise ValueError(f"índice fuera de rango en '{_texto_ruta(tokens)}'")
        return padre[clave]
    if isinstance(clave, int):
        raise ValueError(f"'{_texto_ruta(tokens[:-1])}' es un objeto, no una lista")
    if clave not in padre:
        raise ValueError(f"no existe '{_texto_ruta(tokens)}'")
    return padre[clave]


def _aplicar_cambio(perfil, cambio):
    """Validate and apply one LLM change to ``perfil`` (already a copy).

    Args:
        perfil: Profile copy to mutate.
        cambio: Change dict with ``tipo`` (set/agregar/quitar), ``ruta`` and,
            except for ``quitar``, ``valor``.

    Returns:
        The applied change summary for the response.

    Raises:
        ValueError: If the change is invalid; the caller then discards the copy
            and answers 400.
    """
    if not isinstance(cambio, dict):
        raise ValueError("el cambio no es un objeto JSON")

    tipo = str(cambio.get("tipo") or "").strip().lower()
    if tipo not in ("set", "agregar", "quitar"):
        raise ValueError("tipo '{}' no válido (set, agregar, quitar)".format(cambio.get("tipo")))

    tokens = _tokens_ruta(cambio.get("ruta"))
    valor = cambio.get("valor")
    if tipo != "quitar":
        if "valor" not in cambio:
            raise ValueError(f"falta 'valor' en un cambio '{tipo}'")
        try:
            json.dumps(valor, ensure_ascii=False)
        except (TypeError, ValueError):
            raise ValueError("'valor' no es JSON serializable") from None

    if tipo == "quitar":
        padre = _padre_de(perfil, tokens)
        clave = tokens[-1]
        if isinstance(padre, list):
            if not isinstance(clave, int) or not 0 <= clave < len(padre):
                raise ValueError(f"índice fuera de rango en '{_texto_ruta(tokens)}'")
            del padre[clave]
        else:
            if isinstance(clave, int) or clave not in padre:
                raise ValueError(f"no existe '{_texto_ruta(tokens)}'")
            del padre[clave]

    elif tipo == "agregar":
        destino = _resolver_ruta(perfil, tokens)
        if not isinstance(destino, list):
            raise ValueError(f"'agregar' necesita una lista en '{_texto_ruta(tokens)}'")
        destino.append(valor)

    else:  # set: replace an existing key (or add it to a dict)
        padre = _padre_de(perfil, tokens)
        clave = tokens[-1]
        if isinstance(padre, list):
            if not isinstance(clave, int) or not 0 <= clave < len(padre):
                raise ValueError(f"índice fuera de rango en '{_texto_ruta(tokens)}'")
            padre[clave] = valor
        else:
            if isinstance(clave, int):
                raise ValueError(f"'{_texto_ruta(tokens[:-1])}' es un objeto, no una lista")
            padre[clave] = valor

    # Everything that arrives here comes from the user (USR): the prompt already
    # demands it and it is normalized so model-invented tags are never stored
    # (DOC/VIS/PEND do not apply to a fact the user declared).
    return {
        "tipo": tipo,
        "ruta": _texto_ruta(tokens),
        "valor": valor,
        "evidencia": "USR",
    }


def _parsear_json_modelo(texto):
    """Parse the model JSON, tolerating fences and surrounding text.

    Args:
        texto: Raw model output.

    Returns:
        The parsed JSON value.

    Raises:
        ValueError: If the output is empty or not valid JSON.
    """
    crudo = (texto or "").strip()
    if not crudo:
        raise ValueError("el modelo devolvió una respuesta vacía")
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", crudo, flags=re.DOTALL)
    if fence:
        crudo = fence.group(1).strip()
    inicio, fin = crudo.find("{"), crudo.rfind("}")
    if inicio != -1 and fin > inicio:
        crudo = crudo[inicio : fin + 1]
    try:
        return json.loads(crudo)
    except json.JSONDecodeError as err:
        raise ValueError(f"la respuesta del modelo no es JSON válido: {err}") from err


def _guardar_perfil(ruta, perfil):
    """Back up the current profile and write the new one atomically.

    The JSON goes to a temp file in the same directory and is published with
    ``os.replace``: if the process dies halfway the original stays intact and
    ``perfil.json.bak`` holds the previous version.

    Args:
        ruta: Absolute path of the profile file.
        perfil: Profile dict to write.
    """
    directorio = os.path.dirname(ruta) or "."
    modo = None
    if os.path.isfile(ruta):
        shutil.copy2(ruta, ruta + ".bak")
        modo = os.stat(ruta).st_mode & 0o777
    descriptor, temporal = tempfile.mkstemp(prefix=".perfil-", suffix=".tmp", dir=directorio)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
            json.dump(perfil, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if modo is not None:
            os.chmod(temporal, modo)
        os.replace(temporal, ruta)
    except BaseException:
        try:
            os.unlink(temporal)
        except OSError:
            pass
        raise


def _validar_claves_perfil(nuevo, actual):
    """Validate the top-level keys of a full profile.

    Shared by the technical-mode PUT and ``POST /api/import`` so both paths
    accept exactly the same: the editable keys (``CLAVES_PERFIL``) plus the ones
    the profile already had (e.g. ``fuentes``).

    Args:
        nuevo: Candidate profile object.
        actual: Current profile, or ``None``/``{}`` when there is none.

    Returns:
        An error message, or ``None`` when the profile is valid.
    """
    if not isinstance(nuevo, dict):
        return "falta 'perfil' (objeto JSON completo)"
    if any(not isinstance(k, str) for k in nuevo):
        return "las claves del perfil deben ser texto"

    permitidas = set(CLAVES_PERFIL) | set(actual or {})
    desconocidas = [k for k in nuevo if k not in permitidas]
    if desconocidas:
        return "clave(s) no reconocida(s): {}. Válidas: {}".format(
            ", ".join(desconocidas), ", ".join(sorted(permitidas))
        )
    return None


def _perfil_para_editar():
    """Load the profile and its path.

    Returns:
        A ``(perfil, ruta)`` tuple.

    Raises:
        RuntimeError: If the profile is not a JSON object.
    """
    ruta = _ruta_perfil()
    perfil = notify_mod._cargar_perfil()
    if not isinstance(perfil, dict):
        raise RuntimeError("el perfil no es un objeto JSON")
    return perfil, ruta


@app.post("/api/perfil/agregar")
def api_perfil_agregar():
    """Merge a fact written by the user into the profile.

    The LLM proposes ``set``/``agregar``/``quitar`` changes on profile paths;
    each is validated here and nothing is written unless all of them are valid
    (400 otherwise, file untouched).

    Body:
        ``{"texto": "..."}``, at most 4000 characters.

    Returns:
        ``{"ok": true, "cambios": [...], "nota": "..."}``, or 400/500/502 with
        an ``error`` message.
    """
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return jsonify({"error": "el body debe ser un objeto JSON"}), 400
    texto = (datos.get("texto") or "").strip()
    if not texto:
        return jsonify({"error": "falta 'texto' con el dato a añadir"}), 400
    if len(texto) > 4000:
        return jsonify({"error": "'texto' es demasiado largo (máx. 4000 caracteres)"}), 400

    # The lock also covers the LLM call: the plan is computed against the
    # profile that will be written, so concurrent edits cannot interleave.
    with _LOCK_PERFIL:
        try:
            perfil, ruta = _perfil_para_editar()
        except Exception as err:  # noqa: BLE001
            return jsonify({"error": f"no se pudo cargar el perfil: {err}"}), 500

        mensajes = [
            {"role": "system", "content": SYSTEM_PERFIL},
            {
                "role": "user",
                "content": (
                    f"PERFIL ACTUAL (data/perfil.json):\n{_perfil_texto(perfil)}\n\n"
                    f"TEXTO NUEVO DEL USUARIO:\n{texto}"
                ),
            },
        ]
        try:
            respuesta = llm.chat(mensajes, temperature=0.1, max_tokens=2000, json_mode=True)
        except llm.LLMError as err:
            return jsonify({"error": f"el LLM no pudo analizar el texto: {err}"}), 502
        except Exception as err:  # noqa: BLE001 — surface a readable error.
            return jsonify({"error": f"el LLM no pudo analizar el texto: {err}"}), 502

        try:
            plan = _parsear_json_modelo(respuesta)
        except ValueError as err:
            return jsonify({"error": str(err)}), 502

        if not isinstance(plan, dict):
            return jsonify({"error": "el modelo no devolvió un objeto JSON"}), 502

        cambios = plan.get("cambios")
        if cambios is None:
            cambios = []
        if not isinstance(cambios, list):
            return jsonify({"error": "'cambios' debe ser una lista"}), 502
        nota = str(plan.get("nota") or "").strip()

        copia = copy.deepcopy(perfil)
        aplicados = []
        for posicion, cambio in enumerate(cambios, start=1):
            try:
                # Applied to the copy: if change n leaves the profile in a bad
                # state and n+1 fails, nothing is written.
                aplicados.append(_aplicar_cambio(copia, cambio))
            except ValueError as err:
                return jsonify(
                    {
                        "error": f"cambio {posicion} no válido: {err}",
                    }
                ), 400

        if not aplicados:
            return jsonify(
                {
                    "ok": True,
                    "cambios": [],
                    "nota": nota or "El texto no aporta ningún dato concreto; no se cambió nada.",
                }
            )

        try:
            _guardar_perfil(ruta, copia)
        except Exception as err:  # noqa: BLE001
            print(
                t(
                    f"[web] no se pudo escribir el perfil: {err}",
                    f"[web] could not write the profile: {err}",
                ),
            )
            return jsonify({"error": f"no se pudo guardar el perfil: {err}"}), 500

    cambios_txt = ", ".join("{} {}".format(c["tipo"], c["ruta"]) for c in aplicados)
    print(
        t(
            f"[web] perfil actualizado ({len(aplicados)} cambio(s)): {cambios_txt}",
            f"[web] profile updated ({len(aplicados)} change(s)): {cambios_txt}",
        ),
    )
    return jsonify({"ok": True, "cambios": aplicados, "nota": nota})


@app.post("/api/perfil/borrar")
def api_perfil_borrar():
    """Delete a profile key or index (with backup).

    Body:
        ``{"ruta": "certifications"}`` or ``{"ruta": "experience.2"}``.

    Returns:
        ``{"ok": true, "ruta": ...}``, or 400 when the path does not exist or
        cannot be deleted, 500 when saving fails.
    """
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return jsonify({"error": "el body debe ser un objeto JSON"}), 400
    ruta_usuario = (datos.get("ruta") or "").strip()
    if not ruta_usuario:
        return jsonify({"error": "falta 'ruta' (clave o índice a borrar)"}), 400

    with _LOCK_PERFIL:
        try:
            perfil, ruta_fichero = _perfil_para_editar()
        except Exception as err:  # noqa: BLE001
            return jsonify({"error": f"no se pudo cargar el perfil: {err}"}), 500

        copia = copy.deepcopy(perfil)
        try:
            borrado = _aplicar_cambio(copia, {"tipo": "quitar", "ruta": ruta_usuario})
        except ValueError as err:
            return jsonify({"error": str(err)}), 400

        try:
            _guardar_perfil(ruta_fichero, copia)
        except Exception as err:  # noqa: BLE001
            print(
                t(
                    f"[web] no se pudo escribir el perfil: {err}",
                    f"[web] could not write the profile: {err}",
                ),
            )
            return jsonify({"error": f"no se pudo guardar el perfil: {err}"}), 500

    print(
        t(
            "[web] perfil: borrado {}".format(borrado["ruta"]),
            "[web] profile: deleted {}".format(borrado["ruta"]),
        )
    )
    return jsonify({"ok": True, "ruta": borrado["ruta"]})


@app.put("/api/perfil")
def api_perfil_put():
    """Replace the whole profile with the edited JSON (technical mode).

    Body:
        ``{"perfil": {...}}``. Validates the object and its top-level keys (the
        editable ones plus the existing ones, e.g. ``fuentes``), then writes it
        atomically after a backup.

    Returns:
        ``{"ok": true}``, or 400/500 with an ``error`` message.
    """
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return jsonify({"error": "el body debe ser un objeto JSON"}), 400
    nuevo = datos.get("perfil")
    if not isinstance(nuevo, dict):
        return jsonify({"error": "falta 'perfil' (objeto JSON completo)"}), 400
    if any(not isinstance(k, str) for k in nuevo):
        return jsonify({"error": "las claves del perfil deben ser texto"}), 400

    with _LOCK_PERFIL:
        try:
            actual, ruta = _perfil_para_editar()
        except Exception as err:  # noqa: BLE001
            return jsonify({"error": f"no se pudo cargar el perfil: {err}"}), 500

        error = _validar_claves_perfil(nuevo, actual)
        if error:
            return jsonify({"error": error}), 400

        try:
            _guardar_perfil(ruta, nuevo)
        except Exception as err:  # noqa: BLE001
            print(
                t(
                    f"[web] no se pudo escribir el perfil (PUT): {err}",
                    f"[web] could not write the profile (PUT): {err}",
                ),
            )
            return jsonify({"error": f"no se pudo guardar el perfil: {err}"}), 500

    print(
        t(
            "[web] perfil reemplazado vía JSON (modo técnico)",
            "[web] profile replaced via JSON (technical mode)",
        )
    )
    return jsonify({"ok": True})


@app.post("/api/preguntar")
def api_preguntar():
    """Answer application questions with the profile and the LLM.

    Body:
        ``{"preguntas": [...]}`` or ``{"pregunta": "..."}``, plus optional
        ``empresa`` (str) and ``contexto``/``oferta`` (job or company text).

    Returns:
        ``{"respuestas": [...], "preguntas": [...], "errores": [...]}`` with one
        answer per question in the same order, or 400/500/502 with ``error``.
    """
    datos = request.get_json(silent=True) or {}
    if not isinstance(datos, dict):
        return jsonify({"error": "el body debe ser un objeto JSON"}), 400

    preguntas = _normalizar_preguntas(datos)
    if not preguntas:
        return jsonify({"error": "falta 'preguntas' (lista) o 'pregunta'"}), 400

    try:
        perfil = notify_mod._cargar_perfil()
    except Exception as err:  # noqa: BLE001
        return jsonify({"error": f"no se pudo cargar el perfil: {err}"}), 500

    empresa = (datos.get("empresa") or "").strip()
    oferta = (datos.get("contexto") or datos.get("oferta") or "").strip()

    partes = []
    if oferta:
        partes.append(
            "CONTEXTO DE LA OFERTA / EMPRESA (pegado por el usuario; fuente para "
            f"lo relativo a la empresa y al puesto):\n{oferta}"
        )
    elif empresa:
        partes.append(f"EMPRESA: {empresa}")
    partes.append(
        "PERFIL DEL CANDIDATO (data/perfil.json, única fuente de verdad sobre "
        f"el candidato):\n{_perfil_texto(perfil)}"
    )
    contexto = "\n\n".join(partes)

    respuestas = []
    errores = []
    for pregunta in preguntas:
        try:
            respuestas.append(_responder_pregunta(pregunta, contexto))
        except Exception as err:  # noqa: BLE001 — one question does not sink the rest.
            print(
                t(
                    f"[web] pregunta fallida: {err}",
                    f"[web] question failed: {err}",
                ),
            )
            respuestas.append("")
            errores.append(str(err))

    if not any(respuestas):
        detalle = errores[0] if errores else "sin respuesta del modelo"
        return jsonify(
            {
                "error": f"el LLM no devolvió respuesta: {detalle}",
                "respuestas": respuestas,
                "errores": errores,
            }
        ), 502

    return jsonify({"respuestas": respuestas, "preguntas": preguntas, "errores": errores})


# ---------------------------------------------------------------------------
# Editable configuration (data/settings.json)
# ---------------------------------------------------------------------------


def _config_efectiva():
    """Return the effective config (.env + settings.json) without required keys.

    Returns:
        The merged config dict, or the defaults when loading fails.
    """
    try:
        return config.load_config(estricto=False)
    except Exception as err:  # noqa: BLE001 — the panel must still answer.
        print(
            t(
                f"[web] no se pudo cargar la config: {err}",
                f"[web] could not load the config: {err}",
            ),
        )
        return config.config_por_defecto()


def _respuesta_config(extra=None):
    """Build the shared GET/PUT body: masked config plus secret status.

    Args:
        extra: Optional dict merged into the response body.

    Returns:
        The JSON response.
    """
    cfg = _config_efectiva()
    cuerpo = {
        "config": config.enmascarar_config(cfg),
        "secretos": config.secretos_configurados(cfg),
    }
    if extra:
        cuerpo.update(extra)
    return jsonify(cuerpo)


@app.get("/api/config")
def api_config_get():
    """Return the effective configuration.

    ``config`` holds every value with secrets replaced by ``""``; ``secretos``
    reports which ones are set (True/False) without revealing them.

    Returns:
        The JSON response.
    """
    return _respuesta_config()


@app.put("/api/config")
def api_config_put():
    """Update ``data/settings.json``, validated against the config.py spec.

    Accepts the keys object directly or wrapped in ``{"config": {...}}``.
    Unknown keys and wrong types are rejected (400), as are out-of-range values
    (SCORE_THRESHOLD 0-100, TEMPERATURA 0-2, MIN<=MAX for the doubtful range).
    An empty secret is left untouched. The file is merged with what was already
    there and written atomically; values are never logged.

    Returns:
        The masked config plus ``guardado``, or 400/500 with ``error``.
    """
    datos = request.get_json(silent=True)
    if datos is None:
        return jsonify({"error": "el body debe ser un objeto JSON"}), 400
    if isinstance(datos, dict) and isinstance(datos.get("config"), dict):
        datos = datos["config"]  # also accept {"config": {...}}
    if not isinstance(datos, dict):
        return jsonify({"error": "el body debe ser un objeto JSON"}), 400
    if not datos:
        return jsonify({"error": "no hay claves que actualizar"}), 400

    # 1) Known key and correct type.
    cambios = {}
    for clave, valor in datos.items():
        if clave == "SETTINGS_PATH":
            return jsonify({"error": "SETTINGS_PATH no es editable desde el panel"}), 400
        try:
            cambios[clave] = config.normalizar_valor(clave, valor)
        except KeyError:
            return jsonify(
                {
                    "error": f"clave desconocida: {clave}",
                    "claves": sorted(config.CONFIG_SPEC),
                }
            ), 400
        except ValueError as err:
            return jsonify({"error": str(err)}), 400

    # 2) Secrets: empty value means "do not touch" (the panel never resends them).
    for clave in list(cambios):
        if config.es_secreta(clave) and not cambios[clave]:
            del cambios[clave]

    if not cambios:
        return _respuesta_config({"guardado": []})

    # 3) Ranges checked on the final state (patch plus what is already stored).
    base = _config_efectiva()
    efectivo = {clave: base.get(clave) for clave in config.CONFIG_SPEC}
    efectivo.update(cambios)
    try:
        config.validar_rangos(efectivo)
    except ValueError as err:
        return jsonify({"error": str(err)}), 400

    # 4) Atomic persistence, merged with what was already stored.
    try:
        config.guardar_settings(cambios, ruta=config.ruta_settings(base))
    except Exception as err:  # noqa: BLE001 — surface a readable error.
        return jsonify({"error": f"no se pudo guardar la configuración: {err}"}), 500

    return _respuesta_config({"guardado": sorted(cambios)})


@app.get("/candidaturas/<path:filename>")
def candidaturas(filename):
    """Serve the generated PDFs and cover.txt.

    Args:
        filename: Path relative to the materials directory.

    Returns:
        The requested file, or 404 when the directory does not exist.
    """
    directorio = _directorio_candidaturas()
    if not os.path.isdir(directorio):
        return jsonify({"error": "no hay candidaturas generadas"}), 404
    return send_from_directory(directorio, filename)


@app.get("/healthz")
def healthz():
    """Liveness probe for the compose file.

    Returns:
        ``{"ok": true}``.
    """
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Metrics (read-only)
# ---------------------------------------------------------------------------


def _host_fuente(url):
    """Derive the source name from a URL (no ``www.``, no port).

    Args:
        url: Offer URL.

    Returns:
        The registrable host, or ``"desconocida"``.
    """
    host = _host_de(url or "")
    if not host:
        return "desconocida"
    host = host.split("@")[-1]
    if ":" in host:
        host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    partes = [p for p in host.split(".") if p]
    if len(partes) >= 2 and not re.match(r"^\d+$", partes[-1]):
        return ".".join(partes[-2:])
    return host or "desconocida"


def _ruta_base_lectura():
    """Resolve the database path through db.py without creating or writing it.

    Returns:
        The database path.

    Raises:
        RuntimeError: If ``DB_PATH`` cannot be resolved.
    """
    try:
        return db._resolver_path(None)  # only reads DB_PATH from the config.
    except Exception as err:  # noqa: BLE001 — unreadable config.
        raise RuntimeError(f"no se pudo resolver DB_PATH: {err}") from err


def _metricas_bd():
    """Count offers by state and by source on a read-only connection.

    Returns:
        A dict with ``total``, ``por_estado``, ``por_fuente`` and
        ``ultima_actualizacion``; all zeros/empty when there is no database yet.
    """
    ruta = _ruta_base_lectura()
    vacio = {
        "total": 0,
        "por_estado": {},
        "por_fuente": {},
        "ultima_actualizacion": None,
    }
    if not os.path.isfile(ruta):
        return vacio  # no database yet: the panel can still start.

    # mode=ro: another connection writing does not block this one and is never
    # modified; if the file cannot be opened the error surfaces as a 500.
    uri = "file:{}?mode=ro".format(ruta.replace("?", "%3f").replace("#", "%23"))
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        columnas = {fila[1] for fila in conn.execute("PRAGMA table_info(oportunidades)")}
        if not columnas:
            return vacio

        por_estado = {}
        for estado, n in conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(estado), ''), 'pendiente') AS e, "
            "COUNT(*) FROM oportunidades GROUP BY e ORDER BY e"
        ):
            por_estado[str(estado)] = int(n)

        por_fuente = {}
        if "fuente" in columnas:
            filas = conn.execute(
                "SELECT COALESCE(NULLIF(TRIM(fuente), ''), 'desconocida'), "
                "COUNT(*) FROM oportunidades GROUP BY 1"
            ).fetchall()
        else:
            # The schema has no fuente column: derive it from the url domain.
            filas = []
            for url, n in conn.execute("SELECT url, COUNT(*) FROM oportunidades GROUP BY url"):
                filas.append((_host_fuente(url), n))
            agrupado = {}
            for fuente, n in filas:
                agrupado[fuente] = agrupado.get(fuente, 0) + int(n)
            filas = list(agrupado.items())

        for fuente, n in filas:
            clave = str(fuente or "desconocida")
            por_fuente[clave] = por_fuente.get(clave, 0) + int(n)

        ultima = conn.execute(
            "SELECT MAX(COALESCE(updated_at, created_at)) FROM oportunidades"
        ).fetchone()[0]
    finally:
        conn.close()

    return {
        "total": sum(por_estado.values()),
        "por_estado": por_estado,
        "por_fuente": por_fuente,
        "ultima_actualizacion": ultima,
    }


@app.get("/api/metrics")
def api_metrics():
    """Return the database summary: total, per state, per source, last update.

    Returns:
        The metrics as JSON, or 500 when the database cannot be read.
    """
    try:
        datos = _metricas_bd()
    except Exception as err:  # noqa: BLE001 — surface a readable error.
        return jsonify({"error": f"no se pudieron leer las métricas: {err}"}), 500
    return jsonify(datos)


# ---------------------------------------------------------------------------
# Self-host: health, export/import and source diagnostics
# ---------------------------------------------------------------------------


@app.get("/api/salud")
def api_salud():
    """Run the self-host health diagnostics (passthrough of ``health``).

    Checks the DeepSeek key/endpoint, Telegram ``getMe``, the LinkedIn session,
    writes to ``/data`` and the Chromium binary. It makes network calls (up to
    ~5s per block), which is why ``/healthz`` does not use it: the Docker
    healthcheck must stay instant.

    Returns:
        The diagnostics as JSON, or 500 when they fail to run.
    """
    try:
        cfg = config.load_config(estricto=False)
        return jsonify(health.diagnostico(cfg))
    except Exception as err:  # noqa: BLE001 — surface a readable error.
        print(
            t(
                f"[web] diagnóstico de salud fallido: {err}",
                f"[web] health diagnosis failed: {err}",
            ),
        )
        return jsonify({"error": f"no se pudo ejecutar el diagnóstico: {err}"}), 500


@app.get("/api/export")
def api_export():
    """Export profile + effective settings to migrate between instances.

    ``settings`` ships with secrets replaced by ``""``
    (``config.enmascarar_config``), so the exported file leaks no keys; they
    must be entered again on the target instance.

    Returns:
        ``{"perfil": {...}, "settings": {...}}``, or 500 on load errors.
    """
    try:
        perfil = notify_mod._cargar_perfil()
    except Exception as err:  # noqa: BLE001
        return jsonify({"error": f"no se pudo cargar el perfil: {err}"}), 500
    if not isinstance(perfil, dict):
        return jsonify({"error": "el perfil no es un objeto JSON"}), 500

    try:
        settings = config.enmascarar_config(_config_efectiva())
    except Exception as err:  # noqa: BLE001
        return jsonify({"error": f"no se pudo cargar la configuración: {err}"}), 500

    return jsonify({"perfil": perfil, "settings": settings})


@app.post("/api/import")
def api_import():
    """Restore profile and/or ``settings`` from an export (both optional).

    Body:
        ``{"perfil": {...}, "settings": {...}}``. Everything is validated before
        writing: the profile with the same keys as the technical-mode PUT and
        the settings against the ``config.py`` spec (``SETTINGS_PATH`` is not
        importable because it would redirect its own location). The profile is
        written under ``_LOCK_PERFIL`` with backup and atomically; settings are
        merged with what is already stored. Empty secrets (the export case)
        are left untouched.

    Returns:
        ``{"ok": true, "importado": [...]}``, or 400/500 with ``error``.
    """
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return jsonify({"error": "el body debe ser un objeto JSON"}), 400

    nuevo_perfil = datos.get("perfil")
    nuevos_settings = datos.get("settings")
    if nuevo_perfil is not None and not isinstance(nuevo_perfil, dict):
        return jsonify({"error": "'perfil' debe ser un objeto JSON"}), 400
    if nuevos_settings is not None and not isinstance(nuevos_settings, dict):
        return jsonify({"error": "'settings' debe ser un objeto JSON"}), 400
    if nuevo_perfil is None and nuevos_settings is None:
        return jsonify(
            {
                "error": "no hay nada que importar: faltan 'perfil' y 'settings'",
            }
        ), 400

    # 1) Full validation before writing anything (no half-applied imports).
    ruta_perfil = _ruta_perfil() if nuevo_perfil is not None else ""
    if nuevo_perfil is not None:
        try:
            actual = notify_mod._cargar_perfil()
        except Exception:  # noqa: BLE001 — without a previous profile, validate strict.
            actual = {}
        if not isinstance(actual, dict):
            actual = {}
        error = _validar_claves_perfil(nuevo_perfil, actual)
        if error:
            return jsonify({"error": error}), 400

    cambios = {}
    if nuevos_settings is not None:
        for clave, valor in nuevos_settings.items():
            if clave == "SETTINGS_PATH":
                return jsonify(
                    {
                        "error": "SETTINGS_PATH no es importable desde el panel",
                    }
                ), 400
            if clave not in config.CONFIG_SPEC:
                return jsonify(
                    {
                        "error": f"clave de configuración desconocida: {clave}",
                        "claves": sorted(config.CONFIG_SPEC),
                    }
                ), 400
            if config.es_secreta(clave) and not valor:
                continue  # secret masked by the export: leave it untouched.
            cambios[clave] = valor

    # 2) Write: profile (backup + atomic) and settings (atomic merge).
    importado = []
    if nuevo_perfil is not None:
        with _LOCK_PERFIL:
            try:
                os.makedirs(os.path.dirname(ruta_perfil) or ".", exist_ok=True)
                _guardar_perfil(ruta_perfil, nuevo_perfil)
            except Exception as err:  # noqa: BLE001
                print(
                    t(
                        f"[web] no se pudo importar el perfil: {err}",
                        f"[web] could not import the profile: {err}",
                    ),
                )
                return jsonify({"error": f"no se pudo guardar el perfil: {err}"}), 500
        importado.append("perfil")

    if nuevos_settings is not None and cambios:
        base = _config_efectiva()
        try:
            config.guardar_settings(cambios, ruta=config.ruta_settings(base))
        except Exception as err:  # noqa: BLE001
            print(
                t(
                    f"[web] no se pudo importar la configuración: {err}",
                    f"[web] could not import the configuration: {err}",
                ),
            )
            return jsonify(
                {
                    "error": f"no se pudo guardar la configuración: {err}",
                    "importado": importado,
                }
            ), 500
        importado.append("settings")

    print(
        t("[web] importado: %s", "[web] imported: %s")
        % (", ".join(importado) or t("nada", "nothing"))
    )
    return jsonify({"ok": True, "importado": importado})


@app.post("/api/fuente/probar")
def api_fuente_probar():
    """Probe a board source with a single real request.

    Body:
        ``{"nombre": "workable", "parametros": {"slug": "..."}}``.

    Returns:
        ``{"ok": ..., "detalle": ...}`` from ``search.boards.probar_fuente``;
        network errors are reported in ``detalle``, not raised.
    """
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return jsonify({"error": "el body debe ser un objeto JSON"}), 400
    nombre = str(datos.get("nombre") or "").strip()
    if not nombre:
        return jsonify({"error": "falta 'nombre' (p. ej. \"workable\")"}), 400
    parametros = datos.get("parametros")
    if parametros is not None and not isinstance(parametros, dict):
        return jsonify({"error": "'parametros' debe ser un objeto JSON"}), 400

    try:
        from search.boards import probar_fuente
    except Exception as err:  # noqa: BLE001 — module missing in this deploy.
        return jsonify(
            {
                "ok": False,
                "detalle": f"no se pudo cargar search.boards: {err}",
            }
        ), 500

    try:
        resultado = probar_fuente(nombre, parametros or {})
    except Exception as err:  # noqa: BLE001 — probar_fuente should not raise.
        print(
            t(
                f"[web] prueba de fuente fallida ({nombre}): {err}",
                f"[web] source test failed ({nombre}): {err}",
            ),
        )
        return jsonify({"ok": False, "detalle": f"error en la prueba: {err}"})

    if not isinstance(resultado, dict):
        return jsonify({"ok": False, "detalle": "respuesta inesperada de la fuente"})
    return jsonify(resultado)


def _ruta_log():
    """Return the service log path (``JOB_LOG_PATH``) for live activity.

    Returns:
        The configured path, falling back to ``/data/logs/jobautomat.log``.
    """
    ruta = None
    try:
        ruta = config.load_config().get("JOB_LOG_PATH")
    except Exception:  # noqa: BLE001
        ruta = None
    return ruta or os.environ.get("JOB_LOG_PATH") or "/data/logs/jobautomat.log"


@app.get("/api/log")
def api_log():
    """Tail the service log for real-time activity.

    Query params:
        n: Number of lines, clamped to 1..3000 (default 250).

    Returns:
        ``{"lineas": [...], "mtime": ..., "activo": bool, "ruta": ...}``;
        ``activo`` is true when the log was written in the last 90 seconds.
    """
    try:
        n = int(request.args.get("n") or 250)
    except (TypeError, ValueError):
        n = 250
    n = max(1, min(n, 3000))
    ruta = _ruta_log()
    lineas = []
    mtime = None
    activo = False
    if os.path.isfile(ruta):
        try:
            mtime = os.path.getmtime(ruta)
            activo = (time.time() - mtime) < 90
            with open(ruta, encoding="utf-8", errors="replace") as fh:
                lineas = [linea.rstrip("\n") for linea in fh.readlines()[-n:]]
        except Exception as err:  # noqa: BLE001
            lineas = [
                t(f"[web] no se pudo leer el log: {err}", f"[web] could not read the log: {err}")
            ]
    return jsonify({"lineas": lineas, "mtime": mtime, "activo": activo, "ruta": ruta})


# ---------------------------------------------------------------------------
# Manual search ("Buscar ahora" button)
# ---------------------------------------------------------------------------

# Lock file name: the same one main.py uses (NOMBRE_LOCK_BUSQUEDA).
_NOMBRE_LOCK_BUSCAR = ".buscar.lock"


def _ruta_db():
    """Return the effective ``state.db`` path (as resolved by ``db.py``).

    Reuses ``_ruta_base_lectura`` so DB_PATH logic is not duplicated and the
    database is never created here. If the config cannot be read, the
    ``state.db`` next to the service is used.

    Returns:
        The database path.
    """
    try:
        return _ruta_base_lectura()
    except Exception as err:  # noqa: BLE001 — the lock must not break the panel.
        print(
            t(
                f"[web] no se pudo resolver la base para el lock: {err}",
                f"[web] could not resolve the database for the lock: {err}",
            ),
        )
        return os.path.join(BASE_DIR, "state.db")


def _ruta_lock_buscar():
    """Return the lock path shared with ``main.py``.

    Returns:
        ``<dir of state.db>/.buscar.lock``.
    """
    return os.path.join(os.path.dirname(os.path.abspath(_ruta_db())), _NOMBRE_LOCK_BUSCAR)


def _busqueda_activa():
    """Check whether another process holds the search lock.

    Tries to take the lock exclusively without blocking; on success it releases
    it immediately and returns ``False``. Without ``fcntl`` (non-POSIX) it
    returns ``False``: the panel never disables the button on its own.

    Returns:
        ``True`` when a search is running, ``False`` otherwise.
    """
    if fcntl is None:
        return False
    ruta = _ruta_lock_buscar()
    try:
        os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
        descriptor = os.open(ruta, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as err:
        print(
            t(
                f"[web] no se pudo abrir el lock {ruta}: {err}",
                f"[web] could not open the lock {ruta}: {err}",
            ),
        )
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True  # held by a running search
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


@app.get("/api/buscar/estado")
def api_buscar_estado():
    """Report whether a search is running, so the panel can render the button.

    Returns:
        ``{"activo": bool}``, or 500 when the check fails.
    """
    try:
        activo = bool(_busqueda_activa())
    except Exception as err:  # noqa: BLE001 — surface a readable error.
        return jsonify({"error": f"no se pudo comprobar la búsqueda: {err}"}), 500
    return jsonify({"activo": activo})


@app.post("/api/buscar")
def api_buscar():
    """Launch ``main.py --modo manual`` in the background (one at a time).

    ``main`` is not imported on purpose: importing it would run its code and
    pull patchright into the panel process. It runs as a subprocess in a new
    session with output silenced (the run logs to ``JOB_LOG_PATH``, which is
    what live activity reads).

    Returns:
        202 when the search starts, 409 when one is already running, 500 when
        it cannot be launched.
    """
    if _busqueda_activa():
        return jsonify({"ok": False, "detalle": "ya hay una búsqueda en curso"}), 409

    comando = [
        sys.executable,
        os.path.join(BASE_DIR, "main.py"),
        "--modo",
        "manual",
    ]
    try:
        subprocess.Popen(
            comando,
            cwd=BASE_DIR,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as err:  # noqa: BLE001 — surface a readable error.
        print(
            t(
                f"[web] no se pudo lanzar la búsqueda manual: {err}",
                f"[web] could not launch the manual search: {err}",
            ),
        )
        return jsonify({"ok": False, "detalle": f"no se pudo lanzar la búsqueda: {err}"}), 500

    print(
        t(
            "[web] búsqueda manual lanzada ({})".format(" ".join(comando)),
            "[web] manual search launched ({})".format(" ".join(comando)),
        )
    )
    return jsonify({"ok": True, "detalle": "búsqueda lanzada"}), 202


@app.post("/api/busquedas/limpiar")
def api_limpiar_busquedas():
    """Delete the search results so the next run starts from zero.

    Discards every offer in the search flow (``para_confirmar`` and
    ``descartada``) and empties the score cache. Approved offers and their
    generated materials are kept.

    Returns:
        ``{"borradas": n}`` with the number of deleted ``oportunidades`` rows,
        or 500 with ``{"error": ...}`` when the database cannot be cleaned.
    """
    try:
        borradas = db.limpiar_busquedas()
    except Exception as err:  # noqa: BLE001 — surface a readable DB error.
        print(
            t(
                f"[web] no se pudieron borrar las búsquedas: {err}",
                f"[web] could not clear the search results: {err}",
            ),
        )
        return jsonify({"error": f"no se pudieron borrar las búsquedas: {err}"}), 500

    print(
        t(
            f"[web] búsquedas borradas: {borradas} ofertas",
            f"[web] search results cleared: {borradas} offers",
        ),
    )
    return jsonify({"borradas": borradas})


if __name__ == "__main__":
    # Werkzeug access logs are noisy: keep only warnings and errors.
    import logging

    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    puerto = int(os.environ.get("WEB_PORT") or 8085)
    print(
        t(
            f"[web] panel en http://0.0.0.0:{puerto} "
            f"(auth: {'sí' if _password() else 'NO — sólo LAN'})",
            f"[web] panel on http://0.0.0.0:{puerto} "
            f"(auth: {'yes' if _password() else 'NO — LAN only'})",
        ),
    )
    app.run(host="0.0.0.0", port=puerto, threaded=True)
