"""i18n.py — Log-language switch.

Framework log messages are written through ``t(es, en)``: Spanish by default
and English when ``IDIOMA_LOGS=en`` (env) or ``i18n.configurar_idioma("en")``
is called at the start of a run.

Only **log** strings go through here. Texts the user reads (Telegram messages,
prompts, panel) and the descriptive scoring texts (``IDIOMA_SCORING``) are not
translated by this module.
"""

import os

_IDIOMA = (os.environ.get("IDIOMA_LOGS") or "es").strip().lower()[:2]
if _IDIOMA not in ("en", "es"):
    _IDIOMA = "es"


def configurar_idioma(idioma):
    """Set the log language ("en" or "es"); anything else is ignored."""
    global _IDIOMA
    if idioma in ("en", "es"):
        _IDIOMA = idioma


def t(es, en):
    """Return ``en`` when the log language is English, ``es`` otherwise."""
    return en if _IDIOMA == "en" else es
