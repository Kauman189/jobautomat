"""generate.py — Cover letter, tailored CV and message for an offer.

All functions use ``llm.chat`` and work with the real profile
(``data/perfil.json``) and the repository's base CV. The central rule is
**never invent**: the tailored CV only reorders and emphasizes content that
already exists in the base CV, and the letter only cites documented evidence.

Every document also goes through ``_verificar_y_humanizar``, which makes ONE
extra LLM call (bounded cost: one per document) with two tasks: remove or
conservatively rephrase any factual claim not backed by the profile or the
offer, and rewrite in a natural, active voice. As a deterministic safety net
(free), sentences whose numeric metrics do not appear in the evidence sources
are pruned.

Public functions:
    generar_cover_letter(job, perfil) -> str
    generar_cv_adaptado(job, perfil) -> str
    generar_mensaje(job, perfil) -> str

The public functions still return text (``str``); when possible they also
render the ATS-friendly PDF of the CV and the letter to
``../candidaturas/<id>/cv.pdf`` and ``cover.pdf`` and store the paths in
``job["cv_pdf"]`` / ``job["cover_pdf"]``. A PDF failure never interrupts text
generation.
"""

import json
import logging
import os
import re

import llm

_log = logging.getLogger(__name__)

# Optional PDF render: without reportlab the text pipeline must keep working,
# so the import cannot break the module.
try:
    import pdf_gen
except Exception:  # noqa: BLE001 — cualquier fallo de import degrada a "sin PDF".
    pdf_gen = None

# Project root (jobautomat/), resolved from service/.
_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUTA_CV_ES = os.path.join(_RAIZ, "assets", "cv", "base-cv-es.md")
_RUTA_CV_EN = os.path.join(_RAIZ, "assets", "cv", "base-cv-en.md")

# Per-job deliverables: ../candidaturas/<id>/cv.pdf and cover.pdf.
_DIR_CANDIDATURAS = os.environ.get("CANDIDATURAS_DIR", "").strip() or os.path.join(_RAIZ, "candidaturas")

# Words that reveal an English ad (to pick the CV and language).
_STOPWORDS_EN = {
    "the", "and", "with", "for", "you", "your", "our", "we", "are", "is",
    "role", "team", "will", "have", "experience", "design", "product",
    "about", "this", "that", "from", "work", "who", "what", "looking",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _texto(valor):
    """Turn any value into clean text ('' when None)."""
    if valor is None:
        return ""
    if isinstance(valor, (list, tuple)):
        return " ".join(str(v) for v in valor)
    return str(valor)


def _texto_oferta(job):
    """Concatenate the offer's textual fields."""
    claves = (
        "titulo", "title", "empresa", "company", "descripcion", "description",
        "requisitos", "requirements", "ubicacion", "location", "remoto",
        "remote", "contrato", "salario", "salary",
    )
    return " ".join(_texto(job.get(k)) for k in claves if job.get(k))


def idioma_oferta(job):
    """Return ``"es"`` or ``"en"`` from the declared or detected language.

    Prefers the explicit ``idioma``/``language`` field; otherwise counts English
    stopwords against Spanish signals in the text.
    """
    for clave in ("idioma", "language", "lang"):
        valor = _texto(job.get(clave)).lower()
        if valor.startswith("en"):
            return "en"
        if valor.startswith("es"):
            return "es"

    texto = _texto_oferta(job).lower()
    if not texto:
        return "es"

    palabras = re.findall(r"[a-záéíóúñü]+", texto)
    inglesas = sum(1 for p in palabras if p in _STOPWORDS_EN)
    espanolas = sum(
        1 for p in palabras if p in {"de", "para", "con", "que", "el", "la", "los", "las", "y", "en"}
    )
    # Accents and eñes are a strong Spanish signal.
    if re.search(r"[áéíóúñ¿¡]", texto):
        espanolas += 5
    return "en" if inglesas > espanolas else "es"


def _directorio_candidatura(job):
    """Return ``../candidaturas/<id>``, sanitizing the offer id.

    The id is stable (``h-<hash>``) but is sanitized anyway so an unexpected id
    cannot escape the applications directory.
    """
    ident = str((job or {}).get("id") or "").strip() or "sin-id"
    ident = re.sub(r"[^A-Za-z0-9._-]+", "_", ident).strip("._") or "sin-id"
    return os.path.join(_DIR_CANDIDATURAS, ident[:80])


def _funcion_pdf(nombre):
    """Return the requested ``pdf_gen`` function, or None without a renderer."""
    return getattr(pdf_gen, nombre, None) if pdf_gen is not None else None


def _adjuntar_pdf(job, texto, nombre_archivo, clave, generador):
    """Render the document PDF and store its path in ``job[clave]``.

    Designed to never break the pipeline: any error (missing reportlab, full
    disk, empty text) is logged as a warning and the generated text is kept.
    Returns the PDF path or ``None``.
    """
    try:
        if not texto or generador is None or not isinstance(job, dict):
            return None
        ruta = os.path.join(_directorio_candidatura(job), nombre_archivo)
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        generador(texto, ruta)
        if os.path.isfile(ruta):
            job[clave] = ruta
            return ruta
        _log.warning("PDF no escrito para %s: %s", job.get("id"), nombre_archivo)
    except Exception as err:  # noqa: BLE001 — el PDF es un extra, no el pipeline.
        _log.warning(
            "PDF %s no generado para %s: %s", nombre_archivo, (job or {}).get("id"), err
        )
    return None


def _verificar_pdf_cv(job, texto_cv, perfil, ruta_pdf):
    """Deterministic gate: re-extract the CV PDF text and look for hard data.

    Stores the result in ``job["pdf_check"]`` (``ok``/``faltantes``/
    ``paginas``/``motivo``) for the panel to display. Never blocks or raises:
    a CV without PDF, or a failure of the gate itself, is only logged.
    """
    if not isinstance(job, dict):
        return None
    verificador = _funcion_pdf("verificar_pdf_cv")
    if verificador is None:
        return None
    try:
        if not ruta_pdf:
            resultado = {
                "ok": False,
                "faltantes": [],
                "paginas": None,
                "motivo": "el CV no se renderizó a PDF",
            }
        else:
            resultado = verificador(texto_cv or "", ruta_pdf, perfil)
        job["pdf_check"] = resultado
        if not isinstance(resultado, dict):
            return resultado
        faltantes = resultado.get("faltantes") or []
        if faltantes:
            _log.warning(
                "Gate PDF del CV (%s): faltan %d datos duros en el PDF: %s",
                job.get("id"),
                len(faltantes),
                "; ".join(str(f) for f in faltantes),
            )
        elif resultado.get("ok") is False:
            _log.warning(
                "Gate PDF del CV (%s): %s", job.get("id"), resultado.get("motivo")
            )
        else:
            _log.info(
                "Gate PDF del CV (%s): OK (%s páginas, %s)",
                job.get("id"),
                resultado.get("paginas"),
                resultado.get("motivo") or "sin incidencias",
            )
        return resultado
    except Exception as err:  # noqa: BLE001 — el gate jamás tumba la candidatura.
        _log.warning(
            "Gate PDF del CV (%s) falló y se ignora: %s", (job or {}).get("id"), err
        )
        return None


def _leer_cv(idioma):
    """Read the base CV in the given language (Spanish fallback)."""
    ruta = _RUTA_CV_EN if idioma == "en" else _RUTA_CV_ES
    try:
        with open(ruta, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        # Fallback: try the other language before failing.
        alternativa = _RUTA_CV_ES if idioma == "en" else _RUTA_CV_EN
        with open(alternativa, "r", encoding="utf-8") as fh:
            return fh.read()


def _resumen_perfil(perfil):
    """Compact, faithful profile summary for the prompts."""
    perfil = perfil or {}
    lineas = [
        "NOMBRE: %s" % (perfil.get("identity") or {}).get("nombre", ""),
        "HEADLINE: %s" % perfil.get("headline", ""),
    ]
    pos = perfil.get("positioning_emphasis") or {}
    if pos.get("declaracion_principal"):
        lineas.append("POSICIONAMIENTO: %s" % pos["declaracion_principal"])
    if pos.get("matiz_liderazgo"):
        lineas.append("MATIZ DE LIDERAZGO: %s" % pos["matiz_liderazgo"])

    for exp in perfil.get("experience") or []:
        lineas.append(
            "- %s en %s (%s, %s)"
            % (
                exp.get("titulo_cv", ""),
                exp.get("empresa", ""),
                exp.get("periodo", ""),
                exp.get("ubicacion", ""),
            )
        )
        for bullet in (exp.get("bullets") or [])[:4]:
            lineas.append("  · %s" % bullet)
        if exp.get("nota"):
            lineas.append("  (límite: %s)" % exp["nota"])

    for proj in perfil.get("projects") or []:
        lineas.append(
            "- Proyecto %s (%s): %s"
            % (proj.get("nombre", ""), proj.get("rol", ""), proj.get("descripcion", ""))
        )

    skills = perfil.get("skills") or {}
    for grupo, items in skills.items():
        lineas.append("SKILLS %s: %s" % (grupo, ", ".join(items)))

    idiomas = perfil.get("languages") or {}
    lineas.append(
        "IDIOMAS: "
        + "; ".join(
            "%s=%s" % (k, (v or {}).get("nivel") or "sin nivel acreditado")
            for k, v in idiomas.items()
        )
    )
    return "\n".join(lineas)


def _bloque_oferta(job):
    """Format the offer for inclusion in a prompt."""
    descripcion = _texto(job.get("descripcion") or job.get("description")).strip()
    if len(descripcion) > 6000:
        descripcion = descripcion[:6000] + "\n[...recortado...]"
    return (
        "Título: %s\nEmpresa: %s\nUbicación: %s\nModalidad: %s\n"
        "Salario: %s\nContrato: %s\nURL: %s\nDescripción:\n%s"
        % (
            _texto(job.get("titulo") or job.get("title")) or "(sin título)",
            _texto(job.get("empresa") or job.get("company")) or "(sin empresa)",
            _texto(job.get("ubicacion") or job.get("location")) or "(sin ubicación)",
            _texto(job.get("remoto") or job.get("remote")) or "(no indicada)",
            _texto(job.get("salario") or job.get("salary")) or "(no publicado)",
            _texto(job.get("contrato")) or "(no indicado)",
            _texto(job.get("url")) or "(sin URL)",
            descripcion or "(sin descripción)",
        )
    )


def _limpiar_salida(texto):
    """Strip markdown fences and surrounding whitespace from the reply."""
    texto = (texto or "").strip()
    match = re.match(r"^```(?:markdown|md|text)?\s*(.*?)\s*```$", texto, flags=re.DOTALL)
    if match:
        texto = match.group(1).strip()
    return texto


def _chat(sistema, usuario, temperature=0.6, max_tokens=1800):
    """Call ``llm.chat``, retrying once when the budget runs out.

    Reasoning models (deepseek-flash) spend tokens in ``reasoning_content``
    before writing the answer. If the first call ends by length, it retries
    once with double the budget before failing.
    """
    mensajes = [
        {"role": "system", "content": sistema},
        {"role": "user", "content": usuario},
    ]
    try:
        respuesta = llm.chat(
            mensajes, temperature=temperature, max_tokens=max_tokens
        )
    except llm.LLMError as err:
        if "max_tokens" not in str(err) and "vacío" not in str(err):
            raise
        respuesta = llm.chat(
            mensajes, temperature=temperature, max_tokens=max_tokens * 2
        )
    return _limpiar_salida(respuesta)


# ---------------------------------------------------------------------------
# Anti-invention verification + humanization
# ---------------------------------------------------------------------------

# Document types accepted by the verifier.
TIPO_CARTA = "carta"
TIPO_CV = "cv"
TIPO_MENSAJE = "mensaje"

# Minimum budget of the verification call. DeepSeek-flash reasons before
# answering and spends tokens in ``reasoning_content``: below ~400 the content
# arrives empty with finish_reason='length'.
_MAX_TOKENS_VERIFICACION_MIN = 2000
_MAX_TOKENS_VERIFICACION_MAX = 12000

# "Metric" patterns: figures that are often invented (percentages, years of
# experience, multipliers, amounts). Dates and bare years are not pruned, to
# leave accredited experience untouched.
_PATRONES_METRICA = (
    re.compile(r"\b\d+(?:[.,]\d+)?\s?%"),
    re.compile(r"\b\d+(?:[.,]\d+)?\s*por\s?ciento\b", re.IGNORECASE),
    re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:\+|m[áa]s)?\s*a[ñn]os?\b", re.IGNORECASE),
    re.compile(r"\+\s*\d+(?:[.,]\d+)?"),
    re.compile(r"\b\d+(?:[.,]\d+)?\s*[xX]\b"),
    re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:k|K|mil|millones?)\b"),
    re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:€|EUR|USD)\b"),
    re.compile(r"(?:€|EUR|USD)\s?\d+(?:[.,]\d+)?"),
)

_SISTEMA_VERIFICACION = """Eres un verificador editorial y corrector de estilo. \
Recibes un texto generado por otra IA, el perfil verificado del candidato y \
la oferta. Tienes dos tareas y devuelves ÚNICAMENTE el texto final.

TAREA A — verificación factual (prioritaria):
1. Localiza toda afirmación factual: fechas, años de experiencia, métricas \
(porcentajes, importes, multiplicadores), salario, herramientas y tecnologías, \
títulos de puesto, niveles (junior/senior/lead), idiomas, empresas, clientes, \
certificaciones y sectores.
2. Comprueba cada una contra el PERFIL y la OFERTA. Si no está respaldada, \
ELIMÍNALA o reformúlala de forma conservadora y cualitativa ("mejoré el flujo \
de compra" en lugar de "aumenté la conversión un 40%"). Nunca añadas datos \
nuevos ni sustituyas una cifra por otra.
3. No inventes métricas, logros ni responsabilidades para "compensar" lo que \
borras. Si un párrafo se queda sin sustancia respaldada, elimínalo entero.

TAREA B — humanización:
- Voz activa y primera persona; evita pasiva y construcciones impersonales.
- Sin clichés ni relleno: "apasionado", "proactivo", "sinergias", "dinámico", \
"resultados tangibles", "en constante aprendizaje", "sólida trayectoria", \
"aprovechar al máximo", "no dudes en contactarme".
- Frases de longitud variada, verbos concretos, cero entusiasmo exagerado.
- No añadas introducciones meta ("Aquí tienes el texto revisado:"), ni \
comentarios, ni markdown nuevo.

Devuelve sólo el documento final, en el mismo idioma, formato y extensión."""

# Words a document cannot end with: when the verifier's reply ends in one of
# them, it was cut by the token budget.
_PALABRAS_SIN_CIERRE = {
    "de", "del", "el", "la", "los", "las", "un", "una", "unos", "unas", "y",
    "o", "u", "que", "con", "para", "por", "en", "mi", "mis", "su", "sus",
    "al", "se", "es", "son", "a", "como", "más", "sin", "sobre", "entre",
    "cuando", "donde", "mientras", "muy", "the", "and", "with", "for", "to",
    "of", "in", "my", "your", "our", "that", "is", "are",
}

_INSTRUCCIONES_TIPO = {
    TIPO_CARTA: (
        "Carta de presentación: texto plano, sin markdown, sin encabezados ni "
        "listas, 120-180 palabras, firma con el nombre del candidato."
    ),
    TIPO_CV: (
        "CV en markdown: conserva TODOS los puestos, proyectos y formación, "
        "las fechas y los títulos exactamente como están, y el mismo formato "
        "(encabezados, negritas, bullets). Puedes acortar bullets, nunca "
        "borrar un puesto."
    ),
    TIPO_MENSAJE: (
        "Mensaje corto de contacto: máximo 300 caracteres con espacios, 2 "
        "frases, sin saludo corporativo ni firma extensa."
    ),
}


def _fuentes_evidencia(job, perfil, tipo):
    """Return the textual corpus backing the document's claims.

    Includes the full serialized profile, the offer's textual fields and, for
    the CV, the base CV of the matching language (the real source of dates,
    titles and bullets).
    """
    partes = []
    try:
        partes.append(json.dumps(perfil or {}, ensure_ascii=False))
    except (TypeError, ValueError):
        partes.append(str(perfil or {}))
    partes.append(_texto_oferta(job or {}))
    if tipo == TIPO_CV:
        try:
            partes.append(_leer_cv(idioma_oferta(job or {})))
        except OSError:
            pass
    return "\n".join(p for p in partes if p)


def _numero_respaldado(numero, fuentes):
    """True if the figure appears literally in the sources (with bounds)."""
    patron = r"(?<![\d.,])" + re.escape(numero) + r"(?![\d])"
    if re.search(patron, fuentes):
        return True
    # Decimal variant with dot: 4,5% <-> 4.5%
    alternativo = numero.replace(",", ".") if "," in numero else numero.replace(".", ",")
    if alternativo != numero:
        patron = r"(?<![\d.,])" + re.escape(alternativo) + r"(?![\d])"
        return re.search(patron, fuentes) is not None
    return False


def _afirmaciones_no_respaldadas(texto, job, perfil, tipo):
    """List the text's metrics that do not appear in the sources.

    Cheap deterministic check (no LLM) acting as a safety net when the model
    does not fulfil the verification task. Returns the literal fragments found
    (e.g. ``"un 40%"``).
    """
    texto = texto or ""
    if not texto.strip():
        return []
    fuentes = _fuentes_evidencia(job, perfil, tipo)
    hallazgos = []
    vistos = set()
    for patron in _PATRONES_METRICA:
        for match in patron.finditer(texto):
            fragmento = match.group(0).strip()
            numeros = re.findall(r"\d+(?:[.,]\d+)?", fragmento)
            if not numeros:
                continue
            if all(_numero_respaldado(n, fuentes) for n in numeros):
                continue
            clave = fragmento.lower()
            if clave in vistos:
                continue
            vistos.add(clave)
            hallazgos.append(fragmento)
    return hallazgos


def _podar_afirmaciones_no_respaldadas(texto, job, perfil, tipo):
    """Drop sentences with metrics not backed by the sources.

    Works line by line (to respect markdown bullets) and, inside each line,
    sentence by sentence. If pruning leaves the document empty, the original
    text is returned: an imperfect document beats a blank one. Nothing is added
    or rewritten.
    """
    malas = _afirmaciones_no_respaldadas(texto, job, perfil, tipo)
    if not malas:
        return texto

    def _frase_sucia(frase):
        return any(mala.lower() in frase.lower() for mala in malas)

    lineas_salida = []
    for linea in texto.splitlines():
        if not linea.strip():
            lineas_salida.append(linea)
            continue
        # Split sentences keeping the markdown prefix of the line.
        frases = re.split(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑÜ¿¡\"'(\[])", linea)
        conservadas = [f for f in frases if not _frase_sucia(f)]
        if not conservadas:
            continue  # the whole line was an invented claim
        lineas_salida.append(" ".join(conservadas).rstrip())

    podado = "\n".join(lineas_salida)
    podado = re.sub(r"[ \t]{2,}", " ", podado)
    podado = re.sub(r"\n{3,}", "\n\n", podado).strip()
    return podado or texto


def _presupuesto_verificacion(texto):
    """Tokens for the verification call, proportional to the document."""
    estimado = len(texto or "") // 2 + 900
    return max(_MAX_TOKENS_VERIFICACION_MIN, min(_MAX_TOKENS_VERIFICACION_MAX, estimado))


def _parece_truncado(texto, tipo):
    """True if the verifier's reply was cut mid-sentence.

    A final period is not enough: letters usually close with the signature
    (the candidate's name), without punctuation. Text ending in a comma, a hyphen or
    a function word (article, preposition, conjunction) counts as truncated,
    which is the real pattern of a ``max_tokens`` cut. The CV is treated
    permissively because a markdown bullet may end without a period (the length
    guard already protects against aggressive summaries).
    """
    limpio = (texto or "").strip()
    if not limpio:
        return True
    if tipo == TIPO_CV:
        return False
    if limpio[-1] in ".!?:\"”»)*_]":
        return False
    if limpio[-1] in ",-–—":
        return True
    ultima = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+$", limpio)
    if not ultima:
        return False
    return ultima[0].lower() in _PALABRAS_SIN_CIERRE


# Closing sections the base CV always has. If the tailored CV lost any, the
# reply was cut by ``max_tokens`` (the real pattern is ending right at the
# "## SELECTED PROJECTS" heading).
_CLAVES_CIERRE_CV = (
    "education",
    "educación",
    "certification",
    "certificación",
    "idioma",
    "language",
)


def _cv_truncado(cv, base):
    """True if the tailored CV lost the base CV's final sections.

    ``_parece_truncado`` is deliberately permissive with the CV (a markdown
    bullet may close without a period), so the real cut is detected by
    comparing against the base CV: either the result is much shorter than
    expected or one of the closing sections is gone (SELECTED PROJECTS /
    EDUCATION / CERTIFICATIONS & LANGUAGES).

    When in doubt it returns True: a complete, honest base CV beats a cut
    tailored one.
    """
    base = base or ""
    cv = (cv or "").strip()
    if not base:
        return False
    if not cv:
        return True
    if len(cv) < len(base) * 0.7:
        return True
    cv_lower = cv.lower()
    base_lower = base.lower()
    for clave in _CLAVES_CIERRE_CV:
        if clave in base_lower and clave not in cv_lower:
            return True
    return False


def _desenvolver_salida(texto):
    """Strip fences, wrapping quotes and meta prefaces from the model."""
    texto = _limpiar_salida(texto)
    # "Aquí tienes el texto revisado:" and similar one-line courtesies.
    texto = re.sub(
        r"^(?:aqu[íi] (?:tienes|est[áa])[^\n:]*:|texto (?:revisado|final)\s*:)\s*",
        "",
        texto,
        flags=re.IGNORECASE,
    ).strip()
    if len(texto) > 1 and texto[0] in "\"“" and texto[-1] in "\"”":
        texto = texto[1:-1].strip()
    return texto


def _verificar_y_humanizar(texto, job, perfil, tipo):
    """Verify and humanize a generated document with ONE LLM call.

    Contract:
        - Input: generated ``texto``, ``job`` (normalized offer), ``perfil``
          (dict from ``data/perfil.json``) and ``tipo`` (``"carta"``, ``"cv"``
          or ``"mensaje"``).
        - Output: ``str`` with the final document (never None). Keeps language,
          format and approximate length; it may be shorter because unsupported
          claims are removed.
        - The LLM call uses low temperature and a token budget proportional to
          the text (always >= 400).
        - Graceful degradation: if the LLM fails (``llm.LLMError``) or returns
          something unusable (empty, truncated), the original text is returned
          without breaking the pipeline.
        - Cost: exactly one extra call per document.
        - No secrets are included or logged: only profile, offer and text.
    """
    texto = (texto or "").strip()
    if not texto:
        return texto

    tipo = tipo if tipo in _INSTRUCCIONES_TIPO else TIPO_CARTA
    usuario = """PERFIL VERIFICADO DEL CANDIDATO (única fuente sobre el candidato)
{perfil}

OFERTA (única fuente sobre el puesto)
{oferta}

TIPO DE DOCUMENTO
{tipo}

TEXTO A REVISAR
{texto}

TAREA
Aplica la verificación factual y la humanización. Devuelve sólo el documento
final, sin comentarios.""".format(
        perfil=_resumen_perfil(perfil),
        oferta=_bloque_oferta(job or {}),
        tipo=_INSTRUCCIONES_TIPO[tipo],
        texto=texto,
    )

    try:
        revisado = _chat(
            _SISTEMA_VERIFICACION,
            usuario,
            temperature=0.2,
            max_tokens=_presupuesto_verificacion(texto),
        )
    except llm.LLMError:
        # Without verification the application is not blocked: use the original.
        return texto
    except Exception:  # noqa: BLE001 — any verifier failure degrades.
        return texto

    revisado = _desenvolver_salida(revisado)
    if not revisado:
        return texto

    # Guards: reply truncated by max_tokens, or one that "summarizes" the
    # document. In both cases the rewrite is discarded and the original (pruned
    # of unsupported metrics) is returned, without spending another call.
    minimo = 0.6 if tipo == TIPO_CV else 0.35
    if _parece_truncado(revisado, tipo) or len(revisado) < len(texto) * minimo:
        return _podar_afirmaciones_no_respaldadas(texto, job, perfil, tipo)

    # Deterministic safety net: if the model let an unsupported metric
    # through, the sentence holding it is pruned (no extra cost).
    return _podar_afirmaciones_no_respaldadas(revisado, job, perfil, tipo)


# ---------------------------------------------------------------------------
# Cover letter
# ---------------------------------------------------------------------------

_SISTEMA_CARTA = """Escribes cartas de presentación breves para el candidato, \
Product Designer (UI/UX) y desarrollador frontend. Reglas estrictas:

- Texto plano, sin markdown, sin encabezados ni listas.
- 120-180 palabras. Saludo al equipo de la empresa, 2-3 párrafos y cierre.
- Abre con una conexión concreta con lo que pide el anuncio (no elogios
  genéricos ni afirmaciones sobre la empresa que no estén en el anuncio).
- Usa verbos de acción en primera persona: diseñé, implementé, lideré,
  traduje, documenté.
- Cita sólo evidencia que aparezca en el perfil o en el CV aportados.
- Prohibido: métricas inventadas, buzzwords ("apasionado", "sinergias",
  "proactivo"), listas de tecnologías, afirmar cargos no acreditados
  (fundador, Senior Product Designer, responsable de personas).
- Cierra con un siguiente paso natural (revisar el portfolio del candidato o
  una conversación breve). Sin entusiasmo exagerado.
- Escribe en el idioma indicado."""


def generar_cover_letter(job, perfil):
    """Generate a cover letter tailored to the offer.

    Returns the letter's plain text (no subject or metadata). The result goes
    through ``_verificar_y_humanizar`` (one extra call) and the original text
    is returned if that verification fails.

    When reportlab is available it also writes the PDF to
    ``../candidaturas/<id>/cover.pdf`` and its path to ``job["cover_pdf"]``.
    A PDF render failure does not change the returned text.
    """
    idioma = idioma_oferta(job)
    nombre_idioma = "inglés" if idioma == "en" else "español"
    usuario = """PERFIL DEL CANDIDATO
{perfil}

OFERTA
{oferta}

TAREA
Escribe la carta de presentación en {idioma}. Elige dos o tres evidencias
reales del perfil que respondan a lo que pide el anuncio. Firma con el
nombre del candidato. Devuelve sólo el texto de la carta.""".format(
        perfil=_resumen_perfil(perfil),
        oferta=_bloque_oferta(job),
        idioma=nombre_idioma,
    )
    carta = _verificar_y_humanizar(
        _chat(_SISTEMA_CARTA, usuario, temperature=0.6, max_tokens=3000),
        job,
        perfil,
        TIPO_CARTA,
    )
    _adjuntar_pdf(job, carta, "cover.pdf", "cover_pdf", _funcion_pdf("generar_pdf_cover"))
    return carta


# ---------------------------------------------------------------------------
# Tailored CV
# ---------------------------------------------------------------------------

_SISTEMA_CV = """Adaptas CVs sin inventar contenido. Recibes un CV base en \
markdown y una oferta. Devuelves el CV completo en markdown.

Reglas estrictas:
- No añadas experiencia, empresas, fechas, títulos, certificaciones,
  idiomas, skills ni métricas que no estén en el CV base.
- Sí puedes: reordenar secciones, reescribir el resumen profesional para
  conectar con el puesto, priorizar los bullets más relevantes dentro de
  cada puesto y agrupar skills por relevancia.
- Mantén TODOS los puestos, proyectos y formación del CV base: no elimines
  trayectoria. Puedes acortar bullets secundarios, nunca borrar un puesto.
- Conserva las fechas y los títulos exactamente como aparecen.
- Respeta el matiz de liderazgo funcional y no eleves el nivel del puesto.
- Mismo idioma que el CV base. Mantén el formato markdown (encabezados,
  negritas y bullets) y una extensión similar.
- El CV debe caber en MÁXIMO 2 páginas A4.
- Sé conciso: acorta o prioriza bullets antes que eliminar puestos; no
  añadas secciones nuevas.
- Devuelve sólo el CV, sin comentarios ni explicaciones."""


def generar_cv_adaptado(job, perfil):
    """Generate a version of the base CV reordered for the offer.

    Reads ``assets/cv/base-cv-es.md`` or ``base-cv-en.md`` depending on the ad's
    language and returns the full CV in markdown. The result goes through
    ``_verificar_y_humanizar`` (one extra call, with the base CV as evidence
    source); if that verification fails the original text is returned.

    When reportlab is available it also writes the PDF to
    ``../candidaturas/<id>/cv.pdf`` and its path to ``job["cv_pdf"]``. A PDF
    render failure does not change the returned text.
    """
    idioma = idioma_oferta(job)
    cv_base = _leer_cv(idioma)
    usuario = """CV BASE
{cv}

PERFIL ESTRUCTURADO (referencia de evidencia, no añadir nada que no esté
en el CV base)
{perfil}

OFERTA
{oferta}

TAREA
Adapta el CV base a esta oferta: reescribe el resumen profesional para
destacar el encaje, reordena las secciones y prioriza los bullets y skills
más relevantes. No inventes nada. Devuelve el CV completo en markdown.""".format(
        cv=cv_base,
        perfil=_resumen_perfil(perfil),
        oferta=_bloque_oferta(job),
    )
    cv = _verificar_y_humanizar(
        _chat(_SISTEMA_CV, usuario, temperature=0.4, max_tokens=8000),
        job,
        perfil,
        TIPO_CV,
    )
    # If the adaptation (or its verification) was cut and lost the final
    # sections, fall back to the full base CV: whole and honest beats cut.
    if _cv_truncado(cv, cv_base):
        _log.warning(
            "CV adaptado truncado (%d chars frente a %d del base): "
            "se usa el CV base completo.",
            len(cv or ""),
            len(cv_base or ""),
        )
        cv = cv_base
    ruta_cv = _adjuntar_pdf(
        job, cv, "cv.pdf", "cv_pdf", _funcion_pdf("generar_pdf_cv")
    )
    # Deterministic (non-blocking) gate: checks the PDF keeps the profile's
    # hard data and stores the result in job["pdf_check"].
    _verificar_pdf_cv(job, cv, perfil, ruta_cv)
    return cv


# ---------------------------------------------------------------------------
# Short message (LinkedIn / email)
# ---------------------------------------------------------------------------

_SISTEMA_MENSAJE = """Escribes mensajes breves de contacto para LinkedIn en \
nombre del candidato, Product Designer (UI/UX) y desarrollador frontend.

- Máximo 300 caracteres con espacios (límite de nota de conexión).
- 2 frases: por qué escribe sobre ese puesto concreto y una evidencia real.
- Tono natural y directo, sin buzzwords ni métricas inventadas.
- Sin enlaces largos; como mucho el portfolio del candidato.
- Sin saludo corporativo ni firma extensa.
- Escribe en el idioma indicado. Devuelve sólo el mensaje."""


def generar_mensaje(job, perfil):
    """Generate a short message to accompany the application.

    Returns the message text (max 300 characters). The result goes through
    ``_verificar_y_humanizar`` (one extra call) and the original text is
    returned if that verification fails.
    """
    idioma = idioma_oferta(job)
    nombre_idioma = "inglés" if idioma == "en" else "español"
    usuario = """PERFIL
{perfil}

OFERTA
{oferta}

TAREA
Escribe el mensaje en {idioma} (máximo 300 caracteres).""".format(
        perfil=_resumen_perfil(perfil),
        oferta=_bloque_oferta(job),
        idioma=nombre_idioma,
    )
    return _verificar_y_humanizar(
        _chat(_SISTEMA_MENSAJE, usuario, temperature=0.7, max_tokens=2500),
        job,
        perfil,
        TIPO_MENSAJE,
    )


if __name__ == "__main__":
    # Self-check with a fictitious offer.
    with open(
        os.path.join(_RAIZ, "data", "perfil.json"), "r", encoding="utf-8"
    ) as fh:
        _perfil = json.load(fh)
    _job = {
        "titulo": "Product Designer (Remote)",
        "empresa": "Acme",
        "remoto": "100% remoto",
        "descripcion": "Diseño de producto en Figma para SaaS B2B; design systems y colaboración con frontend Angular.",
    }
    print(generar_cover_letter(_job, _perfil))
