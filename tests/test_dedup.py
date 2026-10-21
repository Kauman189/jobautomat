"""Tests de la deduplicación de ofertas: ``service/main.py``.

Cubre ``_normalizar_texto_clave``, ``_claves_deduplicacion`` y
``_filtrar_keywords_negativas``. Ninguna de estas funciones toca la red.
"""

import pytest

import main


# --- _normalizar_texto_clave ---------------------------------------------

@pytest.mark.parametrize(
    "texto",
    [
        "Acme",
        "Acme Inc",
        "ACME, INC.",
        "Acme LLC",
        "Acme Ltd.",
        "Acme GmbH",
        "Acme Corp",
    ],
)
def test_normalizar_quita_sufijos_societarios(texto):
    assert main._normalizar_texto_clave(texto) == "acme"


@pytest.mark.parametrize("texto", ["Estudio S.L.", "Estudio S. L.", "Estudio SL"])
def test_normalizar_sufijos_espanoles(texto):
    assert main._normalizar_texto_clave(texto) == "estudio"


@pytest.mark.parametrize("texto", ["Diseñador", "DISENADOR", "  diseñador  "])
def test_normalizar_quita_acentos_y_normaliza_caja(texto):
    assert main._normalizar_texto_clave(texto) == "disenador"


def test_normalizar_quita_parentesis_de_modalidad():
    assert main._normalizar_texto_clave(
        "Product Designer (Remote)"
    ) == main._normalizar_texto_clave("Product Designer")


def test_normalizar_conserva_parentesis_que_distinguen():
    ux = main._normalizar_texto_clave("Designer (UX)")
    ui = main._normalizar_texto_clave("Designer (UI)")
    assert ux != ui
    assert "ux" in ux and "ui" in ui


def test_normalizar_texto_vacio():
    assert main._normalizar_texto_clave("") == ""
    assert main._normalizar_texto_clave(None) == ""


# --- _claves_deduplicacion ------------------------------------------------

def test_claves_estables_para_la_misma_oferta():
    oferta = {
        "titulo": "Product Designer",
        "empresa": "Acme Inc",
        "ubicacion": "Madrid",
        "url": "https://example.com/jobs/1",
    }
    primera = main._claves_deduplicacion(oferta)
    segunda = main._claves_deduplicacion(dict(oferta))
    assert primera == segunda
    assert primera == [
        "url:https://example.com/jobs/1",
        "te:product designer|acme",
    ]


def test_claves_iguales_entre_fuentes_con_empresa_escrita_distinto():
    linkedin = {
        "titulo": "Diseñador de Producto (Remote)",
        "empresa": "Acme, LLC",
        "url": "https://linkedin.com/jobs/view/1?ref=abc#top",
    }
    board = {
        "title": "Disenador de Producto",
        "company": "ACME Inc.",
        "job_url": "https://linkedin.com/jobs/view/1/",
    }
    assert main._claves_deduplicacion(linkedin) == main._claves_deduplicacion(board)


def test_claves_distintas_si_cambia_el_puesto():
    a = main._claves_deduplicacion({"titulo": "Designer (UX)", "empresa": "Acme"})
    b = main._claves_deduplicacion({"titulo": "Designer (UI)", "empresa": "Acme"})
    assert a != b


def test_claves_sin_empresa_no_acaban_en_separador():
    assert main._claves_deduplicacion({"titulo": "Product Designer"}) == [
        "te:product designer"
    ]


def test_claves_sin_datos_devuelve_lista_vacia():
    assert main._claves_deduplicacion({}) == []


def test_fusionar_fuentes_descarta_duplicados_y_respeta_prioridad():
    linkedin = [
        {"titulo": "Product Designer", "empresa": "Acme", "fuente": "linkedin"},
    ]
    board = [
        {"titulo": "Product Designer (Remote)", "empresa": "Acme Inc.", "fuente": "board"},
        {"titulo": "UX Researcher", "empresa": "Acme", "fuente": "board"},
    ]
    fusion = main._fusionar_fuentes(linkedin, board)
    assert [o["fuente"] for o in fusion] == ["linkedin", "board"]
    assert len(fusion) == 2


# --- _filtrar_keywords_negativas -----------------------------------------

OFERTAS = [
    {"titulo": "Product Designer", "descripcion": "Diseño de producto en SaaS."},
    {"titulo": "Gaming UI Designer", "descripcion": "Casino y apuestas."},
    {"titulo": "Diseñador", "descripcion": "Equipo de CRYPTO y trading."},
]


def test_sin_keywords_devuelve_la_lista_intacta():
    assert main._filtrar_keywords_negativas(OFERTAS, keywords=[]) == OFERTAS


def test_filtra_por_palabra_completa_en_titulo_y_descripcion():
    salida = main._filtrar_keywords_negativas(OFERTAS, keywords=["gaming", "cripto"])
    # "CRYPTO" -> "crypto" no coincide con "cripto": sólo cae el de gaming.
    assert [o["titulo"] for o in salida] == ["Product Designer", "Diseñador"]


def test_no_descarta_coincidencias_parciales():
    ofertas = [{"titulo": "Ventas y marketing", "descripcion": ""}]
    assert main._filtrar_keywords_negativas(ofertas, keywords=["venta"]) == ofertas


def test_normaliza_acentos_en_las_keywords():
    ofertas = [{"titulo": "Diseño Gráfico", "descripcion": ""}, {"titulo": "Product", "descripcion": ""}]
    salida = main._filtrar_keywords_negativas(ofertas, keywords=["diseño grafico"])
    assert [o["titulo"] for o in salida] == ["Product"]


def test_keywords_desde_config():
    config = {"KEYWORDS_NEGATIVAS": ["Gaming", "gaming"]}
    salida = main._filtrar_keywords_negativas(OFERTAS, config=config)
    assert "Gaming UI Designer" not in [o["titulo"] for o in salida]
