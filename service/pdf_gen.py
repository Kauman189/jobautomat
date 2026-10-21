"""pdf_gen.py — Render documents to A4 PDF with Harvard styling.

Main path: HTML + CSS rendered by ``patchright``'s Chromium (the same browser
used by ``search/linkedin.py``). Fallback: reportlab with Helvetica when
Chromium fails to start.

Always respected (ATS-friendly):

- Single column, no tables, no images, no headers or footers (no page numbers,
  no letterheads).
- Real, extractable text: Chromium renders with system fonts (Georgia / Times
  New Roman), nothing is converted to an image.
- Markdown is converted by ``limpiar_markdown()`` into ``(tipo, texto)`` blocks
  and from there into semantic HTML (``h1``, ``h2``, ``p``, ``ul``).
- All content is escaped with ``html.escape`` before reaching the HTML.

Public functions:
    texto_a_pdf(texto, ruta, titulo=None, autor=None) -> str
    generar_pdf_cv(texto_md, ruta) -> str
    generar_pdf_cover(texto, ruta) -> str
"""

import base64
import html
import json
import logging
import os
import re
import shutil
import subprocess
import unicodedata
import zlib

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from xml.sax.saxutils import escape

_registro = logging.getLogger(__name__)

# Cached PDF author name (``identity.nombre`` from the user profile).
_AUTOR_CANDIDATO = None

# Page margins in millimetres (used by Chromium and reportlab).
_MARGENES_MM = {"top": 15, "bottom": 15, "left": 18, "right": 18}

# ---------------------------------------------------------------------------
# reportlab fallback render
# ---------------------------------------------------------------------------

# Margins: 20 mm sides give ~90-character lines with Helvetica 10.5.
_MARGEN_LATERAL = 20 * mm
_MARGEN_SUPERIOR = 18 * mm
_MARGEN_INFERIOR = 18 * mm

# Mappings for typographic signs outside WinAnsi or unreliable in text
# extraction (arrows, angle quotes, bullets...).
_EQUIVALENCIAS = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2212": "-",
    "\u2022": "-", "\u00b7": "-", "\u25aa": "-", "\u25cf": "-",
    "\u2026": "...", "\u2192": "->", "\u2190": "<-", "\u2194": "<->",
    "\u2264": "<=", "\u2265": ">=", "\u00a0": " ", "\u202f": " ",
    "\u2009": " ", "\u200b": "", "\ufeff": "", "\u00ad": "",
}

# Styles (created once; reportlab reuses them for every document).
_ESTILO_TITULO = ParagraphStyle(
    "titulo",
    fontName="Helvetica-Bold",
    fontSize=15,
    leading=19,
    alignment=TA_CENTER,
    spaceBefore=0,
    spaceAfter=6,
)
_ESTILO_SECCION = ParagraphStyle(
    "seccion",
    fontName="Helvetica-Bold",
    fontSize=11.5,
    leading=15,
    alignment=TA_LEFT,
    spaceBefore=11,
    spaceAfter=3,
)
_ESTILO_TEXTO = ParagraphStyle(
    "texto",
    fontName="Helvetica",
    fontSize=10.5,
    leading=14,
    alignment=TA_LEFT,
    spaceAfter=4,
)
_ESTILO_BULLET = ParagraphStyle(
    "bullet",
    parent=_ESTILO_TEXTO,
    leftIndent=12,
    firstLineIndent=-8,
    spaceAfter=2.5,
)
_ESTILO_FIRMA = ParagraphStyle("firma", parent=_ESTILO_TEXTO, spaceBefore=8)


# ---------------------------------------------------------------------------
# Text cleanup
# ---------------------------------------------------------------------------

def _apto_helvetica(texto):
    """Degrade to WinAnsi/ASCII the characters Helvetica cannot represent.

    Runs after typographic signs are normalized. Anything still unrepresentable
    is reduced to its unaccented form; if that fails too it is dropped, so no
    stray ``?`` reaches the PDF.
    """
    for origen, destino in _EQUIVALENCIAS.items():
        texto = texto.replace(origen, destino)

    salida = []
    for caracter in texto:
        try:
            caracter.encode("cp1252")
            salida.append(caracter)
            continue
        except UnicodeEncodeError:
            pass
        # Second attempt: strip diacritics (ǻ -> a, ł -> l...).
        base = unicodedata.normalize("NFKD", caracter)
        base = "".join(c for c in base if not unicodedata.combining(c))
        if base and all(_es_cp1252(c) for c in base):
            salida.append(base)
        # Still unrepresentable: drop it.
    return "".join(salida)


def _es_cp1252(caracter):
    """True if the character can be encoded as WinAnsi."""
    try:
        caracter.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def _quitar_markdown_inline(linea):
    """Strip inline markdown markup, leaving readable text."""
    # Links [texto](url) -> "texto (url)": the URL is kept, useful in a CV.
    linea = re.sub(
        r"\[([^\]]+)\]\(([^)\s]+)\)",
        lambda m: "%s (%s)" % (m.group(1), m.group(2))
        if m.group(2) not in m.group(1)
        else m.group(1),
        linea,
    )
    # Images ![alt](url) -> alt (no image is inserted in the PDF).
    linea = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", linea)
    # Autolinks <https://...> -> https://...
    linea = re.sub(r"<((?:https?|mailto):[^>\s]+)>", r"\1", linea)
    # Bold, italics and code: the marker goes, the content stays.
    linea = linea.replace("**", "").replace("__", "").replace("`", "")
    linea = re.sub(r"(?<![\w*])\*(?!\s)([^*]+?)(?<!\s)\*(?![\w*])", r"\1", linea)
    linea = re.sub(r"(?<![\w_])_(?!\s)([^_]+?)(?<!\s)_(?![\w_])", r"\1", linea)
    linea = linea.replace("~~", "")
    # Markdown escapes (\*, \_ ...).
    linea = re.sub(r"\\([\\`*_{}\[\]()#+\-.!])", r"\1", linea)
    # Multiple spaces and spaces before punctuation.
    linea = re.sub(r"[ \t]{2,}", " ", linea)
    linea = re.sub(r"\s+([,.;:])", r"\1", linea)
    return linea.strip()


def limpiar_markdown(texto):
    """Convert markdown into a list of ``(tipo, texto)`` blocks.

    Types: ``"titulo"`` (H1), ``"seccion"`` (H2-H6), ``"bullet"`` and
    ``"texto"``. Empty blocks are not returned. Pure and deterministic, easy to
    test without reportlab.
    """
    if not texto:
        return []

    plano = str(texto).replace("\r\n", "\n").replace("\r", "\n")

    # Code fences: keep the content as plain text.
    plano = re.sub(r"^\s*```[^\n]*$", "", plano, flags=re.MULTILINE)

    bloques = []
    for cruda in plano.split("\n"):
        linea = cruda.rstrip()
        if not linea.strip():
            continue
        # Horizontal rules: they add nothing in plain text.
        if re.fullmatch(r"\s{0,3}([-*_])(?:\s*\1){2,}\s*", linea):
            continue
        # Block quotes: drop the leading ">".
        linea = re.sub(r"^\s{0,3}>\s?", "", linea)

        nivel = re.match(r"^\s{0,3}(#{1,6})\s+(.*)$", linea)
        if nivel:
            tipo = "titulo" if len(nivel.group(1)) == 1 else "seccion"
            contenido = _quitar_markdown_inline(nivel.group(2))
            contenido = contenido.rstrip("#").strip()
            if contenido:
                bloques.append((tipo, contenido))
            continue

        # Markdown bullets (*, -, +) -> single hyphen.
        bullet = re.match(r"^\s{0,8}[*+\-]\s+(.*)$", linea)
        if bullet and not re.match(r"^\s*[-*+]\s*$", linea):
            contenido = _quitar_markdown_inline(bullet.group(1))
            if contenido:
                bloques.append(("bullet", contenido))
            continue

        # Numbered lists: the number is kept as text.
        contenido = _quitar_markdown_inline(linea)
        if contenido:
            bloques.append(("texto", contenido))

    return bloques


# ---------------------------------------------------------------------------
# HTML + CSS (Harvard style)
# ---------------------------------------------------------------------------

_PATRON_CONTACTO = re.compile(r"[@]|(?:\+\d[\d\s]{6,})|\d{3}[\s.\-]?\d{2,}")

_CSS = """
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; }
    body {
        font-family: Georgia, "Times New Roman", "DejaVu Serif", serif;
        font-size: 10pt;
        line-height: 1.33;
        color: #111111;
        -webkit-font-smoothing: antialiased;
    }
    h1.nombre {
        font-size: 22pt;
        font-weight: normal;
        line-height: 1.1;
        letter-spacing: 0.09em;
        text-transform: uppercase;
        text-align: center;
        margin: 0 0 4pt 0;
    }
    p.subtitulo {
        text-align: center;
        font-size: 10.2pt;
        font-style: italic;
        margin: 0 0 3pt 0;
    }
    p.contacto {
        text-align: center;
        font-size: 9pt;
        line-height: 1.45;
        margin: 0 0 8pt 0;
        padding-bottom: 6pt;
        border-bottom: 0.9pt solid #111111;
    }
    h2.seccion {
        font-size: 9.9pt;
        font-weight: bold;
        text-transform: uppercase;
        letter-spacing: 0.09em;
        margin: 10.5pt 0 4pt 0;
        padding-bottom: 2.5pt;
        border-bottom: 0.7pt solid #111111;
        page-break-after: avoid;
    }
    p {
        margin: 0 0 3.2pt 0;
        orphans: 2;
        widows: 2;
    }
    p.puesto {
        font-weight: bold;
        margin: 7pt 0 2pt 0;
        page-break-after: avoid;
    }
    p.firma { margin-top: 14pt; }
    ul {
        margin: 0 0 4pt 0;
        padding-left: 15pt;
        list-style-type: disc;
    }
    li {
        margin: 0 0 1.1pt 0;
        padding-left: 1pt;
        page-break-inside: avoid;
    }
"""


def _parece_contacto(texto):
    """True if the line looks like a contact line (email, phone, networks)."""
    if not texto or len(texto) < 12:
        return False
    separadores = texto.count("|") + texto.count("\u00b7")
    return separadores >= 2 and bool(_PATRON_CONTACTO.search(texto))


def bloques_a_html(bloques, titulo=None, autor=None):
    """Convert ``(tipo, texto)`` blocks into a complete HTML5 document.

    Applies the Harvard hierarchy: large serif name, underlined contact line,
    uppercase ruled sections and clean body. Text blocks preceding a bullet
    list are treated as a job title (bold) to keep the CV readable.
    """
    cuerpo = []
    total = len(bloques)
    estado_cabecera = "inicio"  # state machine: inicio -> subtitulo -> contacto -> listo
    hay_nombre = False
    en_lista = False

    for indice, (tipo, contenido) in enumerate(bloques):
        seguro = html.escape(contenido, quote=False)
        siguiente = bloques[indice + 1][0] if indice + 1 < total else None

        if tipo == "titulo":
            if not hay_nombre:
                cuerpo.append('<h1 class="nombre">%s</h1>' % seguro)
                hay_nombre = True
                estado_cabecera = "subtitulo"
            else:
                # Second H1: demoted to a section to keep the hierarchy.
                cuerpo.append('<h2 class="seccion">%s</h2>' % seguro)
            continue

        if tipo == "seccion":
            cuerpo.append('<h2 class="seccion">%s</h2>' % seguro)
            continue

        if tipo == "bullet":
            # Group consecutive bullets into a single list.
            if en_lista:
                cuerpo.append("<li>%s</li>" % seguro)
            else:
                cuerpo.append("<ul><li>%s</li>" % seguro)
                en_lista = True
            if siguiente != "bullet":
                cuerpo.append("</ul>")
                en_lista = False
            continue

        if en_lista:
            cuerpo.append("</ul>")
            en_lista = False

        # tipo == "texto"
        if hay_nombre and estado_cabecera in ("subtitulo", "contacto"):
            if _parece_contacto(contenido):
                cuerpo.append('<p class="contacto">%s</p>' % seguro)
                estado_cabecera = "listo"
            elif estado_cabecera == "subtitulo":
                cuerpo.append('<p class="subtitulo">%s</p>' % seguro)
                estado_cabecera = "contacto"
            else:
                cuerpo.append("<p>%s</p>" % seguro)
                estado_cabecera = "listo"
            continue

        if siguiente == "bullet" and len(contenido) <= 220:
            cuerpo.append('<p class="puesto">%s</p>' % seguro)
            continue

        # Short closing line (letter signature): a little air above.
        es_firma = (
            len(contenido) <= 40
            and indice >= total - 2
            and not contenido.endswith((".", ":", ";", ","))
        )
        cuerpo.append('<p class="%s">%s</p>' % ("firma" if es_firma else "", seguro))

    titulo_html = html.escape(titulo or "Currículum", quote=False)
    autor_html = html.escape(autor or "", quote=False)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="es">\n<head>\n<meta charset="utf-8">\n'
        "<title>%s</title>\n"
        '<meta name="author" content="%s">\n'
        "<style>%s</style>\n</head>\n<body>\n%s\n</body>\n</html>\n"
        % (titulo_html, autor_html, _CSS, "\n".join(cuerpo))
    )


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def contar_paginas(ruta):
    """Page count of a PDF. Uses pypdf when available, else raw bytes."""
    try:
        from pypdf import PdfReader  # lazy import: it is optional

        return len(PdfReader(str(ruta)).pages), "pypdf"
    except Exception:
        pass
    with open(str(ruta), "rb") as manejador:
        crudo = manejador.read()
    # /Type /Page (not /Type /Pages, which is the tree node).
    paginas = len(re.findall(rb"/Type\s*/Page[^s]", crudo))
    return paginas, "bytes"


def _render_chromium(documento_html, ruta):
    """Render the HTML to PDF with patchright's Chromium."""
    from patchright.sync_api import sync_playwright

    playwright = None
    navegador = None
    try:
        playwright = sync_playwright().start()
        navegador = playwright.chromium.launch()
        pagina = navegador.new_page()
        pagina.set_content(documento_html, wait_until="load")
        pagina.pdf(
            path=ruta,
            format="A4",
            print_background=False,
            display_header_footer=False,
            prefer_css_page_size=False,
            margin={
                "top": "%dmm" % _MARGENES_MM["top"],
                "bottom": "%dmm" % _MARGENES_MM["bottom"],
                "left": "%dmm" % _MARGENES_MM["left"],
                "right": "%dmm" % _MARGENES_MM["right"],
            },
        )
    finally:
        if navegador is not None:
            try:
                navegador.close()
            except Exception:  # noqa: BLE001 - closing must never hide the real failure
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:  # noqa: BLE001
                pass


def _render_reportlab(bloques, ruta, titulo=None, autor=None):
    """Fallback render with reportlab (Helvetica, no CSS)."""
    # SimpleDocTemplate adds no header or footer unless onPage is given.
    documento = SimpleDocTemplate(
        ruta,
        pagesize=A4,
        leftMargin=_MARGEN_LATERAL,
        rightMargin=_MARGEN_LATERAL,
        topMargin=_MARGEN_SUPERIOR,
        bottomMargin=_MARGEN_INFERIOR,
        title=titulo or os.path.splitext(os.path.basename(ruta))[0],
        author=autor or "",
        subject="",
        creator="jobautomat",
    )

    historia = []
    for indice, (tipo, contenido) in enumerate(bloques):
        seguro = escape(_apto_helvetica(contenido))
        if tipo == "titulo":
            historia.append(Paragraph(seguro, _ESTILO_TITULO))
        elif tipo == "seccion":
            historia.append(Paragraph(seguro, _ESTILO_SECCION))
        elif tipo == "bullet":
            # Literal hyphen: ATS extractors read "- texto" as a readable bullet.
            historia.append(Paragraph("- " + seguro, _ESTILO_BULLET))
        else:
            # Short closing line (letter signature): a little air above.
            es_firma = (
                len(contenido) <= 40
                and indice >= len(bloques) - 2
                and not contenido.endswith((".", ":", ";", ","))
            )
            historia.append(
                Paragraph(seguro, _ESTILO_FIRMA if es_firma else _ESTILO_TEXTO)
            )

    # An empty block between sections keeps the text from looking cramped.
    if len(historia) > 1:
        historia.insert(0, Spacer(1, 0))

    documento.build(historia)
    return ruta


def texto_a_pdf(texto, ruta, titulo=None, autor=None):
    """Render ``texto`` (markdown or plain) to an A4 PDF with Harvard styling.

    Args:
        texto: document content. Markdown is stripped before writing; HTML is
            not interpreted and no images are inserted.
        ruta: output PDF path. The directory is created when missing.
        titulo: optional ``/Title`` metadata.
        autor: optional ``/Author`` metadata.

    Returns:
        The absolute path of the written PDF.

    Raises:
        ValueError: when the text has nothing renderable.
        OSError: when the directory or the file cannot be written.
    """
    if not ruta:
        raise ValueError("ruta de salida vacía")

    bloques = limpiar_markdown(texto)
    if not bloques:
        raise ValueError("texto vacío: no hay nada que renderizar")

    ruta = os.path.abspath(os.path.expanduser(str(ruta)))
    directorio = os.path.dirname(ruta)
    if directorio:
        os.makedirs(directorio, exist_ok=True)

    titulo_pdf = titulo or os.path.splitext(os.path.basename(ruta))[0]
    documento_html = bloques_a_html(bloques, titulo=titulo_pdf, autor=autor)

    try:
        _render_chromium(documento_html, ruta)
    except Exception as error:  # noqa: BLE001 - any failure falls back to reportlab
        _registro.warning(
            "Chromium no pudo renderizar el PDF (%s); uso reportlab de respaldo",
            error,
        )
        _render_reportlab(bloques, ruta, titulo=titulo, autor=autor)

    paginas, fuente = contar_paginas(ruta)
    if paginas > 2:
        _registro.warning(
            "El PDF %s tiene %d páginas (%s > 2): revisa la longitud del texto",
            ruta,
            paginas,
            fuente,
        )
    return ruta


def _autor_candidato():
    """Author metadata for the PDFs: ``identity.nombre`` from ``data/perfil.json``.

    The name is never hardcoded: it comes from the user's profile at runtime and
    falls back to empty (no author metadata) when the profile is unreadable.
    """
    global _AUTOR_CANDIDATO
    if _AUTOR_CANDIDATO is None:
        nombre = ""
        base = os.path.dirname(os.path.abspath(__file__))
        ruta = os.path.abspath(os.path.join(base, "..", "data", "perfil.json"))
        try:
            with open(ruta, encoding="utf-8") as archivo:
                perfil = json.load(archivo) or {}
            nombre = ((perfil.get("identity") or {}).get("nombre") or "")
        except (OSError, ValueError):
            nombre = ""
        _AUTOR_CANDIDATO = str(nombre).strip()
    return _AUTOR_CANDIDATO


def generar_pdf_cv(texto_md, ruta, autor=None):
    """Generate the tailored CV PDF from its markdown.

    Same as ``texto_a_pdf`` with CV metadata (author = profile name unless
    ``autor`` is given). Returns the absolute path of the written PDF.
    """
    return texto_a_pdf(
        texto_md, ruta, titulo="CV", autor=_autor_candidato() if autor is None else autor
    )


def generar_pdf_cover(texto, ruta, autor=None):
    """Generate the cover letter PDF (plain text).

    Author metadata defaults to the profile name. Returns the absolute path of
    the written PDF.
    """
    return texto_a_pdf(
        texto,
        ruta,
        titulo="Carta de presentación",
        autor=_autor_candidato() if autor is None else autor,
    )


# ---------------------------------------------------------------------------
# Deterministic CV PDF quality gate
# ---------------------------------------------------------------------------
#
# Idea from Rezi/Jobscan: the render (Chromium or reportlab) can silently lose
# content (font, odd markdown, a section that does not fit). An LLM would not
# notice, but re-extracting the PDF text and searching for hard data does.
# Everything here is cheap, deterministic and optional: it never raises.

_TAMANO_MINIMO_PDF = 500  # bytes; below this there is no real page

# Literal ``(...)`` strings of a PDF content stream.
_RE_PDF_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.S)


def _normalizar_comparable(valor):
    """Lowercase, no accents, collapsed spaces.

    Equivalent to ``main._normalizar_texto_clave()`` for what matters here
    (accents and case) without depending on ``main``: ``pdf_gen`` is imported
    from ``generate`` and must not drag the whole service along.
    """
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip().lower()


def _solo_alfanumerico(texto):
    """Keep only letters and digits: tolerates hyphens, dots and line breaks."""
    return re.sub(r"[^a-z0-9]", "", _normalizar_comparable(texto))


def _contiene(heno, aguja):
    """True if ``aguja`` appears in ``heno`` (normalized, or compacted)."""
    if not aguja:
        return True  # an empty profile field is not a PDF failure
    if _normalizar_comparable(aguja) in heno:
        return True
    compacta = _solo_alfanumerico(aguja)
    return bool(compacta) and compacta in _solo_alfanumerico(heno)


def _texto_con_pdftotext(ruta):
    """PDF text via ``pdftotext -layout`` (poppler), when installed."""
    if not shutil.which("pdftotext"):
        return None
    try:
        proceso = subprocess.run(
            ["pdftotext", "-layout", str(ruta), "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        _registro.warning("pdftotext falló sobre %s: %s", ruta, error)
        return None
    if proceso.returncode != 0:
        return None
    return proceso.stdout.decode("utf-8", "replace")


def _texto_con_pypdf(ruta):
    """PDF text via ``pypdf`` or ``PyPDF2``, whichever is installed."""
    lector = None
    modulo = None
    for modulo in ("pypdf", "PyPDF2"):
        try:
            lector = __import__(modulo).PdfReader(str(ruta))
            break
        except Exception:  # noqa: BLE001 — missing module or unreadable PDF
            continue
    if lector is None:
        return None
    try:
        return "\n".join((pagina.extract_text() or "") for pagina in lector.pages)
    except Exception as error:  # noqa: BLE001
        _registro.warning("%s no pudo extraer texto de %s: %s", modulo, ruta, error)
        return None


def _descomprimir_stream(crudo):
    """Apply ASCII85/FlateDecode until the content stream is obtained."""
    carga = crudo.strip(b"\r\n")
    for _ in range(3):
        try:
            return zlib.decompress(carga)
        except zlib.error:
            pass
        try:
            carga = base64.a85decode(carga, adobe=True)
        except (ValueError, TypeError):
            return None
    return None


def _cadenas_de_contenido(stream):
    """Extract the text literals (``(...) Tj`` / ``<...> Tj``) of the stream.

    Only covers PDFs with simple fonts (WinAnsi, as reportlab produces). With
    CID/subset fonts the result is noise and ``_texto_con_stdlib`` discards it
    on quality grounds.
    """
    fragmentos = []
    for coincidencia in re.finditer(rb"\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f\s]+>", stream):
        trozo = coincidencia.group(0)
        if trozo.startswith(b"("):
            crudo = trozo[1:-1]
            crudo = re.sub(rb"\\([nrtbf()\\])", lambda m: {
                b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"",
                b"f": b"", b"(": b"(", b")": b")", b"\\": b"\\",
            }[m.group(1)], crudo)
        else:
            hexa = re.sub(rb"\s+", b"", trozo[1:-1])
            if len(hexa) % 2:
                hexa += b"0"
            try:
                crudo = bytes.fromhex(hexa.decode("ascii"))
            except ValueError:
                continue
            # Two-bytes-per-glyph fonts: heuristic, validated afterwards.
            if crudo.count(b"\x00") > 0:
                crudo = crudo.replace(b"\x00", b"")
        fragmentos.append(crudo.decode("latin-1", "replace"))
    return " ".join(fragmentos)


def _texto_con_stdlib(ruta):
    """Last resort without dependencies: Flate/ASCII85 streams + literals.

    Returns ``None`` when the extracted text does not look readable (CID fonts,
    scanned or encrypted PDF), to avoid false missing-data reports.
    """
    try:
        with open(str(ruta), "rb") as manejador:
            crudo = manejador.read()
    except OSError:
        return None
    partes = []
    for coincidencia in _RE_PDF_STREAM.finditer(crudo):
        stream = _descomprimir_stream(coincidencia.group(1))
        if not stream or (b"Tj" not in stream and b"TJ" not in stream):
            continue
        partes.append(_cadenas_de_contenido(stream))
    texto = "\n".join(parte for parte in partes if parte)
    if len(texto) < 80:
        return None
    imprimibles = sum(1 for c in texto if c.isprintable() or c == "\n")
    letras = sum(1 for c in texto if c.isalpha())
    if imprimibles < len(texto) * 0.9 or letras < len(texto) * 0.3:
        return None
    return texto


def extraer_texto_pdf(ruta):
    """PDF text and the name of the extractor used.

    Order: ``pdftotext`` (poppler) → ``pypdf``/``PyPDF2`` → minimal stdlib
    extractor. Returns ``(None, None)`` when none is usable.
    """
    for nombre, extractor in (
        ("pdftotext", _texto_con_pdftotext),
        ("pypdf", _texto_con_pypdf),
        ("stdlib", _texto_con_stdlib),
    ):
        try:
            texto = extractor(ruta)
        except Exception as error:  # noqa: BLE001 — the gate must not break anything
            _registro.warning("Extractor %s falló sobre %s: %s", nombre, ruta, error)
            continue
        if texto and texto.strip():
            return texto, nombre
    return None, None


def _datos_duros_perfil(perfil):
    """List of ``(etiqueta, valor)`` pairs that must survive the render."""
    perfil = perfil if isinstance(perfil, dict) else {}
    identidad = perfil.get("identity") or {}
    datos = [
        ("nombre", identidad.get("nombre")),
        ("email", identidad.get("email")),
    ]
    for puesto in perfil.get("experience") or []:
        if isinstance(puesto, dict) and puesto.get("empresa"):
            datos.append(("empresa", puesto["empresa"]))
    for estudio in perfil.get("education") or []:
        if isinstance(estudio, dict) and estudio.get("institucion"):
            datos.append(("institución", estudio["institucion"]))
    return [(etiqueta, valor) for etiqueta, valor in datos if valor]


def verificar_pdf_cv(texto_markdown, ruta_pdf, perfil):
    """Check that the CV PDF keeps the hard data from the profile.

    Re-extracts the text of the freshly rendered PDF and looks for the name,
    the email, every company in ``perfil["experience"]`` and every institution
    in ``perfil["education"]``, comparing without accents or case.

    Args:
        texto_markdown: markdown the PDF came from. Only used as a length
            reference to detect a suspiciously short render.
        ruta_pdf: path of the generated PDF.
        perfil: profile dict (``identity``, ``experience``, ``education``).

    Returns:
        ``{"ok": bool|None, "faltantes": [str], "paginas": int|None,
        "motivo": str|None}``. ``ok`` is ``True`` when nothing is missing and
        the PDF has text, ``False`` when some data is missing or there is no
        text, and ``None`` when no PDF extractor is available (only the size
        was checked). Never raises: errors are returned in ``motivo``.
    """
    resultado = {"ok": False, "faltantes": [], "paginas": None, "motivo": None}

    ruta = str(ruta_pdf or "")
    try:
        if not ruta or not os.path.isfile(ruta):
            resultado["motivo"] = "el PDF no existe"
            return resultado
        tamano = os.path.getsize(ruta)
        if tamano <= _TAMANO_MINIMO_PDF:
            resultado["motivo"] = "PDF de %d bytes (posible render vacío)" % tamano
            return resultado
    except OSError as error:
        resultado["motivo"] = "no se pudo leer el PDF: %s" % error
        return resultado

    try:
        resultado["paginas"] = contar_paginas(ruta)[0] or None
    except Exception:  # noqa: BLE001 — the count is informational
        resultado["paginas"] = None

    texto, fuente = extraer_texto_pdf(ruta)
    if texto is None:
        resultado["ok"] = None
        resultado["motivo"] = "sin extractor de PDF"
        return resultado

    normalizado = _normalizar_comparable(texto)
    if not normalizado:
        resultado["motivo"] = "el PDF no contiene texto extraíble (%s)" % fuente
        return resultado

    faltantes = []
    for etiqueta, valor in _datos_duros_perfil(perfil):
        if not _contiene(normalizado, valor):
            faltantes.append("%s: %s" % (etiqueta, valor))
    resultado["faltantes"] = faltantes
    resultado["ok"] = not faltantes

    # Extra diagnosis with the source markdown: a PDF much shorter than its
    # text usually means lost content (even when no data is missing).
    markdown = _normalizar_comparable(texto_markdown)
    if markdown and len(normalizado) < len(markdown) * 0.5:
        resultado["motivo"] = (
            "texto extraído corto: %d chars frente a %d del markdown"
            % (len(normalizado), len(markdown))
        )
    elif faltantes:
        resultado["motivo"] = "datos del perfil ausentes en el PDF (%s)" % fuente
    return resultado


if __name__ == "__main__":
    # Manual self-check: render the base CV to a temporary PDF.
    _raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _origen = os.path.join(_raiz, "assets", "cv", "base-cv-es.md")
    _destino = os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "smoke_cv_base.pdf"
    )
    with open(_origen, "r", encoding="utf-8") as _fh:
        _texto = _fh.read()
    print(generar_pdf_cv(_texto, _destino))
