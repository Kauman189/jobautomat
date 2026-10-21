"""rate.py — Search and score offers, then print a fit table for review.

Usage:
    uv run --with patchright --with requests python rate.py

Does not touch Telegram or generate materials: it only searches LinkedIn,
scores against perfil.json with the scoring.py rubric and prints the top
results to the console.
"""

import sys

import main as M  # reuses cargar_perfil, _normalizar, _fusionar_detalles, _generar_id
import scoring
from config import load_config
from search import get_job_details, search_jobs

# Results per query and rows shown at the end.
LIMITE = 20
TOP = 12


def main():
    config = load_config()
    perfil = M.cargar_perfil(config)

    ofertas = search_jobs(M.QUERIES, limit=LIMITE)

    vistas = {}
    for bruto in ofertas or []:
        if not isinstance(bruto, dict):
            continue
        try:
            cid = M._generar_id(bruto)
        except Exception:  # noqa: BLE001
            cid = bruto.get("job_id") or bruto.get("id") or str(bruto)
        if cid in vistas:
            continue
        vistas[cid] = bruto

    print(f"Ofertas únicas tras dedup: {len(vistas)}")

    resultados = []
    for _cid, bruto in vistas.items():
        op = M._normalizar(bruto)
        job_id = bruto.get("id") or bruto.get("job_id") or op["id"]
        try:
            det = get_job_details(job_id)
            M._fusionar_detalles(op, det)
        except Exception as err:  # noqa: BLE001
            print("sin detalle {}: {}".format(op["id"], err), file=sys.stderr)

        r = scoring.score(op, perfil)
        op["score"] = r["score"]
        op["recomendacion"] = r.get("recomendacion", "")
        op["motivos"] = r.get("motivos", [])
        op["gaps"] = r.get("gaps", [])
        op["red_flags"] = r.get("red_flags", [])
        resultados.append(op)

    resultados.sort(key=lambda o: o.get("score", 0), reverse=True)

    for o in resultados[:TOP]:
        print("")
        print("=" * 72)
        print(
            f"{o.get('score')}% [{o.get('recomendacion') or '?'}] {o.get('titulo')} @ "
            f"{o.get('empresa')}"
        )
        meta = []
        if o.get("remoto"):
            meta.append(o["remoto"])
        if o.get("salario"):
            meta.append(o["salario"])
        if o.get("ubicacion"):
            meta.append(o["ubicacion"])
        if meta:
            print("  {}".format(" · ".join(meta)))
        for m in (o.get("motivos") or [])[:2]:
            print(f"  + {m}")
        for g in (o.get("gaps") or [])[:2]:
            print(f"  - {g}")
        for f in (o.get("red_flags") or [])[:3]:
            print(f"  ! {f}")
        if o.get("url"):
            print("  {}".format(o["url"]))

    print("")
    print(f"TOTAL evaluadas: {len(resultados)} · mostradas: {min(TOP, len(resultados))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
