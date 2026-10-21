"""followup.py — Follow-up on applications sent without a reply.

Standalone module: does not modify ``main.py``, ``generate.py`` or the ``db.py``
schema. Reads the database only through the public API of ``db.py``
(``db.listar``) and optionally keeps a local ledger of follow-ups already sent
in a separate JSON file.

Assumed ``db.py`` states (taken from main.py / notify.py / apply.py and the
real ``service/state.db`` schema):

    para_confirmar  → opportunity detected, not yet approved or sent.
    aprobada        → approved by the user; apply.py sets it.
    enviada         → application sent (state used in smoke_test.py).
    aplicada        → manual variant of "enviada"; no module writes it today,
                      but it is accepted in case it is set by hand.
    descartada      → discarded by the user.

Only ``enviada`` and ``aplicada`` count as "sent applications" here.
``para_confirmar``, ``aprobada`` and ``descartada`` NEVER produce a follow-up.

Follow-up-done flag:
    The ``db.py`` schema has no follow-up column, so two sources are accepted
    without touching the database:
      1. Optional columns if they ever exist (``seguimiento_at``,
         ``followup_at``, ``ultimo_seguimiento``, ``seguimiento_hecho``),
         detected from the keys of the dict returned by ``db.listar``.
      2. Local ledger ``data/seguimientos.json`` (written by
         ``marcar_seguimiento_hecho``). Its format is::

             {"<id_oportunidad>": {"ultimo_seguimiento": "<ISO-8601>",
                                   "veces": 2, "canal": "email",
                                   "nota": "..."}}

Nothing is sent automatically: ``redactar_seguimiento`` only returns text for
a person to review and send.
"""

import datetime
import json
import os

import db
import llm

# States counted as "application sent" (see module docstring).
ESTADOS_ENVIADA = ("enviada", "aplicada")

# Column names that, if present, mark the follow-up as done.
_CAMPOS_FLAG = (
    "seguimiento_at",
    "followup_at",
    "ultimo_seguimiento",
    "seguimiento_hecho",
)

# Fields telling when the application was sent / last touched.
_CAMPOS_FECHA = (
    "enviada_at",
    "aplicada_at",
    "updated_at",
    "created_at",
)

# Default path of the follow-up ledger (next to data/perfil.json).
_LEDGER_POR_DEFECTO = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "seguimientos.json",
)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parsear_fecha(valor):
    """Convert an ISO-8601 date (with or without zone) to an aware UTC datetime.

    Returns ``None`` when empty or unparseable. Naive dates are read as UTC,
    the same way ``db.set_estado`` writes them.
    """
    if not valor:
        return None
    if isinstance(valor, datetime.datetime):
        fecha = valor
    else:
        texto = str(valor).strip().replace("Z", "+00:00")
        try:
            fecha = datetime.datetime.fromisoformat(texto)
        except ValueError:
            return None
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=datetime.timezone.utc)
    return fecha


def _ahora_utc():
    """Current UTC instant (separate function to ease testing)."""
    return datetime.datetime.now(datetime.timezone.utc)


def _fecha_referencia(op):
    """Return (field, datetime) of the last known contact for ``op``.

    Prefers the explicit sent date, then ``updated_at`` and finally
    ``created_at``. Returns ``(None, None)`` when no usable date exists.
    """
    for campo in _CAMPOS_FECHA:
        fecha = _parsear_fecha(op.get(campo))
        if fecha is not None:
            return campo, fecha
    return None, None


def _flag_hecho(op):
    """True if any optional column says the follow-up was already done.

    Date columns count as done when set, booleans when true. Returns False
    when the schema has none of these columns (current case).
    """
    for campo in _CAMPOS_FLAG:
        if campo not in op:
            continue
        valor = op.get(campo)
        if isinstance(valor, str):
            if valor.strip():
                return True
        elif valor:
            return True
    return False


# ---------------------------------------------------------------------------
# Local follow-up ledger (JSON, no SQLite schema change)
# ---------------------------------------------------------------------------

def _cargar_ledger(ruta=None):
    """Read the follow-up ledger and return a dict.

    A missing or unreadable file is treated as an empty ledger: follow-up is a
    helper and must not break the main flow.
    """
    ruta = ruta or _LEDGER_POR_DEFECTO
    if not os.path.isfile(ruta):
        return {}
    try:
        with open(ruta, "r", encoding="utf-8") as fh:
            datos = json.load(fh)
    except (OSError, ValueError):
        return {}
    return datos if isinstance(datos, dict) else {}


def marcar_seguimiento_hecho(id_, canal=None, nota=None, cuando=None, ruta=None):
    """Record that a follow-up has been sent for opportunity ``id_``.

    Writes to the local JSON (not SQLite, keeping the ``db.py`` schema intact)
    and returns the resulting entry dict.

    Args:
        id_: opportunity identifier.
        canal: "email", "linkedin", etc. (optional, informational).
        nota: free text (optional).
        cuando: datetime or ISO string; defaults to now in UTC.
        ruta: alternative ledger path (used in tests).
    """
    if not id_:
        raise ValueError("marcar_seguimiento_hecho requiere un id no vacío.")
    fecha = _parsear_fecha(cuando) or _ahora_utc()
    ruta = ruta or _LEDGER_POR_DEFECTO

    registro = _cargar_ledger(ruta)
    previa = registro.get(str(id_)) or {}
    entrada = {
        "ultimo_seguimiento": fecha.isoformat(),
        "veces": int(previa.get("veces") or 0) + 1,
    }
    if canal:
        entrada["canal"] = canal
    if nota:
        entrada["nota"] = nota
    registro[str(id_)] = entrada

    directorio = os.path.dirname(ruta)
    if directorio and not os.path.isdir(directorio):
        os.makedirs(directorio, exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as fh:
        json.dump(registro, fh, ensure_ascii=False, indent=2, sort_keys=True)
    return entrada


# ---------------------------------------------------------------------------
# Pending follow-ups
# ---------------------------------------------------------------------------

def seguimientos_pendientes(
    dias=7,
    estados=ESTADOS_ENVIADA,
    path=None,
    ledger_path=None,
    incluir_ya_seguidas=False,
):
    """List sent applications with ``dias`` days without movement.

    Criterion (conservative):
      * Only states in ``estados`` (``enviada`` and ``aplicada`` by default).
      * The reference date is the most recent of sent date, ``updated_at`` and
        ``created_at``; without any, the opportunity is dropped from the result
        (age is never invented).
      * At least ``dias`` full days must have passed.
      * Items already flagged/recorded as followed up are excluded unless
        ``incluir_ya_seguidas=True``.

    Returns a list of dicts: a copy of each opportunity with the added keys
    ``_dias_desde_contacto`` (float), ``_fecha_referencia`` (ISO) and
    ``_seguimiento_previo`` (dict or None).

    Args:
        dias: threshold in days (7 by default). Must be >= 0.
        estados: states considered "sent".
        path: alternative SQLite path (``DB_PATH`` by default).
        ledger_path: alternative follow-up JSON ledger path.
        incluir_ya_seguidas: if True, skip the flag/ledger filter.
    """
    try:
        dias = float(dias)
    except (TypeError, ValueError):
        raise ValueError("dias debe ser un número.")
    if dias < 0:
        raise ValueError("dias no puede ser negativo.")

    if isinstance(estados, str):
        estados = (estados,)
    estados = tuple(estados or ())

    ledger = _cargar_ledger(ledger_path)
    ahora = _ahora_utc()
    pendientes = []

    for estado in estados:
        # db.listar filters by state and orders by created_at descending.
        for op in db.listar(estado=estado, path=path):
            if not op:
                continue
            campo, fecha = _fecha_referencia(op)
            if fecha is None:
                continue

            antiguedad = (ahora - fecha).total_seconds() / 86400.0
            if antiguedad < dias:
                continue

            bruto = ledger.get(str(op.get("id")))
            entrada = bruto if isinstance(bruto, dict) else None
            fecha_seguimiento = _parsear_fecha(
                (entrada or {}).get("ultimo_seguimiento")
            )
            ya_seguida = _flag_hecho(op) or (
                fecha_seguimiento is not None and fecha_seguimiento >= fecha
            )
            if ya_seguida and not incluir_ya_seguidas:
                continue

            item = dict(op)
            item["_dias_desde_contacto"] = round(antiguedad, 1)
            item["_fecha_referencia"] = fecha.isoformat()
            item["_campo_fecha"] = campo
            item["_seguimiento_previo"] = entrada
            pendientes.append(item)

    # Oldest first: those are the most urgent to resume.
    pendientes.sort(key=lambda o: o["_dias_desde_contacto"], reverse=True)
    return pendientes


# ---------------------------------------------------------------------------
# Follow-up message drafting
# ---------------------------------------------------------------------------

def _resumen_perfil(perfil):
    """Extract only verified profile data for the prompt.

    Nothing outside ``perfil.json`` is used: the model must not fill gaps.
    Returns a short string used by the Spanish prompt.
    """
    perfil = perfil or {}
    identidad = perfil.get("identity") or {}
    lineas = []
    if identidad.get("nombre"):
        lineas.append("Nombre: " + str(identidad["nombre"]))
    if perfil.get("headline"):
        lineas.append("Perfil: " + str(perfil["headline"]))
    if identidad.get("linkedin"):
        lineas.append("LinkedIn: " + str(identidad["linkedin"]))
    if identidad.get("portfolio"):
        lineas.append("Portfolio: " + str(identidad["portfolio"]))
    return "\n".join(lineas) if lineas else "(sin datos de perfil)"


def redactar_seguimiento(op, perfil, dias=None, canal="email", max_tokens=2000):
    """Draft a short, polite, conservative follow-up message.

    Sends nothing: returns the text for human review. The prompt explicitly
    forbids inventing data (dates, contact names, processes, salaries) and asks
    not to insist or pressure.

    Args:
        op: opportunity dict (ideally a row from ``seguimientos_pendientes``,
            with ``titulo``, ``empresa``, etc.).
        perfil: profile dict (``data/perfil.json``).
        dias: days since sending; when None it is derived from
              ``_dias_desde_contacto`` or from the dates in ``op``.
        canal: "email" or "linkedin" (adjusts length and greeting).
        max_tokens: response budget for the model.

    Returns:
        The message string (no subject). Raises ``ValueError`` on missing
        minimum data and ``llm.LLMError`` if the call fails.
    """
    if not isinstance(op, dict) or not op:
        raise ValueError("redactar_seguimiento requiere un dict de oportunidad.")

    titulo = (op.get("titulo") or "").strip()
    empresa = (op.get("empresa") or "").strip()
    if not titulo and not empresa:
        raise ValueError(
            "La oportunidad no tiene 'titulo' ni 'empresa': no hay contexto "
            "suficiente para redactar un seguimiento sin inventar datos."
        )

    if dias is None:
        dias = op.get("_dias_desde_contacto")
    if dias is None:
        _, fecha = _fecha_referencia(op)
        if fecha is not None:
            dias = round((_ahora_utc() - fecha).total_seconds() / 86400.0, 1)
    dias_txt = (
        f"{dias:g} días" if isinstance(dias, (int, float)) else "varios días"
    )

    canal = (canal or "email").lower()
    if canal == "linkedin":
        formato = (
            "Mensaje para LinkedIn: máximo 90 palabras, sin asunto, saludo "
            "breve y cierre cordial."
        )
    else:
        formato = (
            "Correo electrónico: incluye una línea de asunto al principio "
            "(prefijada con 'Asunto:'), cuerpo de 80-120 palabras."
        )

    prompt = f"""Redacta un mensaje de seguimiento de una candidatura ya enviada.

DATOS REALES (los únicos permitidos):
- Puesto: {titulo or "(no consta)"}
- Empresa: {empresa or "(no consta)"}
- Días desde el envío: {dias_txt}
- Persona candidata:
{_resumen_perfil(perfil)}

REGLAS ESTRICTAS:
1. No inventes nombres de personas, fechas exactas, referencias de proceso,
   nombres de proyectos ni condiciones económicas. Si falta un dato, omítelo.
2. Tono profesional, educado y prudente: recordar el interés, preguntar si
   necesitan algo más y ofrecerse a ampliar información. Sin insistir ni
   presionar, sin reproches y sin plazos inventados.
3. No repitas la carta de presentación ni enumeres el CV; una sola frase de
   encaje profesional como máximo.
4. Escribe en español de España, en primera persona, sin emojis.
5. {formato}
6. Saludo: si no consta el nombre de la persona destinataria, usa un saludo
   neutro ("Hola," o "Buenos días,"); no inventes nombres ni cargos.
7. Devuelve SÓLO el texto del mensaje, sin comentarios ni explicaciones.

Mensaje:"""

    mensaje = llm.chat(
        [
            {
                "role": "system",
                "content": (
                    "Escribes correos de seguimiento profesionales y sobrios. "
                    "Nunca inventas datos que no se te han dado."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=max_tokens,
    )

    mensaje = (mensaje or "").strip()
    # A truncated message (e.g. the model ran out of max_tokens while
    # reasoning) is unusable: failing loudly beats returning half an email.
    # The word threshold catches early cuts.
    if len(mensaje) < 20 or len(mensaje.split()) < 25:
        raise llm.LLMError(
            "El modelo devolvió un seguimiento vacío o truncado "
            f"({len(mensaje.split())} palabras). Aumenta max_tokens."
        )
    return mensaje


if __name__ == "__main__":
    # Read-only local check: how many follow-ups are pending.
    pendientes = seguimientos_pendientes()
    print(f"Seguimientos pendientes (7 días): {len(pendientes)}")
    for op in pendientes:
        print(
            "  -",
            op.get("id"),
            "|",
            op.get("empresa"),
            "|",
            op.get("titulo"),
            "|",
            op.get("_dias_desde_contacto"),
            "días",
        )
