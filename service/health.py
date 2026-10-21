"""health.py — Health diagnostics for the jobautomat self-host.

Checks, without ever raising, the five spots that usually break in an
own deployment:

  * ``deepseek``        — key present and endpoint responding.
  * ``telegram``        — bot ``getMe`` (skipped when disabled).
  * ``linkedin``        — session file present and its age.
  * ``data_escribible`` — the ``/data`` volume is writable.
  * ``chromium``        — patchright/playwright resolve a browser.

Stdlib only (``urllib``), so it runs in the container with nothing installed::

    python -m service.health          # from the repo root
    python health.py                  # from service/

Output is a JSON-serializable dict with a top-level ``ok`` key that is true
only if every *non-skipped* block is fine. Meant to feed a Docker
``healthcheck`` or a status panel.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TIMEOUT_RED = 5  # seconds; short on purpose: this is a diagnostic, not a wait

# Default LinkedIn session path, same as search/linkedin.py.
RUTA_SESION_DEFECTO = "../data/linkedin_session.json"

# Age at which the LinkedIn session is considered expired.
MAX_DIAS_SESION_DEFECTO = 14  # same default as config.SESION_LINKEDIN_MAX_DIAS

_TRUE = ("true", "1", "yes", "y", "si", "sí", "on", "verdadero")
_FALSE = ("false", "0", "no", "n", "off", "falso", "")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _a_bool(valor, por_defecto=True):
    """Tolerant bool: accepts bools, numbers and the usual strings."""
    if valor is None:
        return por_defecto
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, (int, float)):
        return bool(valor)
    texto = str(valor).strip().lower()
    if texto in _TRUE:
        return True
    if texto in _FALSE:
        return False
    return por_defecto


def _valor(cfg, clave, por_defecto=None):
    """Read ``clave`` from config, falling back to the environment.

    ``config.py`` only knows a subset of keys (``SESSION_PATH`` and
    ``SESION_LINKEDIN_MAX_DIAS`` live in the environment because
    ``search/linkedin.py`` reads them), so both layers are checked.
    """
    if cfg:
        valor = cfg.get(clave)
        if valor not in (None, ""):
            return valor
    valor = os.environ.get(clave)
    if valor not in (None, ""):
        return valor
    return por_defecto


def _ruta_absoluta(ruta):
    """Resolve ``ruta`` against ``service/`` when relative (like config.py)."""
    ruta = str(ruta).strip()
    if not os.path.isabs(ruta):
        ruta = os.path.normpath(os.path.join(BASE_DIR, ruta))
    return ruta


def _bloque_ok(detalle, **extra):
    datos = {"ok": True, "detalle": detalle}
    datos.update(extra)
    return datos


def _bloque_error(detalle, **extra):
    datos = {"ok": False, "detalle": detalle}
    datos.update(extra)
    return datos


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _comprobar_deepseek(cfg):
    """Key present + minimal request to the DeepSeek endpoint.

    ``GET /models`` is tried first (cheap, no tokens); if the endpoint does
    not expose it (404/405) a ``POST /chat/completions`` with
    ``max_tokens=1`` is used, which also validates key and network.
    """
    clave = _valor(cfg, "DEEPSEEK_API_KEY")
    if not clave:
        return _bloque_error("sin DEEPSEEK_API_KEY en .env")

    base = str(_valor(cfg, "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")).rstrip("/")
    modelo = str(_valor(cfg, "DEEPSEEK_MODEL", "deepseek-flash"))
    cabeceras = {"Authorization": "Bearer %s" % clave}
    inicio = time.time()

    # 1) Model listing.
    try:
        peticion = urllib.request.Request(base + "/models", headers=cabeceras)
        with urllib.request.urlopen(peticion, timeout=TIMEOUT_RED) as respuesta:
            if respuesta.status == 200:
                return _bloque_ok(
                    "endpoint /models responde 200",
                    endpoint=base + "/models",
                    latencia_ms=int((time.time() - inicio) * 1000),
                )
            estado_models = respuesta.status
    except urllib.error.HTTPError as err:
        estado_models = err.code
        if err.code not in (404, 405):
            # 401/403 = bad key; 5xx = service down. Do not retry.
            return _bloque_error(
                "DeepSeek respondió HTTP %s en /models (¿clave inválida o servicio caído?)"
                % err.code, endpoint=base + "/models",
            )
    except Exception as err:  # noqa: BLE001 — network down, DNS, TLS...
        return _bloque_error("sin respuesta de DeepSeek: %s" % err, endpoint=base + "/models")
    else:
        estado_models = None

    # 2) Fallback: a one-token completion.
    cuerpo = json.dumps({
        "model": modelo,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }).encode("utf-8")
    cabeceras_post = dict(cabeceras)
    cabeceras_post["Content-Type"] = "application/json"
    try:
        peticion = urllib.request.Request(
            base + "/chat/completions", data=cuerpo, headers=cabeceras_post, method="POST"
        )
        with urllib.request.urlopen(peticion, timeout=TIMEOUT_RED) as respuesta:
            if respuesta.status == 200:
                return _bloque_ok(
                    "chat/completions responde 200",
                    endpoint=base + "/chat/completions",
                    latencia_ms=int((time.time() - inicio) * 1000),
                )
            return _bloque_error(
                "DeepSeek respondió HTTP %s en chat/completions" % respuesta.status,
                endpoint=base + "/chat/completions",
            )
    except urllib.error.HTTPError as err:
        return _bloque_error(
            "DeepSeek respondió HTTP %s en chat/completions (¿clave inválida?)" % err.code,
            endpoint=base + "/chat/completions",
            http_models=estado_models,
        )
    except Exception as err:  # noqa: BLE001
        return _bloque_error(
            "sin respuesta de DeepSeek: %s" % err, endpoint=base + "/chat/completions"
        )


def _comprobar_telegram(cfg):
    """Bot ``getMe``. Skipped when Telegram is disabled."""
    habilitado = _a_bool(_valor(cfg, "TELEGRAM_HABILITADO", True), True)
    if not habilitado:
        return {"ok": True, "omitido": True, "detalle": "TELEGRAM_HABILITADO=false"}

    token = _valor(cfg, "TELEGRAM_BOT_TOKEN")
    if not token:
        return _bloque_error("sin TELEGRAM_BOT_TOKEN en .env")

    url = "https://api.telegram.org/bot%s/getMe" % token
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_RED) as respuesta:
            datos = json.loads(respuesta.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as err:
        # 401/404 = revoked or mistyped token.
        return _bloque_error("getMe respondió HTTP %s (¿token inválido?)" % err.code)
    except Exception as err:  # noqa: BLE001
        return _bloque_error("sin respuesta de Telegram: %s" % err)

    if not datos.get("ok"):
        return _bloque_error("getMe devolvió ok=false: %s" % (datos.get("description") or "sin descripción"))
    bot = datos.get("result") or {}
    return _bloque_ok(
        "bot @%s operativo" % bot.get("username", "?"),
        bot_username=bot.get("username"),
        bot_id=bot.get("id"),
    )


def _comprobar_linkedin(cfg):
    """LinkedIn session: existence and age of the cookies file."""
    crudo = _valor(cfg, "SESSION_PATH") or _valor(cfg, "LINKEDIN_SESSION_PATH") or RUTA_SESION_DEFECTO
    ruta = _ruta_absoluta(crudo)
    max_dias = _valor(cfg, "SESION_LINKEDIN_MAX_DIAS", MAX_DIAS_SESION_DEFECTO)
    try:
        max_dias = int(float(str(max_dias).strip()))
    except (TypeError, ValueError):
        max_dias = MAX_DIAS_SESION_DEFECTO

    if not os.path.isfile(ruta):
        return {
            "ok": False,
            "presente": False,
            "ruta": ruta,
            "antiguedad_dias": None,
            "max_dias": max_dias,
            "caducada": True,
            "detalle": "no existe el fichero de sesión (exporta las cookies de LinkedIn)",
        }

    antiguedad_dias = (time.time() - os.path.getmtime(ruta)) / 86400.0
    caducada = antiguedad_dias > max_dias
    if caducada:
        detalle = "sesión de hace %.1f días (máx. %d): conviene renovarla" % (antiguedad_dias, max_dias)
    else:
        detalle = "sesión de hace %.1f días (máx. %d)" % (antiguedad_dias, max_dias)
    return {
        "ok": not caducada,
        "presente": True,
        "ruta": ruta,
        "antiguedad_dias": round(antiguedad_dias, 1),
        "max_dias": max_dias,
        "caducada": caducada,
        "detalle": detalle,
    }


def _comprobar_data(cfg):
    """Real write test (create and delete a ``.tmp``) in each directory."""
    rutas = {
        "db": _valor(cfg, "DB_PATH", "state.db"),
        "perfil": _valor(cfg, "PERFIL_PATH", "../data/perfil.json"),
    }
    directorios = []
    for ruta in rutas.values():
        directorio = os.path.dirname(_ruta_absoluta(ruta)) or "."
        if directorio not in directorios:
            directorios.append(directorio)

    fallos = []
    comprobados = []
    for directorio in directorios:
        comprobados.append(directorio)
        if not os.path.isdir(directorio):
            fallos.append("%s no existe" % directorio)
            continue
        temporal = os.path.join(directorio, ".health-%d.tmp" % os.getpid())
        try:
            with open(temporal, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(temporal)
        except Exception as err:  # noqa: BLE001
            fallos.append("%s no escribible: %s" % (directorio, err))
            try:
                if os.path.exists(temporal):
                    os.remove(temporal)
            except OSError:
                pass

    if fallos:
        return _bloque_error("; ".join(fallos), directorios=comprobados)
    return _bloque_ok("escritura correcta en %s" % ", ".join(comprobados), directorios=comprobados)


def _comprobar_chromium(cfg):
    """patchright (preferred) or playwright resolve a Chromium binary."""
    try:
        from patchright.sync_api import sync_playwright  # noqa: F401
        libreria = "patchright"
    except Exception:  # noqa: BLE001 — not installed: try playwright
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
            libreria = "playwright"
        except Exception:  # noqa: BLE001
            return _bloque_error("sin librería (ni patchright ni playwright instalados)")

    try:
        from patchright.sync_api import sync_playwright as _sp  # noqa: F811
    except Exception:  # noqa: BLE001
        from playwright.sync_api import sync_playwright as _sp  # noqa: F811

    try:
        with _sp() as pw:
            ejecutable = pw.chromium.executable_path
    except Exception as err:  # noqa: BLE001 — browser not downloaded, etc.
        return _bloque_error("%s no resuelve Chromium: %s" % (libreria, err), libreria=libreria)

    if not ejecutable or not os.path.isfile(ejecutable):
        return _bloque_error(
            "%s no encuentra el binario de Chromium (¿faltan los navegadores?)" % libreria,
            libreria=libreria,
            ejecutable=ejecutable,
        )
    return _bloque_ok("Chromium resuelto por %s" % libreria, libreria=libreria, ejecutable=ejecutable)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def diagnostico(cfg):
    """Run every check and return a JSON-serializable dict.

    Never raises: each block is wrapped in its own ``try/except`` and an
    unexpected failure becomes ``ok=False`` with the error as detail.

    Args:
        cfg: configuration dict (``config.load_config()``). May be ``None``
             or incomplete: missing values are taken from ``os.environ``.

    Returns:
        dict with ``deepseek``, ``telegram``, ``linkedin``,
        ``data_escribible``, ``chromium``, ``ok`` and ``generado_en``.
    """
    cfg = cfg or {}
    comprobaciones = (
        ("deepseek", _comprobar_deepseek),
        ("telegram", _comprobar_telegram),
        ("linkedin", _comprobar_linkedin),
        ("data_escribible", _comprobar_data),
        ("chromium", _comprobar_chromium),
    )

    resultado = {}
    for nombre, funcion in comprobaciones:
        try:
            resultado[nombre] = funcion(cfg)
        except Exception as err:  # noqa: BLE001 — diagnostics must never bring anything down
            resultado[nombre] = _bloque_error("error inesperado en la comprobación: %s" % err)

    resultado["ok"] = all(
        bloque.get("ok") for bloque in resultado.values() if not bloque.get("omitido")
    )
    resultado["generado_en"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return resultado


def resumen(resultado):
    """Single-line text summary of the global state."""
    partes = []
    for nombre in ("deepseek", "telegram", "linkedin", "data_escribible", "chromium"):
        bloque = (resultado or {}).get(nombre) or {}
        if bloque.get("omitido"):
            estado = "omitido"
        else:
            estado = "ok" if bloque.get("ok") else "FALLO"
        partes.append("%s=%s" % (nombre, estado))
    return ("global=%s " % ("ok" if (resultado or {}).get("ok") else "degradado")) + " ".join(partes)


if __name__ == "__main__":
    try:
        from config import load_config
    except ImportError:  # run as ``python -m service.health``
        from service.config import load_config  # type: ignore[no-redef]

    try:
        # estricto=False: a diagnostic must work with a half-filled config.
        config_actual = load_config(estricto=False)
    except Exception as err:  # noqa: BLE001
        print("[health] no se pudo cargar la configuración: %s" % err, file=sys.stderr)
        config_actual = {}

    diagnostico_actual = diagnostico(config_actual)
    print(json.dumps(diagnostico_actual, ensure_ascii=False, indent=2, sort_keys=True))
    # Exit code useful for `docker healthcheck`/CI.
    sys.exit(0 if diagnostico_actual.get("ok") else 1)
