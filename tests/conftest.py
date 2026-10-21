"""conftest.py — preparación común de los tests de jobautomat.

Los módulos de ``service/`` usan imports planos (``import config``,
``import db``…) porque están pensados para ejecutarse desde dentro de
``service/``. Aquí se añade ese directorio al ``PYTHONPATH`` para poder
importarlos desde ``tests/``.

Además se aísla cada test de la configuración real del repo: ``SETTINGS_PATH``
apunta a un fichero inexistente dentro de ``tmp_path`` para que
``data/settings.json`` (que puede tener valores locales) no altere las
aserciones.
"""

import os
import sys

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICIO = os.path.join(RAIZ, "service")
if SERVICIO not in sys.path:
    sys.path.insert(0, SERVICIO)


@pytest.fixture(autouse=True)
def _settings_aislados(tmp_path, monkeypatch):
    """Ningún test lee ni escribe ``data/settings.json`` del repo."""
    ruta = tmp_path / "settings-inexistente.json"
    monkeypatch.setenv("SETTINGS_PATH", str(ruta))
    # WEB_PASSWORD del entorno activaría Basic auth en el panel.
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    return ruta


@pytest.fixture
def escribir_env(tmp_path):
    """Fábrica de ficheros ``.env`` temporales: ``escribir_env("A=1")``."""

    def _escribir(contenido, nombre="env-test"):
        ruta = tmp_path / nombre
        ruta.write_text(contenido, encoding="utf-8")
        return str(ruta)

    return _escribir
