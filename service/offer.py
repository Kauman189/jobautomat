"""offer.py — Analysis of a job offer received.

Standalone module: does not modify ``main.py`` or any other service module.
Given the text of an offer (email, pasted PDF, recruiter message) and the
candidate profile, returns a structured, conservative analysis:

    {
      "salario_ofrecido": {min, max, moneda, periodo, bruto_neto, texto,
                           verificado_en_texto},
      "salario_objetivo": {min, max, moneda, concepto, referencia_anual},
      "gap_pct": float | None,      # (offered - target) / target * 100
      "gap_eur": float | None,      # annual difference in the profile currency
      "clausulas": [{clausula, detalle, riesgo}],
      "preguntas_abogado": [str],
      "recomendacion": str,
      "supuestos": [str]
    }

Principles:
  * The LLM only EXTRACTS what the text says and writes questions; it never
    computes figures or fills gaps. The gap is computed here, in Python.
  * Missing data returns ``None`` and is reported in ``supuestos``; no
    "reasonable" salary is ever estimated.
  * ``preguntas_abogado`` are questions for a professional to review the
    contract, not legal advice.
  * Nothing is sent or answered automatically.

``perfil["compensation"]`` keys used are those of ``data/perfil.json``:
``minimo``, ``objetivo`` (e.g. "40k-45k"), ``moneda`` and ``concepto``.
"""

import json
import os
import re

import llm

# Maximum length of the text sent to the model (cost control).
_MAX_TEXTO = 12000

# Allowed clause risk levels; any other value degrades to "no_valorable"
# so nothing is exaggerated or downplayed.
_RIESGOS = ("alto", "medio", "bajo", "no_valorable")

_ESQUEMA_ESPERADO = """{
  "salario_ofrecido": {
    "min": numero | null,
    "max": numero | null,
    "moneda": "EUR" | "USD" | ... | null,
    "periodo": "anual" | "mensual" | "diario" | "hora" | null,
    "bruto_neto": "bruto" | "neto" | null,
    "texto": "cita literal del salario" | null
  },
  "clausulas": [
    {"clausula": "nombre corto", "detalle": "qué dice la oferta, en una frase",
     "riesgo": "alto" | "medio" | "bajo" | "no_valorable"}
  ],
  "preguntas_abogado": ["pregunta concreta para un abogado"],
  "recomendacion": "2-4 frases prudentes",
  "supuestos": ["dato que falta o es ambiguo y cómo se ha tratado"]
}"""


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------

def _a_numero(valor):
    """Convert a model value ("40.000", "40k", 40000) to float.

    Returns ``None`` when not convertible. Never rounds or completes figures.
    """
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip().lower().replace("\u00a0", "")
    if not texto:
        return None
    multiplicador = 1.0
    if texto.endswith("k"):
        multiplicador = 1000.0
        texto = texto[:-1].strip()
    # Spanish format: dot as thousands separator, comma as decimal.
    texto = texto.replace(".", "").replace(",", ".")
    texto = re.sub(r"[^0-9.]", "", texto)
    if not texto:
        return None
    try:
        return float(texto) * multiplicador
    except ValueError:
        return None


def _parsear_rango_objetivo(texto):
    """Extract (min, max) from a range like "40k-45k" or "40.000 - 45.000".

    A single figure yields equal min and max. Returns (None, None) when no
    figure is recognizable.
    """
    if texto is None:
        return None, None
    if isinstance(texto, (int, float)):
        return float(texto), float(texto)
    crudo = str(texto).replace("\u00a0", " ")
    # Each figure may carry a k/K suffix.
    piezas = re.findall(r"(\d+(?:[.,]\d+)?)\s*([kK])?", crudo)
    cifras = []
    for numero, sufijo in piezas:
        valor = _a_numero(numero + ("k" if sufijo else ""))
        if valor is not None:
            cifras.append(valor)
    if not cifras:
        return None, None
    if len(cifras) == 1:
        return cifras[0], cifras[0]
    return min(cifras[0], cifras[1]), max(cifras[0], cifras[1])


def _a_anual(cifra, periodo):
    """Convert a figure to its annual equivalent for the given period.

    Unknown or missing ``periodo`` is treated as annual (same as the profile);
    the caller records that assumption in ``supuestos``.
    """
    if cifra is None:
        return None
    periodo = (periodo or "anual").lower()
    factores = {"anual": 1, "mensual": 12, "diario": 220, "hora": 1760}
    factor = factores.get(periodo, 1)
    return cifra * factor


def _normalizar_para_busqueda(texto):
    """Normalize text for figure comparison: no dots, spaces or €."""
    texto = (texto or "").lower().replace("\u00a0", " ")
    return re.sub(r"[.\s€$]", "", texto)


def _cifra_en_texto(cifra, texto):
    """True if ``cifra`` appears recognizably in ``texto``.

    Accepts "40000", "40.000" and "40k" (and decimal variants). It is a
    plausibility check, not an accounting validation.
    """
    if cifra is None:
        return False
    normalizado = _normalizar_para_busqueda(texto)
    candidatos = set()
    entero = int(round(cifra))
    candidatos.add(str(entero))
    if cifra == entero and entero % 1000 == 0:
        candidatos.add(f"{entero // 1000}k")
    # Decimal-comma forms (e.g. 40500.5 or 40,5k).
    if cifra != entero:
        candidatos.add(str(cifra).replace(".", ","))
        candidatos.add(str(cifra))
    return any(c in normalizado for c in candidatos if c)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

def _datos_objetivo(perfil):
    """Return the normalized target salary from the profile.

    Uses ``perfil["compensation"]``: ``objetivo`` (a range) or, if missing, the
    declared ``minimo``. The gap reference is the low end of the target range
    (conservative: never compared against the best case).
    """
    compensacion = (perfil or {}).get("compensation") or {}
    moneda = compensacion.get("moneda") or "EUR"
    concepto = compensacion.get("concepto") or ""

    minimo, maximo = _parsear_rango_objetivo(compensacion.get("objetivo"))
    if minimo is None:
        minimo = _a_numero(compensacion.get("minimo"))
        if minimo is not None and maximo is None:
            maximo = minimo

    referencia = minimo
    return {
        "min": minimo,
        "max": maximo,
        "moneda": moneda,
        "concepto": concepto,
        "referencia_anual": referencia,
    }


def _resumen_perfil(perfil):
    """Minimal, verified profile summary for the prompt."""
    perfil = perfil or {}
    identidad = perfil.get("identity") or {}
    lineas = []
    if identidad.get("nombre"):
        lineas.append("Nombre: " + str(identidad["nombre"]))
    if perfil.get("headline"):
        lineas.append("Perfil: " + str(perfil["headline"]))
    criterios = perfil.get("search_criteria") or {}
    if criterios.get("modalidad"):
        lineas.append("Modalidad preferida: " + str(criterios["modalidad"]))
    if criterios.get("tipo_contrato"):
        lineas.append("Tipo de contrato aceptado: " + str(criterios["tipo_contrato"]))
    return "\n".join(lineas) if lineas else "(sin datos de perfil)"


# ---------------------------------------------------------------------------
# Sanitizing the model response
# ---------------------------------------------------------------------------

def _texto_limpio(valor, limite=600):
    """Return a trimmed string; empty when the value is not textual."""
    if valor is None:
        return ""
    if not isinstance(valor, str):
        valor = str(valor)
    return valor.strip()[:limite]


def _saneado_salario(datos, texto_oferta, supuestos):
    """Normalize the ``salario_ofrecido`` block returned by the model."""
    datos = datos if isinstance(datos, dict) else {}
    minimo = _a_numero(datos.get("min"))
    maximo = _a_numero(datos.get("max"))
    if minimo is None and maximo is None:
        # Some models return "40-45k" in min or texto: try to parse a range.
        for clave in ("min", "texto"):
            rango_min, rango_max = _parsear_rango_objetivo(datos.get(clave))
            if rango_min is not None:
                minimo, maximo = rango_min, rango_max
                break

    if minimo is not None and maximo is not None and maximo < minimo:
        minimo, maximo = maximo, minimo

    moneda = _texto_limpio(datos.get("moneda"), 8).upper() or None
    periodo = _texto_limpio(datos.get("periodo"), 12).lower() or None
    if periodo not in ("anual", "mensual", "diario", "hora"):
        periodo = None
    bruto_neto = _texto_limpio(datos.get("bruto_neto"), 8).lower() or None
    if bruto_neto not in ("bruto", "neto"):
        bruto_neto = None
    literal = _texto_limpio(datos.get("texto"), 200) or None

    verificado = True
    for cifra in (minimo, maximo):
        if cifra is not None and not _cifra_en_texto(cifra, texto_oferta):
            verificado = False
    if (minimo is not None or maximo is not None) and not verificado:
        supuestos.append(
            "La cifra salarial extraída no aparece literalmente en el texto; "
            "conviene confirmarla con la empresa antes de valorarla."
        )
    if minimo is None and maximo is None:
        supuestos.append(
            "La oferta no indica salario (o no es interpretable): no se estima "
            "ninguna cifra y el gap no se calcula."
        )
    if (minimo is not None or maximo is not None) and periodo is None:
        supuestos.append(
            "No consta el periodo del salario; se asume anual para comparar."
        )
    if bruto_neto is None and (minimo is not None or maximo is not None):
        supuestos.append("No consta si el salario es bruto o neto.")

    return {
        "min": minimo,
        "max": maximo,
        "moneda": moneda,
        "periodo": periodo,
        "bruto_neto": bruto_neto,
        "texto": literal,
        "verificado_en_texto": verificado and (minimo is not None or maximo is not None),
    }


def _saneado_clausulas(datos, supuestos):
    """Normalize the clause list into dicts with clausula/detalle/riesgo."""
    clausulas = []
    if not isinstance(datos, list):
        if datos:
            supuestos.append("El modelo no devolvió cláusulas en formato lista.")
        return clausulas
    for bruta in datos:
        if not isinstance(bruta, dict):
            continue
        nombre = _texto_limpio(bruta.get("clausula"), 120)
        if not nombre:
            continue
        riesgo = _texto_limpio(bruta.get("riesgo"), 20).lower()
        if riesgo not in _RIESGOS:
            riesgo = "no_valorable"
        clausulas.append(
            {
                "clausula": nombre,
                "detalle": _texto_limpio(bruta.get("detalle"), 500),
                "riesgo": riesgo,
            }
        )
    return clausulas


def _saneado_preguntas(datos, maximo=8):
    """Normalize the lawyer question list (unique strings)."""
    if not isinstance(datos, list):
        return []
    preguntas, vistas = [], set()
    for bruta in datos:
        pregunta = _texto_limpio(bruta, 300)
        if len(pregunta) < 10:
            continue
        clave = pregunta.lower()
        if clave in vistas:
            continue
        vistas.add(clave)
        preguntas.append(pregunta)
        if len(preguntas) >= maximo:
            break
    return preguntas


def _parsear_json_tolerante(contenido):
    """Parse the first JSON object of the reply, ignoring trailing text.

    ``llm.complete_json`` slices from the first ``{`` to the last ``}``; a
    second object or braces inside comments make that fail with "Extra data".
    ``raw_decode`` stops at the end of the first object, which is what is
    needed here.
    """
    texto = (contenido or "").strip()
    bloque = re.search(r"```(?:json)?\s*(.*?)\s*```", texto, flags=re.DOTALL)
    if bloque:
        texto = bloque.group(1).strip()
    inicio = texto.find("{")
    if inicio == -1:
        raise llm.LLMError(
            "La respuesta del modelo no contiene ningún objeto JSON: "
            + texto[:200]
        )
    try:
        datos, _ = json.JSONDecoder().raw_decode(texto[inicio:])
    except ValueError as err:
        raise llm.LLMError(
            f"No se pudo parsear el JSON de la oferta: {err}. "
            f"Contenido: {texto[:300]}"
        ) from err
    if not isinstance(datos, dict):
        raise llm.LLMError("El JSON de la oferta no es un objeto.")
    return datos


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analizar_oferta(texto_oferta, perfil, max_tokens=4000):
    """Analyze a job offer and return a structured dict.

    Args:
        texto_oferta: plain text of the offer (email, message or contract).
        perfil: profile dict (``data/perfil.json``); ``compensation`` supplies
                the target salary.
        max_tokens: response budget for the model.

    Returns:
        dict with ``salario_ofrecido``, ``salario_objetivo``, ``gap_pct``,
        ``gap_eur``, ``clausulas``, ``preguntas_abogado``, ``recomendacion``
        and ``supuestos``.

    Raises:
        ValueError if the text is empty or too short to analyze.
        llm.LLMError if the model call fails or returns no JSON object.
    """
    if not isinstance(texto_oferta, str) or len(texto_oferta.strip()) < 40:
        raise ValueError(
            "texto_oferta debe ser una cadena de al menos 40 caracteres."
        )

    texto = texto_oferta.strip()
    if len(texto) > _MAX_TEXTO:
        texto = texto[:_MAX_TEXTO]

    objetivo = _datos_objetivo(perfil)
    supuestos = []
    if objetivo["referencia_anual"] is None:
        supuestos.append(
            "El perfil no tiene salario objetivo interpretable: no se calcula gap."
        )

    prompt = f"""Analiza la OFERTA DE TRABAJO y devuelve SÓLO un objeto JSON con
exactamente esta forma:
{_ESQUEMA_ESPERADO}

DATOS DEL CANDIDATO (contexto, no los repitas en el JSON):
{_resumen_perfil(perfil)}
Salario objetivo declarado: {objetivo["min"]} - {objetivo["max"]} {objetivo["moneda"]} ({objetivo["concepto"]})

REGLAS ESTRICTAS:
1. No inventes NADA. Si el salario, la moneda, el periodo o el carácter
   bruto/neto no aparecen en el texto, usa null. No estimes cifras "de mercado".
2. En "salario_ofrecido.texto" copia la frase literal donde aparece la
   retribución. "min"/"max" son números anuales sólo si el texto los da así;
   si el texto dice "2.500 €/mes", devuelve 2500 con periodo "mensual".
3. "clausulas": sólo cláusulas que estén realmente en la oferta (jornada,
   exclusividad, no competencia, permanencia, variable, teletrabajo, periodo
   de prueba, horas extra, formación, etc.). No añadas cláusulas hipotéticas.
   "riesgo" refleja el riesgo para el candidato, no una opinión legal.
4. "preguntas_abogado": máximo 6 preguntas concretas y útiles para revisar el
   contrato. No son asesoramiento legal.
5. "recomendacion": 2-4 frases prudentes; menciona el gap salarial sólo si hay
   cifra en la oferta. Si faltan datos, dilo.
6. "supuestos": lista de datos ausentes o ambiguos (por ejemplo "no consta
   jornada"). Lista vacía si no hay ninguno.
7. No calcules porcentajes ni diferencias salariales: eso se hace aparte.

OFERTA:
\"\"\"
{texto}
\"\"\"
"""

    # Direct chat+json_mode call so the reply can be parsed tolerantly
    # (complete_json fails with "Extra data" when the model appends text).
    contenido = llm.chat(
        [
            {
                "role": "system",
                "content": (
                    "Eres un analista que responde únicamente con JSON válido "
                    "y no inventa datos que no estén en el texto."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=max_tokens,
        json_mode=True,
    )
    respuesta = _parsear_json_tolerante(contenido)

    supuestos_modelo = _saneado_preguntas(respuesta.get("supuestos"), maximo=10)
    for supuesto in supuestos_modelo:
        if supuesto not in supuestos:
            supuestos.append(supuesto)

    salario = _saneado_salario(
        respuesta.get("salario_ofrecido"), texto, supuestos
    )

    # Deterministic gap calculation: the model never does it.
    referencia = objetivo["referencia_anual"]
    cifra_ofertada = salario["min"] if salario["min"] is not None else salario["max"]
    cifra_anual = _a_anual(cifra_ofertada, salario["periodo"])
    gap_pct = None
    gap_eur = None
    if referencia and cifra_anual is not None:
        gap_eur = round(cifra_anual - referencia, 2)
        gap_pct = round(gap_eur / referencia * 100.0, 1)
        if salario["min"] is not None and salario["max"] is not None:
            supuestos.append(
                "El gap se calcula con el extremo inferior del rango ofrecido "
                "frente al extremo inferior del objetivo."
            )
        if salario["periodo"] not in (None, "anual"):
            supuestos.append(
                "Importe convertido a anual asumiendo 12 meses / 220 días / "
                "1760 horas según el periodo indicado."
            )
        if salario["moneda"] and salario["moneda"] != objetivo["moneda"]:
            supuestos.append(
                "La moneda de la oferta difiere de la del perfil: el gap es "
                "nominal, sin tipo de cambio."
            )
    elif referencia and cifra_anual is None:
        supuestos.append("Sin importe en la oferta: gap no calculable.")

    recomendacion = _texto_limpio(respuesta.get("recomendacion"), 1200)
    if not recomendacion:
        recomendacion = (
            "No se ha podido generar una recomendación a partir del texto. "
            "Revisar la oferta manualmente antes de responder."
        )

    return {
        "salario_ofrecido": salario,
        "salario_objetivo": objetivo,
        "gap_pct": gap_pct,
        "gap_eur": gap_eur,
        "clausulas": _saneado_clausulas(respuesta.get("clausulas"), supuestos),
        "preguntas_abogado": _saneado_preguntas(respuesta.get("preguntas_abogado")),
        "recomendacion": recomendacion,
        "supuestos": supuestos,
    }


def formatear_analisis(analisis):
    """Return a readable text summary of the ``analizar_oferta`` dict.

    Useful for Telegram or the console. Sends nothing by itself.
    """
    if not isinstance(analisis, dict):
        raise ValueError("formatear_analisis requiere el dict de analizar_oferta.")
    salario = analisis.get("salario_ofrecido") or {}
    objetivo = analisis.get("salario_objetivo") or {}

    def _importe(valor):
        if valor is None:
            return "no consta"
        return f"{valor:,.0f}".replace(",", ".")

    lineas = ["ANÁLISIS DE OFERTA", ""]
    lineas.append(
        "Salario ofrecido: "
        + _importe(salario.get("min"))
        + (" - " + _importe(salario.get("max")) if salario.get("max") is not None and salario.get("max") != salario.get("min") else "")
        + " "
        + (salario.get("moneda") or "")
        + (" / " + str(salario.get("periodo")) if salario.get("periodo") else "")
    )
    lineas.append(
        "Objetivo: "
        + _importe(objetivo.get("min"))
        + " - "
        + _importe(objetivo.get("max"))
        + " "
        + (objetivo.get("moneda") or "")
    )
    if analisis.get("gap_pct") is not None:
        lineas.append(
            f"Gap: {analisis['gap_pct']:+.1f}% ({_importe(analisis.get('gap_eur'))} anuales)"
        )
    else:
        lineas.append("Gap: no calculable")

    lineas.append("")
    lineas.append("Cláusulas:")
    for clausula in analisis.get("clausulas") or []:
        lineas.append(
            f"- [{clausula.get('riesgo')}] {clausula.get('clausula')}: "
            f"{clausula.get('detalle')}"
        )
    if not analisis.get("clausulas"):
        lineas.append("- (ninguna detectada)")

    lineas.append("")
    lineas.append("Preguntas para abogado:")
    for pregunta in analisis.get("preguntas_abogado") or []:
        lineas.append("- " + pregunta)
    if not analisis.get("preguntas_abogado"):
        lineas.append("- (ninguna)")

    lineas.append("")
    lineas.append("Recomendación: " + (analisis.get("recomendacion") or ""))
    if analisis.get("supuestos"):
        lineas.append("")
        lineas.append("Supuestos:")
        for supuesto in analisis["supuestos"]:
            lineas.append("- " + supuesto)
    return "\n".join(lineas)


if __name__ == "__main__":
    # Offline self-check: profile and range parsing only.
    ruta = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "perfil.json",
    )
    with open(ruta, "r", encoding="utf-8") as fh:
        perfil_local = json.load(fh)
    print("Objetivo salarial:", _datos_objetivo(perfil_local))
    print("Rango '40k-45k':", _parsear_rango_objetivo("40k-45k"))
