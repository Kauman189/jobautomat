#!/usr/bin/env python3
"""CLI para gestionar el tracker de oportunidades de empleo.

Gestiona ``data/oportunidades.json`` (campo ``oportunidades`` = lista de
objetos). Respeta el bloque ``schema`` del archivo, que no se modifica, y valida
los estados contra la lista permitida que se lee de ``schema.estado``. Solo usa
la biblioteca estándar de Python (sin dependencias externas).

Comandos
--------
add <flags>
    Crea una oportunidad nueva. Flags disponibles:
        --id ID                    identificador (por defecto, hash de --url)
        --fuente {fuentes}         obligatorio: linkedin | indeed | web
        --url URL
        --titulo TITULO           obligatorio
        --empresa EMPRESA         obligatorio
        --salario TEXTO            salario publicado (opcional)
        --ubicacion TEXTO
        --remoto true|false
        --restriccion TEXTO        restricción geográfica (opcional)
        --aclaracion               marca restriccion_requiere_aclaracion=True
        --contrato {contratos}     laboral | b2b | desconocido (def: desconocido)
        --estado {estados}         (def: descubierta)
        --score 0-100              (def: 0)
        --notas TEXTO
list [--estado ESTADO]
    Lista las oportunidades; --estado filtra por estado.
set-estado <id> <estado>
    Cambia el estado de una oportunidad. El estado se valida contra el schema.
score <id> <0-100>
    Asigna el score (0-100) a una oportunidad.
nota <id> <texto>
    Fija el campo ``notas`` de una oportunidad (reemplaza el valor anterior).
export [--out CSV]
    Vuelca todas las oportunidades a CSV (stdout por defecto, o a --out).

Ejemplos
--------
    python scripts/tracker.py add --fuente linkedin --titulo "Product Designer" \
        --empresa "Acme" --url "https://..." --remoto true --estado descubierta
    python scripts/tracker.py list --estado priorizada
    python scripts/tracker.py set-estado op123abc entrevista
    python scripts/tracker.py score op123abc 75
    python scripts/tracker.py nota op123abc "pedir salario y remoto"
    python scripts/tracker.py export --out oportunidades.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = BASE_DIR / "data" / "oportunidades.json"

ESTADOS_FALLBACK = [
    "descubierta",
    "filtrada",
    "compatible",
    "priorizada",
    "cv_adaptado",
    "cover_lista",
    "aprobacion_pendiente",
    "enviada",
    "respuesta",
    "entrevista",
    "oferta",
    "descartada",
]
FUENTES_FALLBACK = ["linkedin", "indeed", "web"]
CONTRATOS_FALLBACK = ["laboral", "b2b", "desconocido"]

CAMPOS_CSV = [
    "id",
    "fuente",
    "url",
    "titulo",
    "empresa",
    "salario_publicado",
    "ubicacion",
    "remoto",
    "restriccion_geografica",
    "restriccion_requiere_aclaracion",
    "contrato",
    "estado",
    "score",
    "motivo_descarte",
    "skills_match",
    "notas",
]


# ---------- acceso a datos ----------


def cargar() -> dict:
    if not DATA_FILE.exists():
        raise SystemExit(
            f"No existe {DATA_FILE}; créalo primero con las claves 'schema' y 'oportunidades'."
        )
    with DATA_FILE.open(encoding="utf-8") as fh:
        return json.load(fh)


def guardar(data: dict) -> None:
    with DATA_FILE.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def opciones_schema(data: dict, clave: str) -> list[str]:
    """Extrae las opciones permitidas de una línea 'a | b | c' del bloque schema."""
    raw = data.get("schema", {}).get(clave, "")
    return [p.strip() for p in str(raw).split("|") if p.strip()]


def buscar(data: dict, oid: str) -> dict | None:
    for item in data.get("oportunidades", []):
        if item.get("id") == oid:
            return item
    return None


def parse_bool(valor) -> bool | None:
    if valor is None:
        return None
    v = str(valor).strip().lower()
    if v in ("true", "1", "yes", "si", "sí"):
        return True
    if v in ("false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError(f"valor booleano inválido: {valor!r} (usa true/false)")


def _remoto_str(v) -> str:
    if v is True:
        return "sí"
    if v is False:
        return "no"
    return "-"


# ---------- comandos ----------


def cmd_add(data: dict, args) -> None:
    oid = args.id
    if not oid:
        if args.url:
            oid = hashlib.sha1(args.url.encode("utf-8")).hexdigest()[:12]
        else:
            oid = "op-" + hashlib.sha1(str(time.time()).encode("utf-8")).hexdigest()[:8]
    if buscar(data, oid) is not None:
        raise SystemExit(f"Ya existe una oportunidad con id {oid!r}; usa otro --id.")
    if not (0 <= args.score <= 100):
        raise SystemExit("El score debe estar entre 0 y 100.")

    item = {
        "id": oid,
        "fuente": args.fuente,
        "url": args.url,
        "titulo": args.titulo,
        "empresa": args.empresa,
        "salario_publicado": args.salario,
        "ubicacion": args.ubicacion,
        "remoto": args.remoto,
        "restriccion_geografica": args.restriccion_geografica,
        "restriccion_requiere_aclaracion": bool(args.aclaracion),
        "contrato": args.contrato,
        "estado": args.estado,
        "score": args.score,
        "motivo_descarte": None,
        "skills_match": {},
        "notas": args.notas,
    }
    data.setdefault("oportunidades", []).append(item)
    print(f"Creada oportunidad {oid!r}: {args.titulo} @ {args.empresa} [{args.estado}]")


def cmd_list(data: dict, args) -> None:
    items = data.get("oportunidades", [])
    if args.estado:
        items = [i for i in items if i.get("estado") == args.estado]
    if not items:
        print("(sin resultados)")
        return

    encabezados = ["ID", "EMPRESA", "TITULO", "ESTADO", "SCORE", "UBICACION", "REMOTO"]
    filas: list[list[str]] = []
    for i in items:
        filas.append(
            [
                str(i.get("id", "")),
                str(i.get("empresa", "")),
                str(i.get("titulo", "")),
                str(i.get("estado", "")),
                str(i.get("score", "")),
                str(i.get("ubicacion") or ""),
                _remoto_str(i.get("remoto")),
            ]
        )
    todas = [encabezados] + filas
    anchos = [max(len(fila[c]) for fila in todas) for c in range(len(encabezados))]
    for fila in todas:
        print("  ".join(celda.ljust(anchos[idx]) for idx, celda in enumerate(fila)))


def cmd_set_estado(data: dict, args) -> None:
    item = buscar(data, args.id)
    if item is None:
        raise SystemExit(f"No existe la oportunidad {args.id!r}.")
    item["estado"] = args.estado
    print(f"{args.id} -> estado '{args.estado}'")


def cmd_score(data: dict, args) -> None:
    if not (0 <= args.score <= 100):
        raise SystemExit("El score debe estar entre 0 y 100.")
    item = buscar(data, args.id)
    if item is None:
        raise SystemExit(f"No existe la oportunidad {args.id!r}.")
    item["score"] = args.score
    print(f"{args.id} -> score {args.score}")


def cmd_nota(data: dict, args) -> None:
    item = buscar(data, args.id)
    if item is None:
        raise SystemExit(f"No existe la oportunidad {args.id!r}.")
    item["notas"] = " ".join(args.texto)
    print(f"{args.id} -> nota actualizada")


def cmd_export(data: dict, args) -> None:
    items = data.get("oportunidades", [])
    salida = sys.stdout
    cerrar = False
    if args.out:
        salida = open(args.out, "w", encoding="utf-8", newline="")
        cerrar = True
    try:
        w = csv.DictWriter(salida, fieldnames=CAMPOS_CSV)
        w.writeheader()
        for i in items:
            fila = {k: i.get(k) for k in CAMPOS_CSV}
            fila["skills_match"] = json.dumps(i.get("skills_match", {}), ensure_ascii=False)
            fila["remoto"] = _remoto_str(i.get("remoto"))
            w.writerow(fila)
    finally:
        if cerrar:
            salida.close()
    if args.out:
        print(f"Exportadas {len(items)} oportunidades a {args.out}", file=sys.stderr)


# ---------- parser ----------


def construir_parser(estados, fuentes, contratos) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tracker.py",
        description="CLI para gestionar data/oportunidades.json.",
    )
    sub = p.add_subparsers(dest="comando", required=True)

    pa = sub.add_parser("add", help="crea una oportunidad nueva")
    pa.add_argument("--id", help="identificador (por defecto, hash de --url)")
    pa.add_argument("--fuente", required=True, choices=fuentes)
    pa.add_argument("--url")
    pa.add_argument("--titulo", required=True)
    pa.add_argument("--empresa", required=True)
    pa.add_argument("--salario", help="salario publicado")
    pa.add_argument("--ubicacion")
    pa.add_argument("--remoto", type=parse_bool, default=None, metavar="true|false")
    pa.add_argument("--restriccion", dest="restriccion_geografica", help="restricción geográfica")
    pa.add_argument(
        "--aclaracion", action="store_true", help="marca restriccion_requiere_aclaracion=True"
    )
    pa.add_argument("--contrato", choices=contratos, default="desconocido")
    pa.add_argument("--estado", choices=estados, default="descubierta")
    pa.add_argument("--score", type=int, default=0, help="0-100")
    pa.add_argument("--notas", default="")

    pl = sub.add_parser("list", help="lista oportunidades (filtro opcional por estado)")
    pl.add_argument("--estado", choices=estados)

    ps = sub.add_parser("set-estado", help="cambia el estado de una oportunidad")
    ps.add_argument("id")
    ps.add_argument("estado", choices=estados)

    pc = sub.add_parser("score", help="asigna score (0-100)")
    pc.add_argument("id")
    pc.add_argument("score", type=int)

    pn = sub.add_parser("nota", help="fija la nota (campo 'notas')")
    pn.add_argument("id")
    pn.add_argument("texto", nargs="+", help="texto de la nota")

    pe = sub.add_parser("export", help="vuelca oportunidades a CSV")
    pe.add_argument("--out", help="archivo CSV de salida (por defecto, stdout)")

    return p


def main(argv=None) -> None:
    data = cargar()
    estados = opciones_schema(data, "estado") or ESTADOS_FALLBACK
    fuentes = opciones_schema(data, "fuente") or FUENTES_FALLBACK
    contratos = opciones_schema(data, "contrato") or CONTRATOS_FALLBACK
    parser = construir_parser(estados, fuentes, contratos)
    args = parser.parse_args(argv)

    if args.comando == "add":
        cmd_add(data, args)
    elif args.comando == "list":
        cmd_list(data, args)
    elif args.comando == "set-estado":
        cmd_set_estado(data, args)
    elif args.comando == "score":
        cmd_score(data, args)
    elif args.comando == "nota":
        cmd_nota(data, args)
    elif args.comando == "export":
        cmd_export(data, args)
    else:
        parser.error(f"comando desconocido: {args.comando}")

    if args.comando in ("add", "set-estado", "score", "nota"):
        guardar(data)


if __name__ == "__main__":
    main()
