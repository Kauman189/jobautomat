"""Tests de ``service/config.py``: configuración mínima y valores por defecto."""

import pytest

import config


# --- validar_minimo -------------------------------------------------------

def test_validar_minimo_sin_api_key_lanza_runtime_error():
    with pytest.raises(RuntimeError) as excinfo:
        config.validar_minimo({})
    assert "DEEPSEEK_API_KEY" in str(excinfo.value)


def test_validar_minimo_api_key_vacia_tambien_falla():
    with pytest.raises(RuntimeError):
        config.validar_minimo({"DEEPSEEK_API_KEY": "   "})


def test_validar_minimo_avisa_si_telegram_activo_sin_token():
    avisos = config.validar_minimo(
        {"DEEPSEEK_API_KEY": "sk-test", "TELEGRAM_HABILITADO": True}
    )
    assert len(avisos) == 1
    assert "TELEGRAM_BOT_TOKEN" in avisos[0]


def test_validar_minimo_sin_avisos_si_telegram_deshabilitado():
    avisos = config.validar_minimo(
        {
            "DEEPSEEK_API_KEY": "sk-test",
            "TELEGRAM_HABILITADO": False,
            "TELEGRAM_BOT_TOKEN": None,
        }
    )
    assert avisos == []


def test_validar_minimo_sin_avisos_con_token():
    avisos = config.validar_minimo(
        {
            "DEEPSEEK_API_KEY": "sk-test",
            "TELEGRAM_HABILITADO": True,
            "TELEGRAM_BOT_TOKEN": "123:abc",
        }
    )
    assert avisos == []


# --- defaults de las claves nuevas (notificaciones / self-host) -----------

CLAVES_NUEVAS = {
    "TELEGRAM_HABILITADO": True,
    "WEBHOOK_URL": "",
    "ALERTAR_SESION_LINKEDIN": True,
    "SESION_LINKEDIN_MAX_DIAS": 14,
}


def test_las_cuatro_claves_nuevas_estan_en_el_spec_con_su_default():
    for clave, esperado in CLAVES_NUEVAS.items():
        assert clave in config.CONFIG_SPEC, clave
        assert config.config_por_defecto()[clave] == esperado


def test_load_config_aplica_los_defaults_de_las_claves_nuevas(escribir_env):
    ruta = escribir_env("DEEPSEEK_API_KEY=sk-test\n")
    cfg = config.load_config(ruta, estricto=False)
    for clave, esperado in CLAVES_NUEVAS.items():
        assert cfg[clave] == esperado, clave


# --- load_config ----------------------------------------------------------

def test_load_config_estricto_sin_api_key_falla(escribir_env):
    ruta = escribir_env("# sin claves\n")
    with pytest.raises(RuntimeError):
        config.load_config(ruta, estricto=True)


def test_load_config_no_estricto_sin_api_key_no_falla(escribir_env):
    ruta = escribir_env("")
    cfg = config.load_config(ruta, estricto=False)
    assert cfg["DEEPSEEK_API_KEY"] is None
    assert cfg["SCORE_THRESHOLD"] == 80  # conversion int del default


def test_load_config_convierte_tipos_del_env(escribir_env):
    ruta = escribir_env(
        "DEEPSEEK_API_KEY=sk-test\n"
        "SCORE_THRESHOLD=60\n"
        "TELEGRAM_HABILITADO=false\n"
        "ROLES=[\"Product Designer\"]\n"
        "TEMPERATURA=0.7\n"
    )
    cfg = config.load_config(ruta, estricto=True)
    assert cfg["SCORE_THRESHOLD"] == 60
    assert cfg["TELEGRAM_HABILITADO"] is False
    assert cfg["ROLES"] == ["Product Designer"]
    assert cfg["TEMPERATURA"] == pytest.approx(0.7)


def test_load_config_estricto_avisa_pero_no_falla_sin_token_telegram(escribir_env, capsys):
    ruta = escribir_env("DEEPSEEK_API_KEY=sk-test\nTELEGRAM_HABILITADO=true\n")
    cfg = config.load_config(ruta, estricto=True)
    assert cfg["TELEGRAM_BOT_TOKEN"] is None
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
