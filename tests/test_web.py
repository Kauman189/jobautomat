"""Tests de los endpoints de diagnóstico/export del panel (``service/web.py``).

Se saltan si Flask no está instalado (el runtime del servicio sí lo trae).
"""

import pytest

pytest.importorskip("flask")

import web  # noqa: E402


@pytest.fixture
def cliente(monkeypatch):
    """Test client de Flask con config y diagnóstico mockeados (sin red)."""
    cfg = {
        "DEEPSEEK_API_KEY": "sk-real",
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "WEB_PASSWORD": None,
        "SCORE_THRESHOLD": 80,
        "ROLES": ["Product Designer"],
    }
    monkeypatch.setattr(web.config, "load_config", lambda *a, **k: dict(cfg))
    monkeypatch.setattr(web, "_config_efectiva", lambda: dict(cfg))
    app = web.app
    app.config.update(TESTING=True)
    return app.test_client()


def test_api_salud_devuelve_el_diagnostico(cliente, monkeypatch):
    falso = {
        "deepseek": {"ok": True, "detalle": "mock"},
        "telegram": {"ok": True, "detalle": "mock"},
        "linkedin": {"ok": True, "detalle": "mock"},
        "data_escribible": {"ok": True, "detalle": "mock"},
        "chromium": {"ok": True, "detalle": "mock"},
        "ok": True,
        "generado_en": "2026-01-01T00:00:00+0000",
    }
    monkeypatch.setattr(web.health, "diagnostico", lambda cfg: dict(falso))

    respuesta = cliente.get("/api/salud")

    assert respuesta.status_code == 200
    datos = respuesta.get_json()
    assert datos["ok"] is True
    assert "deepseek" in datos and "chromium" in datos


def test_api_salud_convierte_excepcion_en_500(cliente, monkeypatch):
    def _explotar(cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(web.health, "diagnostico", _explotar)

    respuesta = cliente.get("/api/salud")

    assert respuesta.status_code == 500
    assert "boom" in respuesta.get_json()["error"]


def test_api_export_devuelve_perfil_y_settings_enmascarados(cliente, monkeypatch):
    monkeypatch.setattr(
        web.notify_mod,
        "_cargar_perfil",
        lambda: {"puesto": "Product Designer", "ciudad": "Madrid"},
    )

    respuesta = cliente.get("/api/export")

    assert respuesta.status_code == 200
    datos = respuesta.get_json()
    assert datos["perfil"]["puesto"] == "Product Designer"
    # Los secretos salen vacíos aunque estén configurados.
    assert datos["settings"]["DEEPSEEK_API_KEY"] == ""
    assert datos["settings"]["TELEGRAM_BOT_TOKEN"] == ""
    assert datos["settings"]["SCORE_THRESHOLD"] == 80


def test_api_export_falla_si_el_perfil_no_es_objeto(cliente, monkeypatch):
    monkeypatch.setattr(web.notify_mod, "_cargar_perfil", lambda: ["no", "es", "dict"])

    respuesta = cliente.get("/api/export")

    assert respuesta.status_code == 500
    assert "perfil" in respuesta.get_json()["error"]
