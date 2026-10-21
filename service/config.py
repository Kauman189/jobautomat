"""config.py — Service configuration loading.

Reads ``service/.env`` (``KEY=VALUE``) with a minimal hand-rolled parser: no
external dependencies, stdlib only, Python 3.9+.

On top of the ``.env`` there is a web-editable layer: ``data/settings.json``
(path in ``SETTINGS_PATH``). That JSON **wins** over the environment so
thresholds, roles or sources can change without restarting the container. It
is written atomically (``tempfile`` + ``os.replace``) with 0600 permissions
because it may hold secrets.
"""

import copy
import json
import os
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Default path of the editable JSON (relative to service/).
DEFAULT_SETTINGS_PATH = "../data/settings.json"

# Keys read from .env: (conversion type, default value). ``json`` accepts
# lists and objects (serialized/parsed as JSON).
_CONFIG_DEFS = {
    # (type, default)
    "DEEPSEEK_API_KEY": ("str", None),
    "DEEPSEEK_BASE_URL": ("str", "https://api.deepseek.com/v1"),
    "DEEPSEEK_MODEL": ("str", "deepseek-flash"),
    "DEEPSEEK_MODEL_PRO": ("str", "deepseek-v4-pro"),
    "TELEGRAM_BOT_TOKEN": ("str", None),
    "TELEGRAM_CHAT_ID": ("str", None),
    "WEB_PASSWORD": ("str", None),
    "SCORE_THRESHOLD": ("int", "80"),
    "DB_PATH": ("str", "state.db"),
    "PERFIL_PATH": ("str", "../data/perfil.json"),
    # --- editable layer (data/settings.json) ---
    "SETTINGS_PATH": ("str", DEFAULT_SETTINGS_PATH),
    "TOPICS": ("json", []),
    "ROLES": ("json", ["Product Designer", "UI-UX Designer", "Product Design"]),
    "KEYWORDS_NEGATIVAS": ("json", []),
    "UBICACIONES": ("json", []),
    "MODALIDAD": ("str", "remoto"),
    "HORA_BUSQUEDA": ("str", "02:00"),
    "DIAS_BUSQUEDA": ("json", []),
    "FUENTES_LINKEDIN": ("bool", True),
    "FUENTES_BOARDS": ("bool", True),
    "FUENTES_JOBSPY": ("bool", True),
    "TEMPERATURA": ("float", 0.4),
    # Language of descriptive scoring texts: "en" (default) or "es".
    # 'recomendacion' values are never translated (aplicar/dudoso/descartar).
    "IDIOMA_SCORING": ("str", "en"),
    "RANGO_DUDOSO_MIN": ("int", 55),
    "RANGO_DUDOSO_MAX": ("int", 79),
    "MAX_ANTIGUEDAD_DIAS": ("int", 30),
    "LINKEDIN_COOLDOWN_SEGUNDOS": ("int", 21600),
    # --- notifications and self-hosting ---
    # Telegram can be disabled entirely (webhook / panel only).
    "TELEGRAM_HABILITADO": ("bool", True),
    # Generic webhook (ntfy, Discord, Slack...). Empty = no webhook.
    "WEBHOOK_URL": ("str", ""),
    # Alert when the LinkedIn session (cookies) is about to expire.
    "ALERTAR_SESION_LINKEDIN": ("bool", True),
    "SESION_LINKEDIN_MAX_DIAS": ("int", 14),
}

# Mandatory keys: a clear error is raised if missing or empty after loading.
# TELEGRAM_BOT_TOKEN is not required here: with TELEGRAM_HABILITADO=False the
# service runs on webhook/panel, and if enabled but missing the token it only
# warns (see ``validar_minimo``).
_REQUIRED_KEYS = ("DEEPSEEK_API_KEY",)

# Keys never returned in clear by the panel API.
_SECRET_KEYS = ("DEEPSEEK_API_KEY", "TELEGRAM_BOT_TOKEN", "WEB_PASSWORD")

# Ranges enforced when saving from the panel: key -> (min, max).
_RANGOS = {
    "SCORE_THRESHOLD": (0, 100),
    "RANGO_DUDOSO_MIN": (0, 100),
    "RANGO_DUDOSO_MAX": (0, 100),
    "TEMPERATURA": (0.0, 2.0),
    "MAX_ANTIGUEDAD_DIAS": (1, 365),
    "LINKEDIN_COOLDOWN_SEGUNDOS": (0, 604800),
    "SESION_LINKEDIN_MAX_DIAS": (1, 365),
}

# Public aliases for the web layer (validation and key listing).
CONFIG_SPEC = _CONFIG_DEFS
SECRET_KEYS = _SECRET_KEYS

_TRUE = ("true", "1", "yes", "y", "si", "sí", "on", "verdadero")
_FALSE = ("false", "0", "no", "n", "off", "falso", "")


def _warn(mensaje):
    """Warning to stderr. Never receives config values (keys only)."""
    print("[config] AVISO: %s" % mensaje, file=sys.stderr)


def _a_bool(valor):
    """Convert to bool, accepting bools, numbers and the usual strings."""
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, (int, float)):
        return bool(valor)
    texto = str(valor).strip().lower()
    if texto in _TRUE:
        return True
    if texto in _FALSE:
        return False
    raise ValueError("valor booleano no reconocido: %r" % (valor,))


def _convertir(valor, tipo):
    """Convert a raw value to ``tipo``.

    Accepts text (from ``.env``) or already typed values (from
    ``settings.json``). For ``int``/``float`` an empty or non-numeric value
    propagates ``ValueError`` so malformed config is not silently masked.
    """
    if tipo == "json":
        if isinstance(valor, (list, dict)):
            return copy.deepcopy(valor)
        return json.loads(str(valor).strip())
    if tipo == "bool":
        return _a_bool(valor)
    if tipo == "int":
        texto = str(valor).strip()
        numero = float(texto)
        if numero != int(numero):
            raise ValueError("se esperaba un entero: %r" % (valor,))
        return int(numero)
    if tipo == "float":
        return float(str(valor).strip())
    if valor is None:
        return None
    return str(valor).strip()


def _parsear_env(ruta):
    """Parse a ``KEY=VALUE`` file into a dict ({} if the file is missing).

    Blank lines and ``#`` comments are ignored, whitespace is stripped and
    wrapping single/double quotes are removed.
    """
    if not os.path.isfile(ruta):
        return {}

    datos = {}
    with open(ruta, "r", encoding="utf-8") as fh:
        for linea in fh:
            linea = linea.strip()
            if not linea or linea.startswith("#"):
                continue
            if "=" not in linea:
                continue
            clave, valor = linea.split("=", 1)
            clave = clave.strip()
            valor = valor.strip()
            # Strip wrapping quotes.
            if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in ("'", '"'):
                valor = valor[1:-1]
            datos[clave] = valor
    return datos


def ruta_settings(config=None):
    """Absolute path of the effective ``settings.json``.

    Precedence: ``SETTINGS_PATH`` env var (tests, Docker) > ``SETTINGS_PATH``
    from .env/config > default. Relative paths resolve against ``service/``.
    """
    ruta = os.environ.get("SETTINGS_PATH") or (config or {}).get("SETTINGS_PATH")
    if not ruta:
        ruta = DEFAULT_SETTINGS_PATH
    ruta = str(ruta).strip()
    if not os.path.isabs(ruta):
        ruta = os.path.normpath(os.path.join(BASE_DIR, ruta))
    return ruta


def leer_settings(ruta=None):
    """Read ``settings.json`` and return its dict ({} if missing or corrupt).

    A missing file is normal on first run and does not warn. A corrupt
    non-object JSON is ignored with a warning and the env config is used.
    """
    ruta = ruta or ruta_settings()
    if not os.path.isfile(ruta):
        return {}
    try:
        with open(ruta, "r", encoding="utf-8") as fh:
            datos = json.load(fh)
    except Exception as err:  # noqa: BLE001 — corrupt file must not crash the service
        _warn("no se pudo leer %s (%s); se usa el entorno" % (ruta, err))
        return {}
    if not isinstance(datos, dict):
        _warn("%s no contiene un objeto JSON; se ignora" % ruta)
        return {}
    return datos


def guardar_settings(cambios, ruta=None):
    """Merge ``cambios`` into the existing JSON and write it atomically.

    Writes to a temp file in the same directory, fsyncs and ``os.replace``
    (atomic on the same filesystem), leaving 0600 permissions because the
    file may hold secrets. Returns the persisted dict.
    """
    ruta = ruta or ruta_settings()
    actuales = leer_settings(ruta)
    actuales.update(cambios or {})

    directorio = os.path.dirname(ruta) or "."
    os.makedirs(directorio, exist_ok=True)

    fd, temporal = tempfile.mkstemp(
        dir=directorio, prefix=".settings-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(actuales, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(temporal, 0o600)
        except OSError:
            pass  # filesystem without POSIX permissions
        os.replace(temporal, ruta)
    except Exception:
        try:
            os.remove(temporal)
        except OSError:
            pass
        raise
    return actuales


def validar_minimo(cfg):
    """Check the minimum configuration the service needs to operate.

    Returns a list of non-blocking **warnings** for problems that still allow
    a degraded start. The only blocking case is ``DEEPSEEK_API_KEY``: without
    it nothing can be scored or generated, so ``RuntimeError`` is raised.

    Used at startup (``load_config(estricto=True)``); the panel uses
    ``estricto=False`` and never reaches it from a GET.
    """
    cfg = cfg or {}
    avisos = []

    if not str(cfg.get("DEEPSEEK_API_KEY") or "").strip():
        raise RuntimeError(
            "Falta DEEPSEEK_API_KEY: sin él no se puede puntuar ni generar. "
            "Cópialo en service/.env o data/settings.json"
        )

    if cfg.get("TELEGRAM_HABILITADO") and not str(cfg.get("TELEGRAM_BOT_TOKEN") or "").strip():
        avisos.append(
            "TELEGRAM_HABILITADO=true pero falta TELEGRAM_BOT_TOKEN: "
            "no se enviarán avisos por Telegram (define el token o pon "
            "TELEGRAM_HABILITADO=false)."
        )

    return avisos


def load_config(ruta_env=None, estricto=True):
    """Load and validate the service configuration.

    Returns a dict with every key in ``_CONFIG_DEFS``, converted and
    defaulted, then overlaid with ``data/settings.json`` (which wins over the
    environment). With ``estricto`` it runs the startup fail-fast
    (``validar_minimo``), raising ``RuntimeError`` if ``DEEPSEEK_API_KEY`` is
    missing; the panel passes ``estricto=False`` to show a half-filled config.

    Args:
        ruta_env: optional .env path; defaults to ``service/.env``.
        estricto: if True (default) requires the mandatory keys.
    """
    if ruta_env is None:
        # .env lives next to this file (service/).
        ruta_env = os.path.join(BASE_DIR, ".env")

    datos_crudos = _parsear_env(ruta_env)

    config = {}
    for clave, (tipo, por_defecto) in _CONFIG_DEFS.items():
        if clave in datos_crudos and datos_crudos[clave] != "":
            config[clave] = _convertir(datos_crudos[clave], tipo)
        elif por_defecto is not None:
            config[clave] = _convertir(por_defecto, tipo)
        else:
            config[clave] = None

    # Editable layer: settings.json wins over the environment.
    ruta_json = ruta_settings(config)
    config["SETTINGS_PATH"] = ruta_json
    for clave, valor in leer_settings(ruta_json).items():
        if clave == "SETTINGS_PATH":
            continue  # the JSON must not redirect its own location
        if clave.startswith("_"):
            continue  # comment convention in settings.json
        if clave not in _CONFIG_DEFS:
            _warn("clave desconocida en settings.json ignorada: %s" % clave)
            continue
        tipo = _CONFIG_DEFS[clave][0]
        try:
            config[clave] = _convertir(valor, tipo)
        except Exception as err:  # noqa: BLE001 — one bad key does not invalidate the rest
            _warn("valor inválido para %s en settings.json (%s); se conserva el del entorno"
                  % (clave, err))

    if estricto:
        # Startup fail-fast: validate the minimum (RuntimeError if
        # DEEPSEEK_API_KEY is missing) and warn about non-blocking issues.
        # With estricto=False (panel GET) nothing is validated so a
        # half-filled config can still be displayed.
        avisos = validar_minimo(config)
        for aviso in avisos:
            _warn(aviso)
        # Remaining required keys from the spec, for future additions.
        faltantes = [c for c in _REQUIRED_KEYS if not config.get(c)]
        if faltantes:
            raise RuntimeError(
                "Configuración incompleta: faltan claves obligatorias en el .env: "
                + ", ".join(faltantes)
                + f" (archivo revisado: {ruta_env})"
            )

    return config


def config_por_defecto():
    """Config with only the spec defaults (no disk access)."""
    config = {}
    for clave, (tipo, por_defecto) in _CONFIG_DEFS.items():
        config[clave] = _convertir(por_defecto, tipo) if por_defecto is not None else None
    return config


def es_secreta(clave):
    """True if the key must never be returned in clear by the API."""
    return clave in _SECRET_KEYS


def enmascarar_config(config):
    """Serializable copy of ``config`` with secrets replaced by ""."""
    visible = {}
    for clave, valor in (config or {}).items():
        if clave in _SECRET_KEYS:
            visible[clave] = ""
        elif isinstance(valor, (list, dict)):
            visible[clave] = copy.deepcopy(valor)
        else:
            visible[clave] = valor
    return visible


def secretos_configurados(config):
    """``{secret_key: bool}`` telling whether each one has a non-empty value."""
    return {
        clave: bool((config or {}).get(clave))
        for clave in _SECRET_KEYS
    }


def normalizar_valor(clave, valor):
    """Validate ``valor``'s type for ``clave`` and return it normalized.

    Strict on purpose (panel PUT): an int is not accepted as text. Raises
    ``KeyError`` for unknown keys and ``ValueError`` on type mismatch.
    """
    if clave not in _CONFIG_DEFS:
        raise KeyError(clave)
    tipo = _CONFIG_DEFS[clave][0]

    if tipo == "bool":
        if not isinstance(valor, bool):
            raise ValueError("%s debe ser booleano (true/false)" % clave)
        return valor
    if tipo == "int":
        if isinstance(valor, bool) or not isinstance(valor, int):
            raise ValueError("%s debe ser un número entero" % clave)
        return valor
    if tipo == "float":
        if isinstance(valor, bool) or not isinstance(valor, (int, float)):
            raise ValueError("%s debe ser un número" % clave)
        return float(valor)
    if tipo == "json":
        if not isinstance(valor, (list, dict)):
            raise ValueError("%s debe ser una lista u objeto JSON" % clave)
        return copy.deepcopy(valor)
    # str
    if not isinstance(valor, str):
        raise ValueError("%s debe ser texto" % clave)
    return valor.strip()


def validar_rangos(valores):
    """Check numeric ranges and MIN<=MAX coherence on the final state.

    ``valores`` must be the resulting configuration (current settings plus
    changes), not just the patch, so a partial PUT does not raise false
    errors.
    """
    for clave, (minimo, maximo) in _RANGOS.items():
        if clave not in valores or valores[clave] is None:
            continue
        valor = valores[clave]
        if valor < minimo or valor > maximo:
            raise ValueError(
                "%s debe estar entre %s y %s (recibido %s)"
                % (clave, minimo, maximo, valor)
            )

    minimo = valores.get("RANGO_DUDOSO_MIN")
    maximo = valores.get("RANGO_DUDOSO_MAX")
    if minimo is not None and maximo is not None and minimo > maximo:
        raise ValueError(
            "RANGO_DUDOSO_MIN (%s) no puede ser mayor que RANGO_DUDOSO_MAX (%s)"
            % (minimo, maximo)
        )


if __name__ == "__main__":
    # Minimal self-check: prints key NAMES only (never values, no secret leak).
    cfg = load_config()
    print("Claves cargadas:", sorted(cfg.keys()))
