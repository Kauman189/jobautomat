"""db.py — Opportunity persistence in SQLite (stdlib ``sqlite3``).

Defines the ``oportunidades`` table plus the service's usual access helpers:
init the database, check whether an opportunity is new, insert, update state
and list. No external dependencies.
"""

import json
import sqlite3

# Table columns and their order (used to build INSERT and SELECT).
_COLUMNAS = [
    "id",
    "url",
    "titulo",
    "empresa",
    "salario",
    "ubicacion",
    "remoto",
    "contrato",
    "descripcion",
    "estado",
    "score",
    "justificacion",
    "cover_letter",
    "cv_adaptado",
    "mensaje",
    "created_at",
    "updated_at",
]

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS oportunidades (
    id           TEXT PRIMARY KEY,
    url          TEXT,
    titulo       TEXT,
    empresa      TEXT,
    salario      TEXT,
    ubicacion    TEXT,
    remoto       TEXT,
    contrato     TEXT,
    descripcion  TEXT,
    estado       TEXT,
    score        INTEGER,
    justificacion TEXT,
    cover_letter TEXT,
    cv_adaptado  TEXT,
    mensaje      TEXT,
    created_at   TEXT,
    updated_at   TEXT
)
"""

# Score cache: the same offer (same stable id computed in scoring.py) must not
# be re-scored between runs. LLM scores are not deterministic, so keeping the
# first one avoids 65->55->60 drift and saves tokens. ``dimensiones`` holds a
# JSON text (five-grade rubric).
_ESQUEMA_CACHE = """
CREATE TABLE IF NOT EXISTS score_cache (
    id          TEXT PRIMARY KEY,
    score       INTEGER,
    dimensiones TEXT,
    creado_en   TEXT
)
"""


def _resolver_path(path):
    """Return the effective database path.

    With ``path`` None, ``DB_PATH`` from config (.env) is used, resolved
    relative to ``service/`` so ``state.db`` does not depend on the cwd. The
    ``config`` import is deferred to avoid import cycles.
    """
    if path:
        return path
    import os

    import config as config_mod

    db_path = config_mod.load_config().get("DB_PATH") or "state.db"
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), db_path)
    return db_path


def init_db(path=None):
    """Create the ``oportunidades`` table in ``path`` if missing.

    ``path`` is optional (defaults to ``DB_PATH``). Returns the path used so
    the caller can reuse it.
    """
    path = _resolver_path(path)
    conn = sqlite3.connect(path, timeout=30)
    try:
        # WAL allows concurrent reads and writes (parallel scoring threads).
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(_ESQUEMA)
        conn.execute(_ESQUEMA_CACHE)
        # Migration: databases created before the description column existed.
        try:
            conn.execute("ALTER TABLE oportunidades ADD COLUMN descripcion TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()
    finally:
        conn.close()
    return path


def _conectar(path):
    """Open a connection with ``row_factory=sqlite3.Row`` for dict-like rows.

    ``timeout`` and ``busy_timeout`` allow concurrent writes (parallel
    scoring): wait up to 30 s instead of failing with ``database is locked``.
    """
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    # Create the table if it does not exist yet (e.g. first panel/bot start
    # before the scheduler ran). IF NOT EXISTS is cheap.
    conn.execute(_ESQUEMA)
    # Same for the score cache (same idempotent migration).
    conn.execute(_ESQUEMA_CACHE)
    # Idempotent migrations for databases created before these columns.
    for _col in ("descripcion", "justificacion"):
        try:
            conn.execute(f"ALTER TABLE oportunidades ADD COLUMN {_col} TEXT")
        except sqlite3.OperationalError:
            pass
    conn.row_factory = sqlite3.Row
    return conn


def _fila_a_dict(fila):
    """Turn a sqlite3.Row into a plain dict."""
    return dict(fila) if fila is not None else None


def is_nueva(o, path=None):
    """True if opportunity ``o`` does not exist yet by its ``id``.

    ``o`` must be a dict containing ``id``. ``path`` is optional.
    """
    path = _resolver_path(path)
    conn = _conectar(path)
    try:
        fila = conn.execute("SELECT 1 FROM oportunidades WHERE id = ?", (o["id"],)).fetchone()
        return fila is None
    finally:
        conn.close()


def insertar(o, path=None):
    """Insert opportunity ``o`` (dict) into the database.

    An existing ``id`` is ignored (INSERT OR IGNORE) to avoid duplicates.
    Returns True if inserted, False if it already existed. ``path`` is
    optional.
    """
    path = _resolver_path(path)
    # Fill missing columns with None for a complete, stable INSERT.
    valores = [o.get(col) for col in _COLUMNAS]
    marcadores = ", ".join("?" for _ in _COLUMNAS)
    sql = f"INSERT OR IGNORE INTO oportunidades ({', '.join(_COLUMNAS)}) VALUES ({marcadores})"

    conn = _conectar(path)
    try:
        cur = conn.execute(sql, valores)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_estado(id_, estado, path=None):
    """Update ``estado`` and ``updated_at`` for opportunity ``id_``.

    Returns True if a row was modified, False if it did not exist. ``path``
    is optional.
    """
    import datetime

    path = _resolver_path(path)
    ahora = datetime.datetime.now(datetime.UTC).isoformat()
    conn = _conectar(path)
    try:
        cur = conn.execute(
            "UPDATE oportunidades SET estado = ?, updated_at = ? WHERE id = ?",
            (estado, ahora, id_),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def listar(estado=None, path=None):
    """Return the opportunities as a list of dicts.

    Filters by ``estado`` when given. Ordered by ``created_at`` descending
    (newest first). ``path`` is optional (defaults to ``DB_PATH``).
    """
    path = _resolver_path(path)
    conn = _conectar(path)
    try:
        if estado is not None:
            filas = conn.execute(
                "SELECT * FROM oportunidades WHERE estado = ? ORDER BY created_at DESC",
                (estado,),
            ).fetchall()
        else:
            filas = conn.execute("SELECT * FROM oportunidades ORDER BY created_at DESC").fetchall()
        return [_fila_a_dict(f) for f in filas]
    finally:
        conn.close()


def get(id_, path=None):
    """Return opportunity ``id_`` as a dict, or None if it does not exist.

    Used to recover the full offer (description included) when the user
    approves from Telegram and materials must be generated.
    """
    path = _resolver_path(path)
    conn = _conectar(path)
    try:
        fila = conn.execute("SELECT * FROM oportunidades WHERE id = ?", (id_,)).fetchone()
        return _fila_a_dict(fila)
    finally:
        conn.close()


def guardar_materiales(id_, cover_letter, cv_adaptado, mensaje, path=None):
    """Persist the generated materials for opportunity ``id_``.

    Returns True if the row was updated, False if it did not exist. Also
    bumps ``updated_at``.
    """
    import datetime

    path = _resolver_path(path)
    ahora = datetime.datetime.now(datetime.UTC).isoformat()
    conn = _conectar(path)
    try:
        cur = conn.execute(
            "UPDATE oportunidades SET cover_letter = ?, cv_adaptado = ?, "
            "mensaje = ?, updated_at = ? WHERE id = ?",
            (cover_letter, cv_adaptado, mensaje, ahora, id_),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def limpiar_busquedas(path=None):
    """Delete every search result, keeping the approved offers.

    Removes the ``oportunidades`` rows still in the search flow
    (``para_confirmar`` and ``descartada``) and empties the whole
    ``score_cache`` table, so the next run scores every offer again.
    ``aprobada`` rows are left untouched, materials included.

    Args:
        path: Optional database path (defaults to ``DB_PATH``).

    Returns:
        The number of ``oportunidades`` rows deleted.

    Raises:
        sqlite3.Error: If the database cannot be opened or written.
    """
    path = _resolver_path(path)
    conn = _conectar(path)
    try:
        cur = conn.execute(
            "DELETE FROM oportunidades WHERE estado IN ('para_confirmar', 'descartada')"
        )
        borradas = cur.rowcount
        conn.execute("DELETE FROM score_cache")
        conn.commit()
        return borradas
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Score cache (``score_cache`` table)
# ---------------------------------------------------------------------------


def get_score_cache(id_, path=None):
    """Return the cached score for ``id_``, or None if absent.

    ``id_`` is the stable identifier computed in ``scoring.py`` from
    normalized title+company+location. The int is returned as-is so the
    caller can do ``if score is not None`` and skip re-scoring. ``path`` is
    optional (defaults to ``DB_PATH``).
    """
    detalle = get_score_cache_detalle(id_, path)
    return detalle["score"] if detalle else None


def get_score_cache_detalle(id_, path=None):
    """Return ``{"score", "dimensiones", "creado_en"}`` or None if absent.

    ``dimensiones`` is deserialized from the stored JSON; corrupt JSON yields
    ``None`` in that field instead of raising, so the caller can re-score.
    """
    path = _resolver_path(path)
    conn = _conectar(path)
    try:
        fila = conn.execute(
            "SELECT id, score, dimensiones, creado_en FROM score_cache WHERE id = ?",
            (id_,),
        ).fetchone()
        if fila is None:
            return None
        try:
            dimensiones = json.loads(fila["dimensiones"]) if fila["dimensiones"] else None
        except (TypeError, ValueError):
            dimensiones = None
        return {
            "id": fila["id"],
            "score": fila["score"],
            "dimensiones": dimensiones,
            "creado_en": fila["creado_en"],
        }
    finally:
        conn.close()


def set_score_cache(id_, score, dimensiones, path=None):
    """Store (or overwrite) the score for ``id_`` in ``score_cache``.

    Idempotent: ``ON CONFLICT(id) DO UPDATE`` updates the row instead of
    duplicating it. ``dimensiones`` is serialized to JSON; a non-serializable
    value is stored as NULL instead of failing. Returns True when written.
    """
    import datetime

    path = _resolver_path(path)
    try:
        texto_dim = json.dumps(dimensiones, ensure_ascii=False)
    except (TypeError, ValueError):
        texto_dim = None
    try:
        score_int = int(round(float(score)))
    except (TypeError, ValueError):
        score_int = None
    ahora = datetime.datetime.now(datetime.UTC).isoformat()

    conn = _conectar(path)
    try:
        conn.execute(
            "INSERT INTO score_cache (id, score, dimensiones, creado_en) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "score = excluded.score, dimensiones = excluded.dimensiones, "
            "creado_en = excluded.creado_en",
            (id_, score_int, texto_dim, ahora),
        )
        conn.commit()
        return True
    finally:
        conn.close()
