"""search/__init__.py — Job search.

Three independent sources with the same output contract:

  * ``linkedin`` (patchright, with session): ``search_jobs`` + ``get_job_details``.
  * ``boards`` (public ATSs and remote portals, no auth): the aggregator
    ``buscar_ofertas_boards``.
  * ``jobspy`` (Indeed/ZipRecruiter/Glassdoor via ``python-jobspy``, no browser
    and no session): the aggregator ``buscar_ofertas_jobspy``.

Typical usage from ``main.py``::

    from search import (
        LinkedInError,
        buscar_ofertas_boards,
        buscar_ofertas_jobspy,
        search_jobs,
    )

    ofertas = search_jobs(["Software Engineer remote"], limit=20)
    ofertas += buscar_ofertas_boards(limite_por_fuente=100)
    ofertas += buscar_ofertas_jobspy(QUERIES, limite=10, horas_recientes=72)

``search_jobs`` returns dicts with ``job_id``, ``titulo``, ``empresa`` and
``url``; ``get_job_details`` adds ``ubicacion``, ``descripcion``,
``requisitos``, ``salario``, ``remoto`` and ``contrato``.
``buscar_ofertas_boards`` and ``buscar_ofertas_jobspy`` already return the full
contract (``job_id``, ``titulo``, ``empresa``, ``url``, ``ubicacion``,
``remoto``, ``salario``, ``contrato``, ``descripcion``) plus ``source`` with the
origin source. Unknown fields stay ``None``. ``close()`` shuts down the shared
LinkedIn browser at the end of the run.

Both aggregators isolate their failures: if a source or a site goes down, it is
logged and whatever was obtained is returned (they never raise).
"""

from .boards import buscar_ofertas_boards
from .jobspy import buscar_ofertas_jobspy
from .linkedin import LinkedInError, close, get_job_details, search_jobs

__all__ = [
    "search_jobs",
    "get_job_details",
    "LinkedInError",
    "close",
    "buscar_ofertas_boards",
    "buscar_ofertas_jobspy",
]
