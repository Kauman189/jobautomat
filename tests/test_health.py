"""Tests de ``service/health.py`` con la red mockeada (nunca sale a internet)."""

import health
import pytest

CLAVES_ESPERADAS = {
    "deepseek",
    "telegram",
    "linkedin",
    "data_escribible",
    "chromium",
    "ok",
    "generado_en",
}


class _RespuestaFalsa:
    """Context manager mínimo que imita ``urllib.request.urlopen``."""

    def __init__(self, status=200, cuerpo=b'{"ok": true, "result": {"username": "bot", "id": 1}}'):
        self.status = status
        self._cuerpo = cuerpo

    def read(self):
        return self._cuerpo

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def cfg_sana(tmp_path):
    """Config completa apuntando a un directorio temporal escribible."""
    sesion = tmp_path / "linkedin_session.json"
    sesion.write_text("{}", encoding="utf-8")
    return {
        "DEEPSEEK_API_KEY": "sk-test",
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
        "TELEGRAM_HABILITADO": True,
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "SESSION_PATH": str(sesion),
        "DB_PATH": str(tmp_path / "state.db"),
        "PERFIL_PATH": str(tmp_path / "perfil.json"),
    }


@pytest.fixture
def red_falsa(monkeypatch):
    llamadas = []

    def _urlopen(peticion, timeout=None):
        url = getattr(peticion, "full_url", str(peticion))
        llamadas.append(url)
        return _RespuestaFalsa()

    monkeypatch.setattr(health.urllib.request, "urlopen", _urlopen)
    # Chromium depende del entorno (patchright/playwright instalados o no).
    monkeypatch.setattr(health, "_comprobar_chromium", lambda cfg: {"ok": True, "detalle": "mock"})
    return llamadas


def test_diagnostico_devuelve_estructura_esperada(cfg_sana, red_falsa):
    resultado = health.diagnostico(cfg_sana)

    assert CLAVES_ESPERADAS <= set(resultado)
    assert isinstance(resultado["ok"], bool)
    assert resultado["ok"] is True
    assert isinstance(resultado["generado_en"], str) and resultado["generado_en"]
    for nombre in ("deepseek", "telegram", "linkedin", "data_escribible", "chromium"):
        assert isinstance(resultado[nombre], dict)
        assert "ok" in resultado[nombre]


def test_diagnostico_no_sale_a_internet(cfg_sana, red_falsa):
    health.diagnostico(cfg_sana)
    # Sólo endpoints de DeepSeek y Telegram (getMe), nunca otro host.
    assert red_falsa, "se esperaban llamadas mockeadas"
    assert all("api.deepseek.com" in url or "api.telegram.org" in url for url in red_falsa)


def test_diagnostico_falla_si_deepseek_no_responde(cfg_sana, red_falsa, monkeypatch):
    def _urlopen_roto(peticion, timeout=None):
        raise OSError("sin red")

    monkeypatch.setattr(health.urllib.request, "urlopen", _urlopen_roto)
    resultado = health.diagnostico(cfg_sana)

    assert resultado["deepseek"]["ok"] is False
    assert resultado["ok"] is False


def test_diagnostico_sin_api_key_no_llama_a_deepseek(cfg_sana, red_falsa):
    cfg_sana.pop("DEEPSEEK_API_KEY")
    resultado = health.diagnostico(cfg_sana)

    assert resultado["deepseek"]["ok"] is False
    assert "DEEPSEEK_API_KEY" in resultado["deepseek"]["detalle"]


def test_telegram_deshabilitado_se_omite_y_no_penaliza(cfg_sana, red_falsa):
    cfg_sana["TELEGRAM_HABILITADO"] = False
    resultado = health.diagnostico(cfg_sana)

    assert resultado["telegram"]["omitido"] is True
    assert resultado["telegram"]["ok"] is True
    assert resultado["ok"] is True


def test_sesion_linkedin_ausente_marca_ok_false(cfg_sana, red_falsa, tmp_path):
    cfg_sana["SESSION_PATH"] = str(tmp_path / "no-existe.json")
    resultado = health.diagnostico(cfg_sana)

    assert resultado["linkedin"]["presente"] is False
    assert resultado["linkedin"]["ok"] is False
    assert resultado["ok"] is False


def test_diagnostico_nunca_lanza_con_config_vacia(red_falsa):
    resultado = health.diagnostico({})
    assert CLAVES_ESPERADAS <= set(resultado)
    assert isinstance(resultado["ok"], bool)


def test_resumen_una_linea(cfg_sana, red_falsa):
    linea = health.resumen(health.diagnostico(cfg_sana))
    assert linea.startswith("global=ok ")
    assert "\n" not in linea
    assert "deepseek=ok" in linea
