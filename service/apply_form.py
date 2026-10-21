"""apply_form.py — Application form analysis and answer drafting.

This module bridges the gap between "I already have a cover letter and CV" and
"I submit the application": it reads the real form of the offer (Greenhouse,
Lever, Ashby or any other ATS with an HTML form), drafts the answers to the
open questions from the verified profile and, optionally, fills the browser
fields.

GOLDEN RULE — NOTHING IS EVER SUBMITTED
---------------------------------------
This module **never** clicks Submit and never sends Enter keystrokes inside
the fields (``fill`` does not trigger keys). The submit button always stays
with the person. ``rellenar_formulario`` only writes values and returns a
report of what is still pending.

Public functions
----------------
``analizar_formulario(url)``
    Opens the page with patchright and returns the list of form fields. Each
    field is a dict with ``tipo``, ``nombre``, ``etiqueta``, ``pregunta`` and
    ``requerido`` (plus ``opciones``/``valor``/``indice`` as support).
    ``pregunta`` carries text only for open fields (textarea and inputs whose
    label is a question); elsewhere it is ``None``.

``redactar_respuestas(formulario, perfil, op)``
    Drafts one answer per open question, grounded in the profile and the
    offer. Returns ``{clave_campo: respuesta}`` where the key is the field
    ``nombre`` (stable, used for filling) or, if unnamed, the question text.

``rellenar_formulario(url, respuestas)``
    Best effort: navigates, fills what it can and returns a report with what
    was filled, what is pending and the answers ready to paste. On an
    unexpected form (unreachable iframe, odd fields) it does not fail: it
    switches to ``solo_respuestas`` mode.

Design
------
* ``patchright`` is imported inside the browser functions, so
  ``redactar_respuestas`` works without a browser installed and drafting tests
  do not pay the cost of starting Chromium.
* No invented data: anything the profile marks as ``null`` or ``PEND`` is not
  filled; the LLM must emit ``[CONFIRMAR: ...]`` and the module purges any
  metric (percentages, years, amounts) that does not appear in the sources.
* All logging avoids dumping secrets: only URLs, field names and counts.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import unicodedata
from collections.abc import Iterable
from typing import Any

import llm

log = logging.getLogger("jobautomat.apply_form")

# Default profile path, relative to service/ (the .env may override it with
# PERFIL_PATH).
_PERFIL_POR_DEFECTO = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "perfil.json"
)

# Default navigation/load timeout (ms).
_TIMEOUT_MS = 45000

# Questions that are NEVER drafted by the LLM: they ask for behavioural or
# administrative data only the person knows (how they heard about the ad, work
# authorisation, diversity). Inventing them would falsify the application.
_RE_PREGUNTA_NO_REDACTABLE = re.compile(
    r"(how did you hear|c[oó]mo (te|nos) (enteraste|conociste)|referral|referid|"
    r"autorizad|authorized to work|sponsorship|patrocinio|visado|visa|"
    r"gender|g[eé]nero|ethnic|etnia|veteran|discapacidad|disability|"
    r"sexual orientation|orientaci[oó]n sexual|pronoun|"
    r"pronounce|pronunciaci[oó]n|\bgpa\b|grade point|"
    r"portfolio link|enlace (a|del) portfolio|"
    r"i agree|consent|consentimiento|acepto)",
    re.IGNORECASE,
)

# Selectors that are NEVER touched: submit buttons. Listed to document the
# prohibition and for the final report (they are counted, not clicked).
_SELECTOR_ENVIO = (
    'button[type="submit"], input[type="submit"], '
    'button:has-text("Submit"), button:has-text("Enviar"), '
    'button:has-text("Apply"), button:has-text("Solicitar")'
)


# ---------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------


def _sin_acentos(texto: str) -> str:
    """Return ``texto`` lowercased and without diacritics, for comparison."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto).strip().lower()


# ---------------------------------------------------------------------------
# Perfil
# ---------------------------------------------------------------------------


def cargar_perfil(ruta: str | None = None) -> dict:
    """Load ``data/perfil.json`` (or the given path) and return it as a dict.

    If ``ruta`` is None, ``PERFIL_PATH`` from the .env is tried and, if
    unavailable, the repository default path.

    Args:
        ruta: optional explicit path to the profile file.

    Returns:
        dict: the parsed profile.

    Raises:
        FileNotFoundError: if the profile file does not exist; the module
            never invents profiles.
    """
    if ruta is None:
        ruta = os.environ.get("PERFIL_PATH")
    if ruta is None:
        try:
            from config import load_config

            ruta = load_config().get("PERFIL_PATH")
        except Exception:  # an incomplete config (.env without keys) must not block the read
            ruta = None
    if not ruta:
        ruta = _PERFIL_POR_DEFECTO
    if not os.path.isabs(ruta):
        ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), ruta)

    with open(ruta, encoding="utf-8") as fh:
        return json.load(fh)


def _resumen_perfil(perfil: dict) -> str:
    """Compact, faithful profile summary for the prompts (nothing invented)."""
    perfil = perfil or {}
    ident = perfil.get("identity") or {}
    lineas = [
        "NOMBRE: {}".format(ident.get("nombre", "")),
        "HEADLINE: {}".format(perfil.get("headline", "")),
    ]
    if ident.get("ubicacion_cv"):
        lineas.append("UBICACIÓN (según CV): {}".format(ident["ubicacion_cv"]))
    if ident.get("linkedin"):
        lineas.append("LINKEDIN: {}".format(ident["linkedin"]))
    if ident.get("portfolio"):
        lineas.append("PORTFOLIO: {}".format(ident["portfolio"]))

    pos = perfil.get("positioning_emphasis") or {}
    if pos.get("declaracion_principal"):
        lineas.append("POSICIONAMIENTO: {}".format(pos["declaracion_principal"]))
    if pos.get("matiz_liderazgo"):
        lineas.append("MATIZ DE LIDERAZGO: {}".format(pos["matiz_liderazgo"]))

    for exp in perfil.get("experience") or []:
        lineas.append(
            "- {} en {} ({}, {})".format(
                exp.get("titulo_cv", ""),
                exp.get("empresa", ""),
                exp.get("periodo", ""),
                exp.get("ubicacion", ""),
            )
        )
        for bullet in (exp.get("bullets") or [])[:4]:
            lineas.append(f"  · {bullet}")
        if exp.get("nota"):
            lineas.append("  (límite declarado: {})".format(exp["nota"]))

    for proj in perfil.get("projects") or []:
        lineas.append(
            "- Proyecto {} ({}): {}".format(
                proj.get("nombre", ""), proj.get("rol", ""), proj.get("descripcion", "")
            )
        )
        for bullet in (proj.get("bullets") or [])[:2]:
            lineas.append(f"  · {bullet}")

    for grupo, items in (perfil.get("skills") or {}).items():
        lineas.append("SKILLS {}: {}".format(grupo, ", ".join(items)))

    idiomas = perfil.get("languages") or {}
    if idiomas:
        lineas.append(
            "IDIOMAS: "
            + "; ".join(
                "{}={}".format(k, (v or {}).get("nivel") or "sin nivel acreditado")
                for k, v in idiomas.items()
            )
        )

    comp = perfil.get("compensation") or {}
    if comp.get("minimo") is not None:
        lineas.append(
            "COMPENSACIÓN DECLARADA: mínimo {} {} ({}); objetivo {}".format(
                comp.get("minimo"),
                comp.get("moneda", ""),
                comp.get("concepto", ""),
                comp.get("objetivo", ""),
            )
        )

    pendientes = {k: v for k, v in (perfil.get("pendientes") or {}).items() if v in (None, "PEND")}
    if pendientes:
        lineas.append(
            "DATOS NO CONFIRMADOS (prohibido rellenarlos con invenciones): {}".format(
                ", ".join(sorted(pendientes))
            )
        )
    return "\n".join(lineas)


def _bloque_oferta(op: dict) -> str:
    """Format the offer for the prompt (tolerates English keys)."""
    op = op or {}
    descripcion = (op.get("descripcion") or op.get("description") or "").strip()
    if len(descripcion) > 6000:
        descripcion = descripcion[:6000] + "\n[...recortado...]"
    return (
        "PUESTO: {}\nEMPRESA: {}\nUBICACIÓN: {}\nMODALIDAD: {}\n"
        "SALARIO: {}\nURL: {}\nDESCRIPCIÓN:\n{}".format(
            op.get("titulo") or op.get("title") or "(sin título)",
            op.get("empresa") or op.get("company") or "(sin empresa)",
            op.get("ubicacion") or op.get("location") or "(no indicada)",
            op.get("remoto") or op.get("remote") or "(no indicada)",
            op.get("salario") or op.get("salary") or "(no publicado)",
            op.get("url") or "(sin URL)",
            descripcion or "(sin descripción)",
        )
    )


# ---------------------------------------------------------------------------
# Extracción del formulario en el DOM
# ---------------------------------------------------------------------------

# JS that walks the frame DOM and returns the visible fields. It runs in the
# page context (or the ATS iframe), so it uses nothing from Python. Returns a
# list of plain objects.
_JS_EXTRAER = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();

  const visible = (el) => {
    if (!el) return false;
    if (el.type === 'hidden') return false;
    const st = window.getComputedStyle(el);
    if (!st) return false;
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const textoDe = (n) => norm(n && (n.innerText || n.textContent || ''));

  // ¿La etiqueta es realmente una pregunta abierta? Un "Full name" no lo es
  // aunque sea largo; "Describe un proyecto del que estés orgulloso" sí.
  const esPregunta = (t) => {
    if (!t) return false;
    if (/\?\s*$/.test(t)) return true;
    if (t.length < 25) return false;
    return /(why|how|describe|tell us|explain|share|por qu[eé]|c[oó]mo|describe|cu[eé]nta|explica)/i.test(t);
  };

  // Une las pistas de etiqueta sin repetir: "First Name" + "First Name*" se
  // queda en "First Name*" (la parte contenida se descarta).
  const unirPartes = (partes) => {
    const genericos = /^(type here\.?|type here\.\.\.|search|select\.\.\.|choose\.\.\.|your answer.*|e\.g\..*)$/i;
    let salida = [];
    partes.map(norm).filter(Boolean).forEach((t) => {
      if (genericos.test(t)) return;
      if (salida.some((s) => s === t)) return;
      if (salida.some((s) => s.indexOf(t) !== -1)) return;
      salida = salida.filter((s) => t.indexOf(s) === -1);
      salida.push(t);
    });
    return norm(salida.join(' | ')).replace(/\*/g, '').replace(/\s{2,}/g, ' ');
  };

  // Recoge el texto que hace de etiqueta para un campo concreto.
  const etiquetaDe = (el) => {
    const partes = [];
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) partes.push(aria);

    const labelled = el.getAttribute && el.getAttribute('aria-labelledby');
    if (labelled) {
      labelled.split(/\s+/).forEach((id) => {
        const n = document.getElementById(id);
        if (n) partes.push(textoDe(n));
      });
    }

    if (el.id) {
      try {
        const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (l) partes.push(textoDe(l));
      } catch (e) { /* id con caracteres raros: se ignora */ }
    }

    const lab = el.closest && el.closest('label');
    if (lab) partes.push(textoDe(lab));

    const fs = el.closest && el.closest('fieldset');
    if (fs) {
      const lg = fs.querySelector('legend');
      if (lg) partes.push(textoDe(lg));
    }

    // Contenedores típicos de los ATS: .application-question, .field, .form-group
    if (!partes.length) {
      let p = el.parentElement;
      let saltos = 0;
      while (p && saltos < 5) {
        const cand = p.querySelector(
          'label, legend, .application-label, .label, [class*="question-label"], [data-testid*="label"]'
        );
        if (cand && cand !== el) { partes.push(textoDe(cand)); break; }
        p = p.parentElement;
        saltos++;
      }
    }

    // El placeholder sólo sirve si no hay ninguna etiqueta real: si no, mete
    // ruido ("Name | Type here...").
    if (!partes.length) {
      const ph = el.getAttribute && el.getAttribute('placeholder');
      if (ph) partes.push(ph);
    }

    return unirPartes(partes);
  };

  const esRequerido = (el) => {
    if (el.required) return true;
    if ((el.getAttribute('aria-required') || '') === 'true') return true;
    const cont = el.closest('label, .field, .form-group, [class*="question"]');
    if (cont && /\*/.test(textoDe(cont).slice(-6))) return true;
    if (cont && (cont.className || '').toString().indexOf('required') !== -1) return true;
    return false;
  };

  // Alcance: el formulario con más campos visibles; si no hay <form>, el main.
  const formularios = Array.from(document.querySelectorAll('form'));
  let alcance = null;
  let maximo = 0;
  formularios.forEach((f) => {
    const n = f.querySelectorAll('input, select, textarea').length;
    if (n > maximo) { maximo = n; alcance = f; }
  });
  if (!alcance || maximo === 0) {
    alcance = document.querySelector('main, [role="main"]') || document.body;
  }
  if (!alcance) return [];

  const ignorar = /(csrf|authenticity|^utf8$|_token|token$|honeypot|_gotcha|^hp_|g-recaptcha)/i;
  const tiposIgnorados = ['hidden', 'submit', 'button', 'reset', 'image', 'search'];
  const salida = [];
  const vistos = {};
  const contador = {};

  const elementos = alcance.querySelectorAll('input, select, textarea');
  elementos.forEach((el) => {
    if (!visible(el)) return;
    const tag = el.tagName.toLowerCase();
    const tipoCrudo = (el.getAttribute('type') || (tag === 'input' ? 'text' : tag)).toLowerCase();
    if (tiposIgnorados.indexOf(tipoCrudo) !== -1) return;

    // Greenhouse (job-boards nuevo) no usa `name`: el identificador estable es
    // el `id` (first_name, question_68542814...). Se usa como nombre.
    const nombre = el.getAttribute('name') || el.getAttribute('id') || '';
    if (nombre && ignorar.test(nombre)) return;

    const ariaBruto = el.getAttribute('aria-label') || '';
    // Inputs auxiliares de librerías (hidden de React Select, etc.) sin
    // ninguna pista: no son campos del formulario.
    if (!nombre && !ariaBruto && !el.getAttribute('placeholder')) return;

    const role = (el.getAttribute('role') || '').toLowerCase();

    contador[tag] = (contador[tag] || 0) + 1;
    const indice = contador[tag] - 1;

    let tipo = tag === 'textarea' ? 'textarea' : (tag === 'select' ? 'select' : tipoCrudo);
    // Selectores "custom" (React Select / combobox): se listan para el humano,
    // pero no son preguntas abiertas ni se rellenan a ciegas.
    if (role === 'combobox' && tag !== 'select') tipo = 'combobox';

    // Grupo de radios/checkboxes: se emite una sola entrada por nombre, porque
    // cada input es una opción y rellenar se hace por texto de opción.
    const esGrupo = (tipo === 'radio' || tipo === 'checkbox');
    const idGrupo = esGrupo ? (nombre || ('anon-' + indice)) : null;
    if (esGrupo && vistos['g:' + idGrupo]) {
      // Sólo añadimos la opción al grupo ya emitido.
      vistos['g:' + idGrupo].opciones.push(etiquetaDe(el).replace(/^.*\| /, '') || el.value);
      return;
    }

    const etiqueta = etiquetaDe(el);
    if (!etiqueta && !nombre) return; // campo anónimo sin pistas: no aporta

    // En un <select> el innerText del <label> contenedor incluye todas las
    // opciones ("What is your location? Select... Afghanistan..."). Se quitan
    // para quedarse con la pregunta.
    let etiquetaLimpia = etiqueta;
    if (tag === 'select') {
      let limpia = etiqueta;
      Array.from(el.options || []).forEach((o) => {
        const t = norm(o.text);
        if (t) limpia = limpia.split(t).join(' ');
      });
      etiquetaLimpia = norm(limpia);
    }

    const item = {
      tipo: tipo,
      nombre: nombre || null,
      etiqueta: etiquetaLimpia || nombre,
      pregunta: null,
      requerido: esRequerido(el),
      opciones: [],
      valor: el.value || '',
      indice: indice,
      tag: tag
    };

    if (tipo === 'textarea') {
      item.pregunta = etiquetaLimpia || null;
    } else if (tipo === 'text' || tipo === 'email' || tipo === 'tel' || tipo === 'url') {
      // Un input de texto sólo cuenta como pregunta abierta si su etiqueta
      // pregunta algo de verdad: evita mandar "Full name" al redactor.
      if (esPregunta(etiquetaLimpia)) {
        item.pregunta = etiquetaLimpia;
      }
    } else if (tag === 'select') {
      item.opciones = Array.from(el.options || []).map((o) => o.text.trim());
    } else if (esGrupo) {
      item.opciones = [etiquetaDe(el).replace(/^.*\| /, '') || el.value];
    }

    if (esGrupo) vistos['g:' + idGrupo] = item;
    salida.push(item);
  });

  return salida;
}
"""


def _abrir_navegador(pw, headless: bool):
    """Open Chromium through patchright, retrying with the system channel."""
    args = ["--disable-blink-features=AutomationControlled"]
    try:
        navegador = pw.chromium.launch(headless=headless, args=args)
    except Exception as err:  # patchright browsers not installed: fall back to the system Chrome
        log.warning(
            "No se pudo lanzar Chromium de patchright (%s); probando Chrome del sistema.", err
        )
        navegador = pw.chromium.launch(headless=headless, channel="chrome", args=args)
    contexto = navegador.new_context(
        locale="es-ES",
        viewport={"width": 1366, "height": 900},
    )
    return navegador, contexto


def _esperar_carga(pagina) -> None:
    """Wait until the page is usable, without failing if it takes too long."""
    try:
        pagina.wait_for_load_state("domcontentloaded", timeout=_TIMEOUT_MS)
    except Exception:
        pass
    try:
        pagina.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        # Many ATS keep connections open (analytics); this is not an error.
        pass
    # Short wait for React form hydration (Ashby/Greenhouse).
    try:
        pagina.wait_for_timeout(1500)
    except Exception:
        pass


def _candidatos_url(url: str) -> list[str]:
    """URLs to try when looking for the form.

    Lever publishes the offer and the form at ``/apply``; Ashby at
    ``/application``; Greenhouse (new job-boards) serves the form on the same
    page or embedded in an iframe. The original URL is tried first and, when
    the ATS uses a separate route, that one too.

    Args:
        url: original opportunity URL.

    Returns:
        List[str]: candidate URLs in order.
    """
    urls = [url]
    limpio = url.split("?")[0].rstrip("/")
    if "jobs.lever.co" in url and not limpio.endswith("/apply"):
        urls.append(limpio + "/apply")
    if "ashbyhq.com" in url and not limpio.endswith("/application"):
        urls.append(limpio + "/application")
    return urls


def _extraer_de_pagina(pagina) -> list[dict]:
    """Walk the main page and its iframes and return the fields.

    Each field carries the extra key ``_frame`` (index within
    ``pagina.frames``). Cross-origin iframes raise when evaluating JS: they are
    skipped silently (the report reflects it as an inaccessible form).

    Args:
        pagina: patchright page.

    Returns:
        List[dict]: extracted fields.
    """
    campos: list[dict] = []
    for idx, frame in enumerate(pagina.frames):
        try:
            crudos = frame.evaluate(_JS_EXTRAER)
        except Exception:
            continue
        if not crudos:
            continue
        # The main frame may include an iframe with the real form (embedded
        # Greenhouse): the frame with the most fields wins.
        if len(crudos) > len(campos):
            campos = []
            vistos = set()
            for c in crudos:
                c = dict(c)
                c["_frame"] = idx
                # Dedupe: some ATS render the form twice (mobile/desktop view)
                # with the same id.
                clave = _sin_acentos(c.get("nombre") or "") or _sin_acentos(c.get("etiqueta") or "")
                if clave and clave in vistos:
                    continue
                if clave:
                    vistos.add(clave)
                campos.append(c)
    return campos


def _campos_publicos(campos: Iterable[dict]) -> list[dict]:
    """Strip the internal keys (``_frame``) from the fields."""
    salida = []
    for c in campos:
        c = dict(c)
        c.pop("_frame", None)
        salida.append(c)
    return salida


def _analizar(url: str, headless: bool = True) -> tuple[list[dict], dict]:
    """Implementation of ``analizar_formulario`` that also returns metadata.

    ``meta`` includes ``url_final``, ``intentos`` and ``motivo`` (human-readable
    text when no fields were found: login, Cloudflare, opaque iframe...).

    Args:
        url: URL to analyse.
        headless: whether to run the browser without a window.

    Returns:
        Tuple[List[dict], dict]: public fields and the metadata dict.
    """
    from patchright.sync_api import sync_playwright

    meta = {"url_original": url, "url_final": None, "intentos": [], "motivo": None}
    campos: list[dict] = []

    with sync_playwright() as pw:
        navegador, contexto = _abrir_navegador(pw, headless)
        try:
            pagina = contexto.new_page()
            for candidata in _candidatos_url(url):
                try:
                    pagina.goto(candidata, wait_until="domcontentloaded", timeout=_TIMEOUT_MS)
                except Exception as err:
                    meta["intentos"].append({"url": candidata, "error": str(err)[:200]})
                    continue
                _esperar_carga(pagina)
                meta["intentos"].append({"url": candidata, "estado": "cargada"})
                meta["url_final"] = pagina.url
                campos = _extraer_de_pagina(pagina)
                if campos:
                    break

            if not campos:
                meta["motivo"] = _diagnosticar_sin_campos(pagina)
            meta["url_final"] = pagina.url
        finally:
            try:
                contexto.close()
                navegador.close()
            except Exception:
                pass

    return _campos_publicos(campos), meta


def _diagnosticar_sin_campos(pagina) -> str:
    """Explain why there are no fields, without breaking anything."""
    try:
        titulo = (pagina.title() or "").lower()
    except Exception:
        titulo = ""
    try:
        texto = (pagina.inner_text("body") or "").lower()[:4000]
    except Exception:
        texto = ""
    senales_acceso = ("sign in", "iniciar sesión", "log in", "create an account", "sign up")
    if any(p in texto for p in senales_acceso):
        return "La página pide iniciar sesión o registrarse; no se analiza sin sesión."
    senales_bot = ("verify you are human", "checking your browser", "captcha", "cloudflare")
    if any(p in texto for p in senales_bot):
        return (
            "La página muestra un control anti-bot (Cloudflare/captcha); "
            "no se pudo leer el formulario."
        )
    if "no longer" in texto or "not found" in titulo or "404" in titulo:
        return "La oferta ya no está publicada (404 o retirada)."
    if any(f.url and ("greenhouse.io" in f.url or "ashbyhq.com" in f.url) for f in pagina.frames):
        return "El formulario vive en un iframe de otro origen que no se pudo leer."
    return "No se encontraron campos de formulario visibles en la página."


def analizar_formulario(url: str, headless: bool = True) -> list[dict]:
    """Analyse the application form at ``url`` and return its fields.

    Navigates with patchright (no session, no credentials), waits for the load
    and extracts the visible ``input``/``select``/``textarea`` elements with
    their label, name, type and whether they are required.

    Args:
        url: offer or form URL (Greenhouse/Lever/Ashby/other).
        headless: if False, opens a visible window (useful for debugging).

    Returns:
        List[dict]: dicts with ``tipo``, ``nombre``, ``etiqueta``, ``pregunta``
        (text only for open fields) and ``requerido``, plus ``opciones``,
        ``valor`` and ``indice``. Empty list if the form could not be read (the
        reason is logged).
    """
    campos, meta = _analizar(url, headless=headless)
    if not campos:
        log.warning("Sin campos en %s: %s", url, meta.get("motivo"))
    else:
        log.info(
            "Formulario de %s: %d campos (%d preguntas abiertas).",
            url,
            len(campos),
            sum(1 for c in campos if c.get("pregunta")),
        )
    return campos


# ---------------------------------------------------------------------------
# Redacción de respuestas
# ---------------------------------------------------------------------------

_SISTEMA_RESPUESTAS = """Eres un redactor de candidaturas. Escribes respuestas a las \
preguntas abiertas del formulario de una oferta, en primera persona y con voz \
propia del candidato.

REGLAS INNEGOCIABLES
1. Usa SÓLO hechos que aparezcan en el PERFIL o en la OFERTA. No inventes \
empresas, clientes, títulos, años de experiencia, métricas, porcentajes, \
salarios, idiomas ni certificaciones.
2. Si la pregunta pide un dato que no consta en el perfil, escribe el marcador \
[CONFIRMAR: <dato concreto>] en ese punto en lugar de rellenarlo. Es correcto \
dejar un marcador; es grave inventar.
3. No prometas disponibilidad, autorización de trabajo, preaviso ni salario: \
sólo lo que el perfil declare de forma explícita.
4. Responde en el mismo idioma en que está escrita la pregunta.
5. Entre 60 y 140 palabras por respuesta. Concreto y sobrio: sin clichés \
("apasionado", "sinergias"), sin listas de adjetivos, sin repetir el enunciado.
6. Apóyate en 1 o 2 evidencias concretas del perfil (proyecto o empresa) que \
sean relevantes para ESA oferta. Si el perfil declara límites (por ejemplo, \
"no acredita autoría exclusiva"), respétalos.
7. No hables de la empresa con datos que no estén en la oferta. No cites \
rangos salariales, bonos ni condiciones como si los conocieras: si hablas de \
dinero, usa sólo las cifras del apartado COMPENSACIÓN del perfil.
8. Máximo 140 palabras por respuesta (el sistema recorta lo que exceda).

FORMATO: devuelve únicamente JSON válido con esta forma exacta:
{"respuestas": [{"campo": "<nombre exacto del campo>", "respuesta": "<texto>"}]}
No uses otras claves de primer nivel ni añadas notas. Una entrada por cada \
pregunta recibida, sin omitir ninguna."""


def _purgar_metricas(respuesta: str, fuentes: str) -> tuple[str, list[str]]:
    """Remove sentences with metrics that do not appear in the sources.

    The LLM tends to "decorate" with percentages and years of experience. The
    numbers in each sentence are compared with those present in the profile and
    the offer; a sentence with an unsupported figure is dropped.

    Args:
        respuesta: drafted answer.
        fuentes: profile and offer text used as ground truth.

    Returns:
        Tuple[str, List[str]]: the cleaned text and the dropped sentences (for
        logging).
    """
    numeros_fuente = set(re.findall(r"\d+(?:[.,]\d+)?", fuentes or ""))
    patrones = (
        re.compile(r"\d+(?:[.,]\d+)?\s?%"),
        re.compile(r"\d+(?:[.,]\d+)?\s*(?:años|years)\b", re.IGNORECASE),
        re.compile(r"\b\d+(?:[.,]\d+)?\s*[xX]\b"),
        re.compile(r"[€$]\s?\d+(?:[.,]\d+)?"),
        re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:k|K)\b"),
    )
    frases = re.split(r"(?<=[.!?])\s+", respuesta or "")
    conservadas, quitadas = [], []
    for frase in frases:
        cifras = set()
        for patron in patrones:
            for hallado in patron.findall(frase):
                cifras.update(re.findall(r"\d+(?:[.,]\d+)?", hallado))
        if cifras and not cifras.issubset(numeros_fuente):
            quitadas.append(frase.strip())
            continue
        conservadas.append(frase)
    limpio = " ".join(conservadas).strip()
    if quitadas and not limpio:
        limpio = "[CONFIRMAR: respuesta pendiente de datos verificados]"
    return limpio, quitadas


def _llamar_json(prompt: str, max_tokens: int = 4000) -> dict:
    """``llm.complete_json`` with one retry if the token budget runs out."""
    try:
        return llm.complete_json(prompt, max_tokens=max_tokens)
    except llm.LLMError as err:
        if "max_tokens" not in str(err) and "vacío" not in str(err):
            raise
        log.warning("Respuesta vacía del LLM; reintentando con más presupuesto.")
        return llm.complete_json(prompt, max_tokens=max_tokens * 2)


def _acortar_respuesta(texto: str, max_chars: int = 1200) -> str:
    """Trim an overlong answer at the end of a sentence.

    The prompt asks for 140 words, but the model often exceeds it and a
    textarea with 400 words looks machine-generated. The text is cut at the
    last period before the limit (or at the last word) so the paste still reads
    naturally.

    Args:
        texto: answer to trim.
        max_chars: maximum length.

    Returns:
        str: the trimmed answer.
    """
    texto = (texto or "").strip()
    if len(texto) <= max_chars:
        return texto
    corte = texto[:max_chars]
    pos = max(corte.rfind(". "), corte.rfind("! "), corte.rfind("? "))
    if pos > max_chars * 0.55:
        return corte[: pos + 1].strip()
    return corte.rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _normalizar_pares(datos: Any) -> list[tuple[str, str]]:
    """Extract ``[(campo, respuesta)]`` from the shapes the model returns.

    DeepSeek does not always honour the requested schema, so these are accepted:
      * ``{"respuestas": [{"campo": ..., "respuesta": ...}, ...]}``
      * ``{"respuestas": {"campo": "respuesta", ...}}``
      * ``{"campo": ..., "respuesta": ...}`` (a single question)
      * ``{"<field name>": "<answer>", ...}`` (flat dict)

    Args:
        datos: decoded JSON returned by the LLM.

    Returns:
        List[Tuple[str, str]]: field/answer pairs.
    """
    if not isinstance(datos, dict):
        return []

    crudas = datos.get("respuestas")
    if isinstance(crudas, list):
        pares = []
        for item in crudas:
            if isinstance(item, dict):
                campo = item.get("campo") or item.get("pregunta") or item.get("nombre") or ""
                texto = item.get("respuesta") or item.get("texto") or ""
                if isinstance(texto, str):
                    pares.append((str(campo).strip(), texto))
        return pares
    if isinstance(crudas, dict):
        return [(str(k).strip(), v) for k, v in crudas.items() if isinstance(v, str)]

    if datos.get("campo") and isinstance(datos.get("respuesta"), str):
        return [(str(datos["campo"]).strip(), datos["respuesta"])]

    # Flat dict: any key with a text value that is not metadata.
    metadatos = {"nota", "notes", "explicacion", "explicación", "resumen", "comentario"}
    return [
        (str(k).strip(), v)
        for k, v in datos.items()
        if isinstance(v, str) and str(k).strip().lower() not in metadatos and v.strip()
    ]


def redactar_respuestas(
    formulario: list[dict], perfil: dict, op: dict, max_preguntas: int = 8
) -> dict[str, str]:
    """Draft one answer per open question in the form.

    Args:
        formulario: list returned by ``analizar_formulario`` (or equivalent).
        perfil: dict from ``data/perfil.json``.
        op: opportunity dict (``titulo``/``empresa``/``descripcion``...).
        max_preguntas: cap on questions sent to the LLM (bounded cost).

    Returns:
        Dict[str, str]: ``{clave: respuesta}`` with one entry per question. The
        key is the field ``nombre`` (stable, used for filling) or the question
        text when the field has no name. Empty dict if there are no open
        questions or if the LLM returns nothing usable.
    """
    todas_preguntas = [c for c in (formulario or []) if (c.get("pregunta") or "").strip()]
    preguntas = []
    for c in todas_preguntas:
        if _RE_PREGUNTA_NO_REDACTABLE.search(c.get("pregunta") or ""):
            log.info("Se omite (dato personal/administrativo): %s", (c.get("pregunta") or "")[:70])
            continue
        preguntas.append(c)
    if not preguntas:
        log.info("El formulario no tiene preguntas abiertas redactables.")
        return {}
    if len(preguntas) > max_preguntas:
        log.warning("Hay %d preguntas; se redactan las %d primeras.", len(preguntas), max_preguntas)
        preguntas = preguntas[:max_preguntas]

    # Dedupe by field name (a radio group already comes grouped).
    vistos, lista = set(), []
    for c in preguntas:
        clave = (c.get("nombre") or c.get("pregunta") or "").strip()
        if clave in vistos:
            continue
        vistos.add(clave)
        lista.append(c)

    lines = []
    for i, c in enumerate(lista, 1):
        lines.append(
            f"{i}. campo={c.get('nombre') or '(sin nombre)'} | tipo={c.get('tipo')} | "
            f"obligatorio={'sí' if c.get('requerido') else 'no'}\n"
            f"   PREGUNTA: {c.get('pregunta')}"
        )

    prompt = (
        "PERFIL VERIFICADO DEL CANDIDATO\n"
        "==============================\n{}\n\n"
        "OFERTA\n======\n{}\n\n"
        "PREGUNTAS DEL FORMULARIO\n========================\n{}\n\n"
        "Redacta una respuesta para cada pregunta. Usa como valor de 'campo' "
        "exactamente el nombre de campo indicado arriba (o el texto de la "
        "pregunta si no hay nombre). Devuelve sólo el JSON.".format(
            _resumen_perfil(perfil), _bloque_oferta(op), "\n".join(lines)
        )
    )

    try:
        datos = _llamar_json(prompt)
    except llm.LLMError as err:
        log.error("Fallo del LLM al redactar respuestas: %s", err)
        return {}

    pares = _normalizar_pares(datos)
    if not pares:
        log.warning(
            "El LLM no devolvió pares campo/respuesta utilizables: %s",
            list(datos)[:5] if isinstance(datos, dict) else type(datos),
        )
        return {}

    # Index to match the model output against the real list even when spaces
    # or capitalisation differ.
    por_clave = {}
    for c in lista:
        por_clave[_sin_acentos(c.get("nombre") or "")] = c
        por_clave[_sin_acentos(c.get("pregunta") or "")] = c

    fuentes = json.dumps(perfil, ensure_ascii=False) + "\n" + _bloque_oferta(op)
    respuestas: dict[str, str] = {}
    for campo, texto in pares:
        texto = (texto or "").strip()
        if not texto or not campo:
            continue
        original = por_clave.get(_sin_acentos(campo))
        if original is None:
            # The model invented the key: fall back to a partial match.
            for k, v in por_clave.items():
                if k and (k in _sin_acentos(campo) or _sin_acentos(campo) in k):
                    original = v
                    break
        if original is None:
            log.warning("Se descarta una respuesta con campo desconocido: %r", campo[:80])
            continue
        limpio, quitadas = _purgar_metricas(texto, fuentes)
        if quitadas:
            log.info(
                "Podadas %d frases con métricas no respaldadas en '%s'.",
                len(quitadas),
                (original.get("pregunta") or "")[:60],
            )
        recortado = _acortar_respuesta(limpio)
        if recortado != limpio:
            log.info("Respuesta recortada de %d a %d caracteres.", len(limpio), len(recortado))
        clave = (original.get("nombre") or original.get("pregunta") or campo).strip()
        respuestas[clave] = recortado

    log.info("Redactadas %d respuestas de %d preguntas.", len(respuestas), len(lista))
    return respuestas


# ---------------------------------------------------------------------------
# Rellenado (sin enviar nunca)
# ---------------------------------------------------------------------------


def _frame_de(pagina, campo: dict):
    """Return the frame where the field was detected (or the main one)."""
    frames = pagina.frames
    idx = campo.get("_frame", 0) or 0
    return frames[idx] if 0 <= idx < len(frames) else frames[0]


def _localizar_campo(pagina, campo: dict):
    """Return a locator for the field, or None if it cannot be identified.

    Prefers ``[name="..."]`` inside the frame where it was detected and falls
    back to the positional index of the tag (``textarea``/``select``/``input``)
    as a safety net.

    Args:
        pagina: patchright page.
        campo: field dict.

    Returns:
        A locator, or None if the field cannot be located.
    """
    tag = campo.get("tag") or {"textarea": "textarea", "select": "select"}.get(
        campo.get("tipo"), "input"
    )
    frame = _frame_de(pagina, campo)

    nombre = (campo.get("nombre") or "").strip()
    if nombre and '"' not in nombre and "\\" not in nombre:
        try:
            loc = frame.locator(f'{tag}[name="{nombre}"]')
            if loc.count() >= 1:
                return loc.first
        except Exception:
            pass

    indice = campo.get("indice")
    if isinstance(indice, int):
        try:
            loc = frame.locator(tag)
            if loc.count() > indice:
                return loc.nth(indice)
        except Exception:
            pass
    return None


def _opciones_radio(pagina, campo: dict):
    """Return the radio inputs of the field group (not just the first one)."""
    frame = _frame_de(pagina, campo)
    nombre = (campo.get("nombre") or "").strip()
    if nombre and '"' not in nombre:
        try:
            return frame.locator(f'input[type="radio"][name="{nombre}"]')
        except Exception:
            pass
    return frame.locator('input[type="radio"]')


def _texto_opcion_radio(input_loc) -> str:
    """Visible text of the option: value, aria-label and associated label."""
    partes = []
    for attr in ("value", "aria-label"):
        try:
            partes.append(input_loc.get_attribute(attr) or "")
        except Exception:
            pass
    try:
        asociada = input_loc.evaluate(
            "el => { const l = el.closest('label'); if (l) return l.innerText;"
            " if (el.id) { const o = document.querySelector('label[for=\"' + el.id + '\"]');"
            " if (o) return o.innerText; } return ''; }"
        )
        partes.append(asociada or "")
    except Exception:
        pass
    return " ".join(p for p in partes if p)


def _valor_a_escribir(campo: dict, texto: str) -> str:
    """Adapt the answer to the field type (selects and checks)."""
    texto = (texto or "").strip()
    tipo = (campo.get("tipo") or "").lower()
    if tipo in ("select", "radio") and campo.get("opciones"):
        # Pick the option whose text best matches the answer.
        objetivo = _sin_acentos(texto)
        for opcion in campo["opciones"]:
            o = _sin_acentos(opcion)
            if o and (o in objetivo or objetivo in o):
                return opcion
    return texto


def _rellenar_en_pagina(pagina, campos: list[dict], respuestas: dict[str, str]) -> dict:
    """Write each answer into its field. Never clicks Submit."""
    informe = {
        "rellenados": [],
        "no_rellenados": [],
        "respuestas_sin_campo": [],
        "omitidos_por_tipo": [],
    }

    # Normalised answer index (by name and by question text).
    disponibles = {}
    for clave, valor in (respuestas or {}).items():
        disponibles[_sin_acentos(clave)] = (clave, valor)

    usadas = set()
    for campo in campos:
        pregunta = campo.get("pregunta")
        nombre = (campo.get("nombre") or "").strip()
        # 1) Match by field name (stable; used by drafted answers and the
        #    profile contact data).
        objetivo = disponibles.get(_sin_acentos(nombre)) if nombre else None
        # 2) Match by exact question text.
        if objetivo is None and pregunta:
            objetivo = disponibles.get(_sin_acentos(pregunta))
        # 3) Partial match (the model rephrased the question).
        if objetivo is None and pregunta:
            buscado = _sin_acentos(pregunta)
            for k, v in disponibles.items():
                if k and (k in buscado or buscado in k):
                    objetivo = v
                    break
        if objetivo is None:
            continue
        clave, texto = objetivo
        usadas.add(clave)
        tipo = (campo.get("tipo") or "").lower()

        if tipo == "file":
            informe["omitidos_por_tipo"].append(
                {"campo": campo.get("nombre"), "motivo": "file: súbelo tú"}
            )
            continue

        if tipo == "combobox":
            informe["omitidos_por_tipo"].append(
                {"campo": campo.get("nombre"), "motivo": "desplegable custom: selecciónalo tú"}
            )
            continue

        loc = _localizar_campo(pagina, campo)
        if loc is None:
            informe["no_rellenados"].append(
                {"campo": campo.get("nombre"), "motivo": "no localizado en el DOM"}
            )
            continue

        try:
            if tipo == "select":
                valor = _valor_a_escribir(campo, texto)
                try:
                    loc.select_option(label=valor)
                except Exception:
                    loc.select_option(value=valor)
                leido = loc.input_value()
            elif tipo == "radio":
                valor = _valor_a_escribir(campo, texto)
                objetivo_norm = _sin_acentos(valor)
                opciones = _opciones_radio(pagina, campo)
                elegido = None
                parcial = None
                for i in range(opciones.count()):
                    c = opciones.nth(i)
                    etiqueta = _sin_acentos(_texto_opcion_radio(c))
                    valor_opcion = _sin_acentos(c.get_attribute("value") or "")
                    if objetivo_norm and (
                        objetivo_norm == etiqueta or objetivo_norm == valor_opcion
                    ):
                        elegido = c
                        break
                    if (
                        parcial is None
                        and objetivo_norm
                        and (objetivo_norm in etiqueta or (etiqueta and etiqueta in objetivo_norm))
                    ):
                        parcial = c
                elegido = elegido or parcial
                if elegido is None:
                    raise ValueError("opción de radio no encontrada para la respuesta")
                elegido.check()
                leido = valor
            elif tipo == "checkbox":
                informe["omitidos_por_tipo"].append(
                    {"campo": campo.get("nombre"), "motivo": "checkbox: revísalo tú"}
                )
                continue
            else:
                loc.fill(texto)
                # Masked inputs (international phone, rich text) reformat on
                # typing: re-read after a short wait.
                try:
                    pagina.wait_for_timeout(120)
                except Exception:
                    pass
                leido = loc.input_value()

            if tipo != "radio" and not (leido or "").strip():
                # fill() left no readable value: the field cannot be claimed
                # as filled, so it is reported as pending.
                informe["no_rellenados"].append(
                    {
                        "campo": campo.get("nombre"),
                        "motivo": "sin valor legible tras escribir (revísalo tú)",
                    }
                )
                continue

            informe["rellenados"].append({"campo": campo.get("nombre"), "pregunta": pregunta})
        except Exception as err:
            informe["no_rellenados"].append(
                {"campo": campo.get("nombre"), "motivo": str(err).split("\n")[0][:160]}
            )

    for clave in respuestas or {}:
        if clave not in usadas:
            informe["respuestas_sin_campo"].append(clave)
    return informe


def _esperar_cierre_humano(pagina) -> None:
    """Keep the browser open until the person confirms (CLI usage).

    The module never submits, so the point of opening a window is for the human
    to review and press Submit: closing the browser immediately would defeat
    the purpose.
    """
    print(
        "\nFormulario rellenado. NO se ha pulsado Submit.\n"
        "Revisa cada respuesta y envía tú desde el navegador."
    )
    try:
        if sys.stdin and sys.stdin.isatty():
            input("Pulsa Enter aquí para cerrar el navegador (no envía nada)... ")
        else:
            pagina.wait_for_timeout(120000)
    except (EOFError, KeyboardInterrupt):
        pass


def rellenar_formulario(
    url: str,
    respuestas: dict[str, str],
    perfil: dict | None = None,
    headless: bool = False,
    pausa_ms: int = 0,
    esperar_cierre: bool = False,
) -> dict:
    """Fill the form with ``respuestas`` and NEVER submit it.

    Args:
        url: offer/form URL.
        respuestas: ``{campo_o_pregunta: texto}`` (output of
            ``redactar_respuestas``).
        perfil: if given, also fills the obvious contact fields (name, email,
            phone, LinkedIn, portfolio) with the profile values. Never invents:
            only existing keys are used.
        headless: defaults to False (the human must see and press Submit).
        pausa_ms: fixed wait before closing the browser.
        esperar_cierre: if True (CLI), blocks until the person presses Enter
            before closing the window. Defaults to False so automated processes
            do not hang.

    Returns:
        dict: with ``modo`` (``"rellenado"`` or ``"solo_respuestas"``),
        ``rellenados``, ``no_rellenados``, ``respuestas_sin_campo``,
        ``respuestas_para_pegar`` (always, in case they must be pasted by hand)
        and ``nota``. On any failure, ``modo="solo_respuestas"`` with the
        reason, so the drafting work is not lost.
    """
    informe: dict = {
        "url": url,
        "modo": "solo_respuestas",
        "rellenados": [],
        "no_rellenados": [],
        "respuestas_sin_campo": [],
        "omitidos_por_tipo": [],
        "respuestas_para_pegar": dict(respuestas or {}),
        "submit_pulsado": False,
        "url_final": None,
        "url_cambio": False,
        "nota": "",
    }

    if not respuestas and not perfil:
        informe["nota"] = "No hay respuestas que rellenar."
        return informe

    try:
        from patchright.sync_api import sync_playwright
    except Exception as err:
        informe["nota"] = f"patchright no disponible: pega las respuestas a mano ({err})."
        return informe

    try:
        with sync_playwright() as pw:
            navegador, contexto = _abrir_navegador(pw, headless)
            try:
                pagina = contexto.new_page()
                campos: list[dict] = []
                for candidata in _candidatos_url(url):
                    try:
                        pagina.goto(candidata, wait_until="domcontentloaded", timeout=_TIMEOUT_MS)
                    except Exception as err:
                        informe["no_rellenados"].append(
                            {"campo": None, "motivo": f"navegación: {str(err)[:160]}"}
                        )
                        continue
                    _esperar_carga(pagina)
                    campos = _extraer_de_pagina(pagina)
                    if campos:
                        break

                if not campos:
                    informe["nota"] = (
                        f"No se pudo leer el formulario ({_diagnosticar_sin_campos(pagina)}). "
                        "Respuestas listas para pegar."
                    )
                    return informe

                # Reference URL: the one after loading (the trailing slash may
                # have been normalised). If it changes while filling, something
                # navigated and the user must be warned.
                try:
                    url_antes = pagina.url
                except Exception:
                    url_antes = url

                # Contact fill from the profile (only existing data).
                todas = dict(respuestas or {})
                if perfil:
                    todas.update(_respuestas_contacto(campos, perfil))

                resultado = _rellenar_en_pagina(pagina, campos, todas)
                informe.update(resultado)

                # Safety check: the URL must not have changed and the submit
                # button is still in place (it was not clicked).
                try:
                    informe["url_final"] = pagina.url
                    informe["url_cambio"] = pagina.url != url_antes
                except Exception:
                    pass
                try:
                    formularios_envio = pagina.locator(_SELECTOR_ENVIO).count()
                except Exception:
                    formularios_envio = 0

                informe["modo"] = "rellenado" if informe["rellenados"] else "solo_respuestas"
                informe["nota"] = (
                    "Campos rellenados en el navegador. NO se ha pulsado Submit: "
                    f"revisa cada respuesta y envía tú. "
                    f"Botones de envío visibles: {formularios_envio}."
                )
                if informe.get("url_cambio"):
                    informe["nota"] += (
                        " AVISO: la URL cambió durante el rellenado; revisa la página."
                    )
                if esperar_cierre:
                    _esperar_cierre_humano(pagina)
                elif pausa_ms:
                    try:
                        pagina.wait_for_timeout(pausa_ms)
                    except Exception:
                        pass
            finally:
                try:
                    contexto.close()
                    navegador.close()
                except Exception:
                    pass
    except Exception as err:
        # Any browser failure must not lose the answers.
        informe["modo"] = "solo_respuestas"
        informe["nota"] = f"Fallo del navegador ({str(err)[:200]}). Respuestas listas para pegar."
        log.warning("Fallo rellenando %s: %s", url, err)

    return informe


def _respuestas_contacto(campos: list[dict], perfil: dict) -> dict[str, str]:
    """Profile contact data for the typical form fields.

    Returns keys only for values that exist in the profile. Matching is done by
    field name (``email``, ``phone``, ``linkedin``...) and by label. Fields that
    already have a value (prefilled by the ATS) and open questions are left
    untouched.

    Args:
        campos: field dicts as returned by the extractor.
        perfil: profile dict.

    Returns:
        Dict[str, str]: ``{clave_campo: valor}`` for matched fields.
    """
    ident = (perfil or {}).get("identity") or {}
    nombre_completo = (ident.get("nombre") or "").strip()
    partes = nombre_completo.split()
    nombre_pila = partes[0] if partes else None
    apellidos = " ".join(partes[1:]) if len(partes) > 1 else None

    # (needles, value, words that invalidate the match)
    reglas = [
        (("apellido", "last name", "surname", "family name"), apellidos, ("first",)),
        (("first name", "given name", "nombre de pila", "primer nombre"), nombre_pila, ("last",)),
        (
            ("full name", "nombre completo", "your name", "name"),
            nombre_completo,
            (
                "company",
                "empresa",
                "last",
                "first",
                "user",
                "file",
                "project",
                "referr",
                "their",
                "manager",
                "emergency",
            ),
        ),
        (("email", "correo", "e-mail"), ident.get("email"), ()),
        (("phone", "telefono", "mobile", "movil"), ident.get("telefono"), ()),
        (("linkedin",), ident.get("linkedin"), ()),
        (
            ("portfolio", "website", "sitio web", "github", "web personal"),
            ident.get("portfolio") or ident.get("web"),
            ("company",),
        ),
        (
            ("location", "ubicacion", "city", "ciudad", "residencia"),
            ident.get("ubicacion_cv"),
            ("relocation", "country"),
        ),
    ]

    salida: dict[str, str] = {}
    for campo in campos or []:
        if campo.get("pregunta"):
            continue  # open questions go through their drafted answer
        if (campo.get("valor") or "").strip():
            continue  # already has a value: do not overwrite
        tipo = (campo.get("tipo") or "").lower()
        if tipo in ("file", "checkbox", "radio", "select", "textarea", "combobox"):
            continue
        nombre = _sin_acentos(campo.get("nombre") or "")
        etiqueta = _sin_acentos(campo.get("etiqueta") or "")
        for agujas, valor, vetos in reglas:
            if not valor:
                continue
            if any(v in nombre or v in etiqueta for v in vetos):
                continue
            # Matching on the field name/id is reliable. The label is used only
            # when it is short and not a question: otherwise "Has someone
            # referred you? ... their name" would get the full name.
            coincide = any(a in nombre for a in agujas) or (
                len(etiqueta) <= 45 and "?" not in etiqueta and any(a in etiqueta for a in agujas)
            )
            if coincide:
                clave = campo.get("nombre") or campo.get("etiqueta")
                if clave:
                    salida[clave] = valor
                break
    return salida


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _imprimir_json(datos: Any) -> None:
    """Print readable UTF-8 JSON."""
    print(json.dumps(datos, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    """CLI: ``analizar``, ``redactar`` or ``rellenar`` on a URL.

    Never submits the form: ``rellenar`` opens the browser and leaves the submit
    button to the person.

    Args:
        argv: optional argument list (defaults to ``sys.argv``).

    Returns:
        int: process exit code.
    """
    parser = argparse.ArgumentParser(
        description="Analiza y rellena formularios de aplicación (sin enviar)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_an = sub.add_parser("analizar", help="Extrae los campos del formulario")
    p_an.add_argument("url")
    p_an.add_argument("--visible", action="store_true", help="Muestra el navegador")

    p_red = sub.add_parser("redactar", help="Analiza y redacta las respuestas")
    p_red.add_argument("url")

    p_rel = sub.add_parser("rellenar", help="Rellena el formulario SIN enviarlo")
    p_rel.add_argument("url")
    p_rel.add_argument("--headless", action="store_true", help="Sin ventana (no recomendado)")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.cmd == "analizar":
        _imprimir_json(analizar_formulario(args.url, headless=not args.visible))
        return 0

    perfil = cargar_perfil()
    campos = analizar_formulario(args.url)

    if args.cmd == "redactar":
        op = {"titulo": "", "empresa": "", "descripcion": "", "url": args.url}
        respuestas = redactar_respuestas(campos, perfil, op)
        _imprimir_json({"campos": campos, "respuestas": respuestas})
        return 0

    # fill
    op = {"titulo": "", "empresa": "", "descripcion": "", "url": args.url}
    respuestas = redactar_respuestas(campos, perfil, op)
    informe = rellenar_formulario(
        args.url,
        respuestas,
        perfil=perfil,
        headless=args.headless,
        esperar_cierre=not args.headless,
    )
    _imprimir_json(informe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
