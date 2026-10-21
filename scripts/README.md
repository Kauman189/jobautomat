# scripts/ — Herramientas CLI

## tracker.py

CLI en Python estándar (sin dependencias externas) para gestionar el tracker de
oportunidades en [`data/oportunidades.json`](../data/oportunidades.json).

- Respeta el bloque `schema` del JSON (no lo modifica).
- Valida el **estado** contra la lista permitida que se lee de `schema.estado`.
- Requiere Python 3.9+ (usa `list[str]`, `dict | None`).

### Requisitos

Ninguno más allá de Python 3 estándar. Ejecutar desde la raíz del proyecto:

```bash
python3 scripts/tracker.py --help
```

### Comandos

| Comando | Descripción |
|---|---|
| `add` | Crea una oportunidad nueva con datos pasados por flags. |
| `list [--estado X]` | Lista oportunidades, con filtro opcional por estado. |
| `set-estado <id> <estado>` | Cambia el estado (validado contra el schema). |
| `score <id> <0-100>` | Asigna el score de compatibilidad. |
| `nota <id> <texto>` | Fija el campo `notas` (reemplaza el valor anterior). |
| `export [--out CSV]` | Vuelca todas las oportunidades a CSV (stdout por defecto). |

### Estados válidos

`descubierta` · `filtrada` · `compatible` · `priorizada` · `cv_adaptado` ·
`cover_lista` · `aprobacion_pendiente` · `enviada` · `respuesta` · `entrevista` ·
`oferta` · `descartada`

Fuentes: `linkedin` · `indeed` · `web`. Contratos: `laboral` · `b2b` · `desconocido`.

### Ejemplos

```bash
# Crear una oportunidad
python3 scripts/tracker.py add \
  --fuente linkedin \
  --titulo "Product Designer" \
  --empresa "Acme" \
  --url "https://www.linkedin.com/jobs/view/123" \
  --remoto true \
  --salario "no publicado" \
  --contrato laboral \
  --estado descubierta

# Listar (todas o filtradas)
python3 scripts/tracker.py list
python3 scripts/tracker.py list --estado priorizada

# Cambiar estado y score
python3 scripts/tracker.py set-estado <id> entrevista
python3 scripts/tracker.py score <id> 72

# Añadir una nota
python3 scripts/tracker.py nota <id> "confirmar remoto desde España antes de aplicar"

# Exportar a CSV
python3 scripts/tracker.py export --out data/oportunidades.csv
```

### Notas de diseño

- Si `add` no recibe `--id`, genera uno a partir del hash de `--url`; si tampoco
  hay URL, usa un id con marca de tiempo. Rechaza ids duplicados.
- `score` valida el rango 0-100 en `add` y en `score`.
- `nota` reemplaza el contenido previo de `notas`; no concatena.
- `export` escribe `skills_match` como JSON y `remoto` como `sí/no/-`; si usas
  `--out`, el resumen se imprime a stderr para no ensuciar el CSV.
- Los estados, fuentes y contratos válidos se leen del bloque `schema` del JSON,
  así que el script se mantiene sincronizado con el schema sin cambios de código.
