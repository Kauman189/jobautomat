"""notify.py — Telegram notifications and confirmation bot.

Three pieces:

* ``enviar_resumen(ops)`` sends one Telegram message per opportunity, with a
  two-button inline keyboard (``aprobar_<id>`` / ``descartar_<id>``).
  In this phase the service does NOT apply on its own: the user decides from
  the phone.
* ``run_bot()`` long-polls ``getUpdates``, captures the ``callback_query``
  events from those buttons, updates the state in SQLite
  (``aprobada`` / ``descartada``) and answers the callback with
  ``answerCallbackQuery``.
* ``enviar_webhook(cfg, texto)`` pushes the same notice to a generic webhook
  (ntfy, Discord, Slack…) when ``WEBHOOK_URL`` is configured. It is optional
  and must never break the Telegram send: any failure is only logged.

Telegram can be switched off entirely with ``TELEGRAM_HABILITADO=false``; then
sends are limited to the webhook (or to silence, if no webhook is configured
either).

Everything uses ``urllib`` (no external dependencies). The bot token is read
from the configuration and is never printed nor included in error messages.
"""

import datetime
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request

import db

_API_BASE = "https://api.telegram.org/bot{token}/{metodo}"

# Generic webhook timeout (seconds): kept short so it does not delay the run.
_WEBHOOK_TIMEOUT = 8

# Maximum text length sent to a webhook (Discord/ntfy limits).
_WEBHOOK_MAX_TEXTO = 1900

# Valid states of the confirmation flow.
ESTADO_APROBADA = "aprobada"
ESTADO_DESCARTADA = "descartada"

# callback_data prefixes.
_PREFIJO_APROBAR = "aprobar_"
_PREFIJO_DESCARTAR = "descartar_"

# Telegram limit for callback_data (bytes).
_MAX_CALLBACK = 64


class TelegramError(Exception):
    """Telegram API error (never exposes the token)."""


# ---------------------------------------------------------------------------
# Configuration and internal helpers
# ---------------------------------------------------------------------------


def _config():
    """Load the configuration on every call (picks up .env changes)."""
    import config as config_mod

    return config_mod.load_config()


def _cargar_config_estricta():
    """Startup fail-fast: load the configuration in strict mode.

    If something essential is missing (``DEEPSEEK_API_KEY``), ``load_config``
    raises ``RuntimeError``; here the actionable message is printed and the
    process exits with code 1 instead of starting a bot that could do nothing.

    Returns:
        dict: the loaded configuration.

    Raises:
        SystemExit: with code 1 if the configuration is invalid.
    """
    import config as config_mod

    try:
        return config_mod.load_config(estricto=True)
    except RuntimeError as err:
        print(f"[notify] Error de configuración: {err}", file=sys.stderr)
        sys.exit(1)


def telegram_habilitado(cfg):
    """Return True if ``TELEGRAM_HABILITADO`` allows sending over Telegram.

    Accepts the boolean already converted by ``config.load_config`` and, for
    robustness, strings such as ``"false"``/``"0"``/``"off"``. Missing = on.

    Args:
        cfg: configuration mapping (may be None).

    Returns:
        bool: whether Telegram sending is enabled.
    """
    valor = (cfg or {}).get("TELEGRAM_HABILITADO", True)
    if isinstance(valor, str):
        return valor.strip().lower() not in ("", "0", "false", "no", "n", "off")
    return bool(valor)


def enviar_webhook(cfg, texto):
    """POST ``texto`` to ``cfg["WEBHOOK_URL"]`` (ntfy, Discord, Slack…).

    Without ``WEBHOOK_URL`` it does nothing and returns ``False``. The body
    format follows the host, so the receiver needs no extra configuration:

    * ``discord``  → JSON ``{"content": texto}``
    * ``slack``    → JSON ``{"text": texto}``
    * ``ntfy``/other → plain text with a ``Title: jobautomat`` header

    Any error (network, HTTP, DNS) is logged and swallowed: the webhook is an
    extra and must **never** break the Telegram send nor the run.

    Args:
        cfg: configuration mapping; may be None.
        texto: message body.

    Returns:
        bool: True if the POST succeeded, False otherwise.
    """
    cfg = cfg or {}
    url = str(cfg.get("WEBHOOK_URL") or "").strip()
    texto = str(texto or "").strip()
    if not url or not texto:
        return False
    if not url.lower().startswith(("http://", "https://")):
        print("[notify] WEBHOOK_URL ignorada: no es http(s)", file=sys.stderr)
        return False

    texto = texto[:_WEBHOOK_MAX_TEXTO]
    destino = url.lower()
    headers = {"User-Agent": "jobautomat/1.0"}

    if "discord" in destino:
        cuerpo = json.dumps({"content": texto}, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif "slack" in destino:
        cuerpo = json.dumps({"text": texto}, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    else:
        # ntfy and compatible services: raw text in the body.
        cuerpo = texto.encode("utf-8")
        headers["Content-Type"] = "text/plain; charset=utf-8"
        headers["Title"] = "jobautomat"

    peticion = urllib.request.Request(url, data=cuerpo, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(peticion, timeout=_WEBHOOK_TIMEOUT) as resp:
            resp.read(64)
    except Exception as err:  # noqa: BLE001 — a webhook failure never aborts the flow.
        # Only the error type: the URL itself may carry a token.
        print(
            f"[notify] webhook fallido ({type(err).__name__}): {err}",
            file=sys.stderr,
        )
        return False
    return True


def _directorio_estado():
    """Directory holding the database and the state files.

    Returns:
        str: absolute path to the state directory.
    """
    cfg = _config()
    ruta_db = cfg.get("DB_PATH") or "state.db"
    if not os.path.isabs(ruta_db):
        ruta_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), ruta_db)
    return os.path.dirname(ruta_db) or "."


def _api(metodo, payload):
    """Call the Bot API and return its ``result`` field.

    Args:
        metodo: Bot API method name (``sendMessage``, ``getUpdates``…).
        payload: JSON-serialisable request body.

    Returns:
        The ``result`` value returned by Telegram.

    Raises:
        TelegramError: on missing token, transport failure or an API error;
            the full URL (which contains the token) is never included.
    """
    cfg = _config()
    token = cfg.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramError("Falta TELEGRAM_BOT_TOKEN en la configuración.")

    url = _API_BASE.format(token=token, metodo=metodo)
    cuerpo = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=cuerpo,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            datos = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detalle = err.read().decode("utf-8", errors="replace")
        raise TelegramError(f"HTTP {err.code} en {metodo}: {detalle[:300]}") from err
    except urllib.error.URLError as err:
        raise TelegramError(f"Error de red en {metodo}: {err.reason}") from err
    except json.JSONDecodeError as err:
        raise TelegramError(f"Respuesta no-JSON de {metodo}: {err}") from err

    if not datos.get("ok"):
        descripcion = datos.get("description", "sin detalle")
        pista = ""
        if "chat not found" in descripcion:
            pista = (
                " Revisa TELEGRAM_CHAT_ID y abre el bot en Telegram para"
                " pulsar Start: un bot no puede iniciar la conversación."
            )
        raise TelegramError(f"Telegram rechazó {metodo}: {descripcion}{pista}")
    return datos.get("result")


def _escapar(texto):
    """Escape text for ``parse_mode=HTML``."""
    return html.escape(str(texto or ""), quote=False)


def _ruta_mapa_callbacks():
    """Path of the JSON file mapping callback tokens to long ids."""
    return os.path.join(_directorio_estado(), "telegram_callback_ids.json")


def _leer_mapa():
    """Load the token -> id map (empty dict if missing or corrupt)."""
    try:
        with open(_ruta_mapa_callbacks(), encoding="utf-8") as fh:
            datos = json.load(fh)
        return datos if isinstance(datos, dict) else {}
    except (OSError, ValueError):
        return {}


def _guardar_mapa(mapa):
    """Persist the token -> id map atomically."""
    ruta = _ruta_mapa_callbacks()
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(mapa, fh, ensure_ascii=False)
    os.replace(tmp, ruta)


def _token_callback(op_id, accion):
    """Return ``callback_data`` for an action, respecting the 64-byte limit.

    If ``accion:id`` fits, the id is used as-is. Otherwise a short hash token
    is stored in an auxiliary JSON file that ``run_bot`` uses to recover the
    original id, so long LinkedIn ids do not break the button.

    Args:
        op_id: opportunity identifier.
        accion: callback prefix (``_PREFIJO_APROBAR``/``_PREFIJO_DESCARTAR``).

    Returns:
        str: the callback_data string.
    """
    identificador = str(op_id)
    candidato = f"{accion}{identificador}"
    if len(candidato.encode("utf-8")) <= _MAX_CALLBACK:
        return candidato

    import hashlib

    token = hashlib.sha1(identificador.encode("utf-8")).hexdigest()[:20]
    mapa = _leer_mapa()
    mapa[token] = identificador
    _guardar_mapa(mapa)
    return f"{accion}{token}"


def _resolver_id(token):
    """Recover the real opportunity id from a callback token."""
    mapa = _leer_mapa()
    return mapa.get(token, token)


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def _formatear_op(op):
    """Return the HTML text of one opportunity for Telegram."""
    titulo = _escapar(op.get("titulo") or "(sin título)")
    empresa = _escapar(op.get("empresa") or "(sin empresa)")
    lineas = [f"<b>{titulo}</b>", empresa]

    detalles = []
    if op.get("ubicacion"):
        detalles.append(_escapar(op["ubicacion"]))
    if op.get("remoto"):
        detalles.append(_escapar(op["remoto"]))
    if op.get("salario"):
        detalles.append(_escapar(op["salario"]))
    if detalles:
        lineas.append(" · ".join(detalles))

    if op.get("score") is not None:
        lineas.append("Score: <b>{}</b>/100".format(op.get("score")))

    motivos = [m for m in (op.get("motivos") or []) if m]
    if motivos:
        lineas.append("")
        lineas.append("<b>Encaja</b>")
        for motivo in motivos[:3]:
            lineas.append(f"• {_escapar(motivo)}")

    gaps = [g for g in (op.get("gaps") or []) if g]
    if gaps:
        lineas.append("")
        lineas.append("<b>Gaps</b>")
        for gap in gaps[:3]:
            lineas.append(f"• {_escapar(gap)}")

    if op.get("url"):
        lineas.append("")
        lineas.append('<a href="{}">Ver oferta</a>'.format(html.escape(str(op["url"]), quote=True)))

    texto = "\n".join(lineas)
    # Telegram truncates messages at 4096 characters.
    return texto[:4000]


def _texto_plano_op(op):
    """Markup-free version of an opportunity, for the webhook.

    The webhook cannot interpret ``parse_mode=HTML`` (Discord uses a different
    subset, Slack another one and ntfy none), so plain text is sent instead.
    """
    lineas = [
        str(op.get("titulo") or "(sin título)"),
        str(op.get("empresa") or "(sin empresa)"),
    ]
    detalles = [str(op[c]) for c in ("ubicacion", "remoto", "salario") if op.get(c)]
    if detalles:
        lineas.append(" · ".join(detalles))
    if op.get("score") is not None:
        lineas.append("Score: {}/100".format(op.get("score")))
    for etiqueta, clave in (("Encaja", "motivos"), ("Gaps", "gaps")):
        valores = [str(v) for v in (op.get(clave) or []) if v]
        if valores:
            lineas.append("{}: {}".format(etiqueta, " | ".join(valores[:3])))
    if op.get("url"):
        lineas.append(str(op["url"]))
    return "\n".join(lineas)


def _teclado(op):
    """Build the inline_keyboard with the approve/discard buttons."""
    op_id = op.get("id")
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Aprobar",
                    "callback_data": _token_callback(op_id, _PREFIJO_APROBAR),
                },
                {
                    "text": "🗑 Descartar",
                    "callback_data": _token_callback(op_id, _PREFIJO_DESCARTAR),
                },
            ]
        ]
    }


def enviar_oferta(op):
    """Send ONE opportunity to Telegram with its approve/discard buttons.

    Honours ``TELEGRAM_HABILITADO`` (when off, Telegram is not called and
    ``TELEGRAM_CHAT_ID`` is not required) and also pushes the notice to the
    generic webhook, if ``WEBHOOK_URL`` is set.

    Args:
        op: opportunity dict; ignored if empty or without ``id``.

    Returns:
        None

    Raises:
        TelegramError: if the API rejects the send.
    """
    cfg = _config()
    if not op or not op.get("id"):
        return None

    # Webhook first: it never raises, so the notice goes out even if Telegram
    # fails afterwards.
    if not telegram_habilitado(cfg):
        enviar_webhook(cfg, "Jobautomat — oferta para confirmar\n\n" + _texto_plano_op(op))
        return None

    chat_id = cfg.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise TelegramError("Falta TELEGRAM_CHAT_ID en la configuración.")

    enviar_webhook(cfg, "Jobautomat — oferta para confirmar\n\n" + _texto_plano_op(op))
    _api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": _formatear_op(op),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": _teclado(op),
        },
    )
    return None


def enviar_resumen(ops):
    """Send the summary of opportunities pending confirmation.

    One message per opportunity (with its button pair) plus a header with the
    count. An empty list sends a short notice so that bot silence is not
    mistaken for a failure.

    Honours ``TELEGRAM_HABILITADO`` and mirrors the summary to the generic
    webhook (a single POST with all the text, to avoid flooding the channel).

    Args:
        ops: iterable of opportunity dicts.

    Returns:
        None

    Raises:
        TelegramError: if the API rejects the send.
    """
    cfg = _config()
    habilitado = telegram_habilitado(cfg)
    chat_id = cfg.get("TELEGRAM_CHAT_ID")
    if habilitado and not chat_id:
        raise TelegramError("Falta TELEGRAM_CHAT_ID en la configuración.")

    ops = [o for o in (ops or []) if o and o.get("id")]

    if not ops:
        texto = "Jobautomat: sin ofertas nuevas por encima del umbral hoy."
        # Webhook before Telegram: best-effort and cannot raise.
        enviar_webhook(cfg, texto)
        if habilitado:
            _api("sendMessage", {"chat_id": chat_id, "text": texto})
        return None

    cabecera = f"Jobautomat — {len(ops)} oferta{'' if len(ops) == 1 else 's'} para confirmar"

    bloques = [cabecera, ""]
    for op in ops:
        bloques.append(_texto_plano_op(op))
        bloques.append("")
    enviar_webhook(cfg, "\n".join(bloques))

    if not habilitado:
        return None

    _api("sendMessage", {"chat_id": chat_id, "text": cabecera})

    for op in ops:
        enviar_oferta(op)
    return None


def enviar_aviso(texto):
    """Free-form one-line notice (no buttons): Telegram + webhook.

    Meant for operational alerts — e.g. an expired LinkedIn session. Honours
    ``TELEGRAM_HABILITADO`` and **never** raises: a Telegram failure is only
    logged, because a secondary notice must not abort the run.

    Args:
        texto: notice body; ignored if empty.

    Returns:
        None
    """
    texto = str(texto or "").strip()
    if not texto:
        return None
    cfg = _config()
    enviar_webhook(cfg, texto)
    if not telegram_habilitado(cfg):
        return None
    if not cfg.get("TELEGRAM_CHAT_ID"):
        print("[notify] aviso no enviado: falta TELEGRAM_CHAT_ID", file=sys.stderr)
        return None
    try:
        _api("sendMessage", {"chat_id": cfg["TELEGRAM_CHAT_ID"], "text": texto})
    except TelegramError as err:
        print(f"[notify] aviso no enviado: {err}", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Bot long-polling
# ---------------------------------------------------------------------------


def _ruta_offset():
    """File where the ``getUpdates`` offset is persisted."""
    return os.path.join(_directorio_estado(), "telegram_offset.json")


def _leer_offset():
    """Return the stored offset (0 if missing or corrupt)."""
    try:
        with open(_ruta_offset(), encoding="utf-8") as fh:
            datos = json.load(fh)
        return int(datos.get("offset", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def _guardar_offset(offset):
    """Persist the offset so callbacks are not reprocessed after a restart."""
    ruta = _ruta_offset()
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"offset": int(offset)}, fh)
    os.replace(tmp, ruta)


def _procesar_callback(callback):
    """Apply a callback_query: update the state and answer the user.

    Args:
        callback: ``callback_query`` object as delivered by Telegram.

    Returns:
        The confirmation text, or ``None`` if the callback was ignored.

    Raises:
        TelegramError: if the state could not be stored.
    """
    callback_id = callback.get("id")
    datos = callback.get("data") or ""

    if datos.startswith(_PREFIJO_APROBAR):
        accion, estado, etiqueta = _PREFIJO_APROBAR, ESTADO_APROBADA, "Aprobada"
    elif datos.startswith(_PREFIJO_DESCARTAR):
        accion, estado, etiqueta = _PREFIJO_DESCARTAR, ESTADO_DESCARTADA, "Descartada"
    else:
        if callback_id:
            _api(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": "Acción no reconocida."},
            )
        return None

    op_id = _resolver_id(datos[len(accion) :])
    try:
        actualizada = db.set_estado(op_id, estado)
    except Exception as err:  # noqa: BLE001 — the user is told about it.
        if callback_id:
            _api(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_id,
                    "text": "Error al guardar el estado.",
                    "show_alert": True,
                },
            )
        raise TelegramError(f"No se pudo actualizar {op_id}: {err}") from err

    if not actualizada:
        texto = "La oferta ya no existe en la base."
    elif estado == ESTADO_APROBADA:
        texto = "Aprobada — preparando tu pack de candidatura…"
    else:
        texto = f"{etiqueta}: {op_id}"

    if callback_id:
        _api(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": texto},
        )

    # Remove the buttons from the message to prevent double taps.
    mensaje = callback.get("message") or {}
    if mensaje.get("message_id") and mensaje.get("chat", {}).get("id"):
        try:
            _api(
                "editMessageReplyMarkup",
                {
                    "chat_id": mensaje["chat"]["id"],
                    "message_id": mensaje["message_id"],
                    "reply_markup": {"inline_keyboard": []},
                },
            )
        except TelegramError:
            # The message may be too old to edit; not critical.
            pass

    # On approval the materials are generated and sent (cover letter, CV,
    # contact message and PDFs) in a separate message, after answering the
    # callback. This is the link between search and manual application: the
    # user gets the pack and applies themselves. Failures are reported without
    # taking the bot down.
    if estado == ESTADO_APROBADA and actualizada:
        op = db.get(op_id)
        chat_id = mensaje.get("chat", {}).get("id")
        if op and chat_id:
            try:
                _enviar_pack(op, chat_id)
            except Exception as err:  # noqa: BLE001
                try:
                    _api(
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": f"❌ No pude preparar los materiales: {type(err).__name__}",
                        },
                    )
                except TelegramError:
                    pass
                print(f"[notify] pack fallido para {op_id}: {err}")

            # Answers to the form questions (paste-ready), separately.
            try:
                _enviar_respuestas_formulario(op, chat_id)
            except Exception as err:  # noqa: BLE001
                print(f"[notify] respuestas de formulario fallidas para {op_id}: {err}")

    print(
        "[notify] {} {} ({})".format(
            etiqueta.lower(),
            op_id,
            datetime.datetime.now().isoformat(timespec="seconds"),
        )
    )
    return texto


# ---------------------------------------------------------------------------
# Application pack (on approval)
# ---------------------------------------------------------------------------


def _cargar_perfil():
    """Load ``data/perfil.json`` (same path resolution as ``main.py``).

    Returns:
        dict: the parsed profile.

    Raises:
        OSError: if the file cannot be read.
        ValueError: if the file is not valid JSON.
    """
    import config as config_mod

    cfg = config_mod.load_config()
    ruta = cfg.get("PERFIL_PATH") or "../data/perfil.json"
    if not os.path.isabs(ruta):
        ruta = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ruta))
    with open(ruta, encoding="utf-8") as fh:
        return json.load(fh)


def _api_documento(chat_id, ruta, caption=""):
    """Send a local document (multipart) to Telegram.

    ``_api`` only handles JSON; documents need ``multipart/form-data``, so the
    body is built by hand here.

    Args:
        chat_id: destination chat id.
        ruta: path of the local file to upload.
        caption: optional caption shown with the document.

    Returns:
        The ``result`` value returned by Telegram.

    Raises:
        TelegramError: on missing token, transport failure or an API error.
    """
    cfg = _config()
    token = cfg.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramError("Falta TELEGRAM_BOT_TOKEN en la configuración.")

    url = _API_BASE.format(token=token, metodo="sendDocument")
    boundary = f"----jobautomat{int(time.time() * 1000)}"

    with open(ruta, "rb") as fh:
        contenido = fh.read()
    nombre = os.path.basename(ruta)
    extension = os.path.splitext(nombre)[1].lower()
    content_type = {
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".md": "text/markdown",
    }.get(extension, "application/octet-stream")

    def _campo(nombre_campo, valor):
        return (
            (f"--{boundary}\r\n").encode()
            + (f'Content-Disposition: form-data; name="{nombre_campo}"\r\n\r\n').encode()
            + str(valor).encode("utf-8")
            + b"\r\n"
        )

    cuerpo = (
        _campo("chat_id", chat_id)
        + _campo("caption", caption)
        + (f"--{boundary}\r\n").encode()
        + (f'Content-Disposition: form-data; name="document"; filename="{nombre}"\r\n').encode()
        + b"Content-Type: "
        + content_type.encode("utf-8")
        + b"\r\n\r\n"
        + contenido
        + b"\r\n"
        + (f"--{boundary}--\r\n").encode()
    )

    req = urllib.request.Request(
        url,
        data=cuerpo,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            datos = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detalle = err.read().decode("utf-8", errors="replace")
        raise TelegramError(f"HTTP {err.code} en sendDocument: {detalle[:300]}") from err
    except urllib.error.URLError as err:
        raise TelegramError(f"Error de red en sendDocument: {err.reason}") from err
    except json.JSONDecodeError as err:
        raise TelegramError(f"Respuesta no-JSON de sendDocument: {err}") from err

    if not datos.get("ok"):
        raise TelegramError(
            "Telegram rechazó sendDocument: {}".format(datos.get("description", "sin detalle"))
        )
    return datos.get("result")


def _enviar_pack(op, chat_id):
    """Generate ``op``'s materials (if missing) and send the full pack.

    Called when an opportunity is approved. Generates the cover letter, CV and
    contact message once (stored in the database) and sends them over
    Telegram: the text in a message and the CV and cover letter as PDF
    attachments. **Never applies on its own**: it only delivers the materials
    so the user can apply.

    Args:
        op: opportunity dict.
        chat_id: destination chat id.

    Returns:
        None
    """
    import generate as generate_mod

    perfil = _cargar_perfil()

    def _gen(func):
        try:
            return func(op, perfil)
        except Exception as err:  # noqa: BLE001 — one document does not sink the rest.
            print(f"[notify] generación fallida ({func.__name__}): {err}")
            return ""

    cover = op.get("cover_letter") or _gen(generate_mod.generar_cover_letter)
    cv = op.get("cv_adaptado") or _gen(generate_mod.generar_cv_adaptado)
    mensaje = op.get("mensaje") or _gen(generate_mod.generar_mensaje)

    try:
        db.guardar_materiales(op["id"], cover, cv, mensaje)
    except Exception as err:  # noqa: BLE001 — persistence is secondary.
        print("[notify] no se guardaron los materiales de {}: {}".format(op["id"], err))

    lineas = [
        "✅ <b>Aprobada: {}</b>".format(_escapar(op.get("titulo"))),
        _escapar(op.get("empresa")),
        "",
    ]
    if cover:
        lineas.append("<b>Carta de presentación</b>")
        lineas.append(cover)
    if mensaje:
        lineas.append("")
        lineas.append("<b>Mensaje de contacto</b>")
        lineas.append(mensaje)
    if op.get("url"):
        lineas.append("")
        lineas.append(
            '<a href="{}">🔗 Aplicar aquí</a>'.format(html.escape(str(op["url"]), quote=True))
        )
    _api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": "\n".join(lineas)[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )

    cover_txt = None
    if cover:
        try:
            directorio = generate_mod._directorio_candidatura(op)
            os.makedirs(directorio, exist_ok=True)
            cover_txt = os.path.join(directorio, "cover.txt")
            with open(cover_txt, "w", encoding="utf-8") as fh:
                fh.write(cover)
        except Exception as err:  # noqa: BLE001 — the .txt file is a bonus.
            print(f"[notify] no se escribió cover.txt: {err}")
            cover_txt = None

    for ruta, nombre in (
        (op.get("cv_pdf"), "CV adaptado"),
        (op.get("cover_pdf"), "Cover letter (PDF)"),
        (cover_txt, "Cover letter (texto)"),
    ):
        if ruta and os.path.isfile(ruta):
            try:
                _api_documento(chat_id, ruta, caption=nombre)
            except TelegramError as err:
                try:
                    _api(
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": f"No pude adjuntar {nombre}: {err}",
                        },
                    )
                except TelegramError:
                    pass


def _enviar_respuestas_formulario(op, chat_id):
    """Analyse the application ATS form and send drafted answers.

    Best-effort, only for opportunities with a Greenhouse/Lever/Ashby URL. If
    the analysis fails or there are no open questions, a brief notice (or
    nothing) is sent without affecting the main pack. **Never submits the
    form**: answers are delivered so the user pastes them.

    Args:
        op: opportunity dict.
        chat_id: destination chat id.

    Returns:
        None
    """
    url = (op.get("url") or "").lower()
    if not url or not any(s in url for s in ("greenhouse", "lever.co", "ashbyhq")):
        return

    try:
        import apply_form as af
    except Exception as err:  # noqa: BLE001
        print(f"[notify] apply_form no disponible: {err}")
        return

    try:
        formulario = af.analizar_formulario(url)
    except Exception as err:  # noqa: BLE001
        print("[notify] análisis de formulario fallido para {}: {}".format(op["id"], err))
        return

    if not formulario:
        return

    perfil = _cargar_perfil()
    try:
        respuestas = af.redactar_respuestas(formulario, perfil, op)
    except Exception as err:  # noqa: BLE001
        print(f"[notify] redacción de respuestas fallida: {err}")
        return

    if not respuestas:
        try:
            _api(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "📋 El formulario no tiene preguntas abiertas: aplica directo.",
                },
            )
        except TelegramError:
            pass
        return

    # Map name/label -> question text, for readable output.
    pregunta_de = {}
    for campo in formulario:
        clave = (campo.get("nombre") or campo.get("pregunta") or "").strip()
        if clave:
            pregunta_de[clave] = (campo.get("pregunta") or campo.get("etiqueta") or "").strip()

    lineas = ["📋 <b>Respuestas para el formulario</b> (pégalas tú):", ""]
    for clave, respuesta in respuestas.items():
        pregunta = pregunta_de.get(clave) or clave
        lineas.append(f"<b>{_escapar(pregunta)}</b>")
        lineas.append(_escapar(respuesta))
        lineas.append("")
    try:
        _api(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "\n".join(lineas)[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
    except TelegramError as err:
        print(f"[notify] no se enviaron las respuestas: {err}")


def run_bot(poll_timeout=25):
    """Long-polling loop serving the approve/discard buttons.

    Persists the offset in ``telegram_offset.json`` so callbacks are not
    reprocessed after a restart. Stops on Ctrl+C (``KeyboardInterrupt``).
    Network errors are logged and retried with backoff.

    Before entering the loop the configuration is validated in strict mode
    (fail-fast): without ``DEEPSEEK_API_KEY`` the bot can do nothing useful, so
    the error is printed and the process exits with code 1.

    Args:
        poll_timeout: ``getUpdates`` long-poll timeout in seconds.

    Returns:
        None
    """
    _cargar_config_estricta()
    offset = _leer_offset()
    print(f"[notify] bot a la escucha (offset={offset})")

    while True:
        try:
            actualizaciones = _api(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": poll_timeout,
                    "allowed_updates": ["callback_query"],
                },
            )
        except TelegramError as err:
            print(f"[notify] error de polling: {err} (reintento en 5s)")
            time.sleep(5)
            continue

        for update in actualizaciones or []:
            update_id = update.get("update_id")
            if update_id is not None:
                offset = max(offset, int(update_id) + 1)
                _guardar_offset(offset)

            callback = update.get("callback_query")
            if not callback:
                continue
            try:
                _procesar_callback(callback)
            except TelegramError as err:
                print(f"[notify] callback fallido: {err}")


if __name__ == "__main__":
    # Manual bot start (Ctrl+C to exit).
    run_bot()
