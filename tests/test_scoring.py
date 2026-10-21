"""Tests de la caché de puntuaciones de ``service/scoring.py`` (sin LLM)."""

import scoring

OFERTA = {
    "titulo": "Product Designer",
    "empresa": "Acme",
    "ubicacion": "Madrid",
}


def test_id_cache_es_determinista():
    assert scoring.id_cache_oferta(OFERTA) == scoring.id_cache_oferta(dict(OFERTA))


def test_id_cache_estable_entre_variantes_de_escritura():
    variante = {
        "title": "  product   DESIGNER ",
        "company": "ACME",
        "location": "Madrid",
    }
    assert scoring.id_cache_oferta(OFERTA) == scoring.id_cache_oferta(variante)


def test_id_cache_ignora_acentos_puntuacion_y_descripcion():
    con_acentos = {
        "titulo": "Diseñador de Producto, S.L.",
        "empresa": "Ácme",
        "ubicacion": "Mádrid",
        "descripcion": "Texto distinto que no debe cambiar la clave.",
    }
    plano = {
        "titulo": "Disenador de Producto S L",
        "empresa": "Acme",
        "ubicacion": "Madrid",
    }
    assert scoring.id_cache_oferta(con_acentos) == scoring.id_cache_oferta(plano)


def test_id_cache_cambia_con_el_puesto():
    otra = dict(OFERTA, titulo="UX Designer")
    assert scoring.id_cache_oferta(OFERTA) != scoring.id_cache_oferta(otra)


def test_id_cache_tiene_prefijo_y_longitud_estable():
    clave = scoring.id_cache_oferta(OFERTA)
    assert clave.startswith("sc1:")
    assert len(clave) == len("sc1:") + 32
    int(clave[len("sc1:") :], 16)  # es hexadecimal


def test_id_cache_sin_datos_devuelve_none():
    assert scoring.id_cache_oferta({}) is None
    assert scoring.id_cache_oferta(None) is None


def test_score_usa_la_cache_sin_llamar_al_llm(monkeypatch):
    """Si hay caché, ``score()`` devuelve el valor guardado y no puntúa."""
    cacheado = {
        "score": 77,
        "motivos": ["cacheado"],
        "gaps": [],
        "dimensiones": {},
        "red_flags": [],
        "recomendacion": "aplicar",
        "citas": [],
    }
    llamadas = {"llm": 0}

    monkeypatch.setattr(scoring, "_leer_cache", lambda clave: cacheado)

    def _no_llamar(*args, **kwargs):  # pragma: no cover - debe no ejecutarse
        llamadas["llm"] += 1
        raise AssertionError("no debería llamarse al LLM con caché")

    for nombre in ("_llamar_llm", "_evaluar_con_llm", "_llm", "llm_json"):
        if hasattr(scoring, nombre):
            monkeypatch.setattr(scoring, nombre, _no_llamar)

    resultado = scoring.score(OFERTA, {"puesto": "Product Designer"})
    assert resultado["score"] == 77
    assert llamadas["llm"] == 0


def test_leer_cache_devuelve_none_con_id_none(monkeypatch):
    # ``_leer_cache`` con clave None no debe romper: devuelve miss.
    assert scoring._leer_cache(None) is None
