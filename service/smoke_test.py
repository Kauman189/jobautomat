"""smoke_test.py — Comprobación integral del servicio usando sólo stdlib.

Ejecuta tres verificaciones:
  (a) load_config() imprimiendo SOLO los nombres de claves.
  (b) init_db en /tmp + insertar + listar + is_nueva con una op de ejemplo.
  (c) llm.chat con 'responde exactamente: ok' (llamada REAL a DeepSeek).

No imprime la API key ni ningún otro secreto. Se ejecuta con ``python3
smoke_test.py`` desde ``service/`` sin necesidad de venv.
"""

import os
import sys
import tempfile

# Asegurar que el directorio de este script (service/) esté en sys.path
# para poder importar config, llm y db sin instalar nada.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as config_mod  # noqa: E402
import db as db_mod  # noqa: E402
import llm as llm_mod  # noqa: E402


def check_config():
    """(a) Carga configuración e imprime sólo los nombres de claves."""
    print("=== (a) load_config() ===")
    cfg = config_mod.load_config()
    print("Claves cargadas:", sorted(cfg.keys()))
    # Verificaciones mínimas sin exponer valores.
    assert isinstance(cfg.get("SCORE_THRESHOLD"), int), "SCORE_THRESHOLD debe ser int"
    assert cfg.get("DEEPSEEK_API_KEY"), "Falta DEEPSEEK_API_KEY"
    assert cfg.get("TELEGRAM_BOT_TOKEN"), "Falta TELEGRAM_BOT_TOKEN"
    assert cfg.get("DB_PATH") == "state.db", "DB_PATH por defecto debe ser state.db"
    assert cfg.get("PERFIL_PATH") == "../data/perfil.json", "PERFIL_PATH por defecto incorrecto"
    print("OK: config válida (SCORE_THRESHOLD int, obligatorias presentes, defaults OK).")


def check_db():
    """(b) Base SQLite en /tmp: init, insertar, listar, is_nueva."""
    print("=== (b) sqlite en /tmp ===")
    db_path = os.path.join(tempfile.gettempdir(), "jobautomat_smoke.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    db_mod.init_db(db_path)

    # Ruta por defecto: se resuelve desde DB_PATH (.env) al directorio service/.
    print("DB_PATH por defecto resuelto a:", os.path.basename(db_mod._resolver_path(None)))

    op = {
        "id": "smoke-001",
        "url": "https://example.com/job/1",
        "titulo": "Software Engineer",
        "empresa": "Ejemplo S.L.",
        "salario": "40k-45k EUR",
        "ubicacion": "Valencia",
        "remoto": "100%",
        "contrato": "Laboral",
        "estado": "nueva",
        "score": 85,
        "cover_letter": None,
        "cv_adaptado": None,
        "mensaje": None,
        "created_at": "2026-10-21T00:00:00+00:00",
        "updated_at": None,
    }

    assert db_mod.is_nueva(op, path=db_path) is True, "Debe ser nueva antes de insertar"
    assert db_mod.insertar(op, path=db_path) is True, "Debe insertarse la primera vez"
    assert db_mod.is_nueva(op, path=db_path) is False, "No debe ser nueva tras insertar"

    # Insertar de nuevo no debe duplicar.
    assert db_mod.insertar(op, path=db_path) is False, "INSERT OR IGNORE evita duplicados"

    lista = db_mod.listar(path=db_path)
    assert len(lista) == 1, f"Se esperaba 1 fila, hay {len(lista)}"
    fila = lista[0]
    assert fila["titulo"] == "Software Engineer"
    assert fila["score"] == 85

    # Cambiar estado y comprobar que updated_at se rellena.
    assert db_mod.set_estado("smoke-001", "enviada", path=db_path) is True
    fila_actualizada = db_mod.listar(path=db_path)[0]
    assert fila_actualizada["estado"] == "enviada"
    assert fila_actualizada["updated_at"]

    # Filtrar por estado.
    assert len(db_mod.listar(estado="enviada", path=db_path)) == 1
    assert len(db_mod.listar(estado="descartada", path=db_path)) == 0

    print(f"OK: init/insertar/listar/is_nueva/set_estado funcionan en {db_path}.")
    os.remove(db_path)


def check_llm():
    """(c) Llamada real a DeepSeek: 'responde exactamente: ok'.

    Se prueba primero con max_tokens=10 (según el contrato del smoke test) y
    después con un presupuesto suficiente, porque `deepseek-flash` es un
    modelo de razonamiento: con 10 tokens el presupuesto se agota en
    `reasoning_content` y `content` llega vacío.
    """
    print("=== (c) llm.chat (llamada real) ===")
    mensajes = [{"role": "user", "content": "responde exactamente: ok"}]

    try:
        respuesta = llm_mod.chat(mensajes, max_tokens=10)
        limpia = (respuesta or "").strip().lower()
        if limpia == "ok":
            print("OK (max_tokens=10): el modelo devolvió 'ok'.")
        else:
            print(f"AVISO (max_tokens=10): devolvió '{limpia}' (no exactamente 'ok').")
    except Exception as err:  # noqa: BLE001 — capturamos todo para reportar.
        print(f"ERROR (max_tokens=10): {type(err).__name__}: {err}")

    # Control con presupuesto suficiente: valida conexión, autenticación y
    # extracción de 'choices[0].message.content'.
    try:
        respuesta = llm_mod.chat(mensajes, max_tokens=300)
        limpia = (respuesta or "").strip().lower()
        estado = "OK" if limpia == "ok" else "AVISO"
        print(f"{estado} (max_tokens=300): devolvió {limpia!r}.")
    except Exception as err:  # noqa: BLE001
        print(f"ERROR (max_tokens=300): {type(err).__name__}: {err}")

    # Control de modo JSON: complete_json debe devolver un dict parseado.
    try:
        datos = llm_mod.complete_json(
            'Devuelve exactamente este JSON: {"ok": true}', max_tokens=300
        )
        print(f"OK (complete_json): dict parseado -> {datos}")
    except Exception as err:  # noqa: BLE001
        print(f"ERROR (complete_json): {type(err).__name__}: {err}")


if __name__ == "__main__":
    check_config()
    check_db()
    check_llm()
    print("=== Smoke test terminado ===")
