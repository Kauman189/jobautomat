"""apply.py — Queueing of approved applications.

Phase 1 (current): the service never applies automatically. ``encolar(op)``
only marks the opportunity as ``aprobada`` in the database; sending is left
to the user.

Phase 2 (not implemented): submitting the application to LinkedIn (Easy
Apply or external form) via browser automation with the generated CV and
cover letter. Any future implementation must respect LinkedIn's terms of use
and keep prior human confirmation.

Hence this module has no HTTP calls and no automation: just the state change
and the log record.
"""

import logging

import db

ESTADO_APROBADA = "aprobada"

log = logging.getLogger("jobautomat.apply")


def encolar(op):
    """Mark opportunity ``op`` as approved (phase 1: no real submission).

    Args:
        op: dict with at least the ``id`` key.

    Returns ``None``. Logs when the opportunity did not exist.
    """
    if not op or not op.get("id"):
        raise ValueError("encolar() requiere una oportunidad con 'id'.")

    actualizada = db.set_estado(op["id"], ESTADO_APROBADA)
    if actualizada:
        log.info("Oportunidad %s marcada como aprobada (fase 1, sin envío).", op["id"])
    else:
        log.warning("No se encontró la oportunidad %s en la base.", op["id"])
    return None
