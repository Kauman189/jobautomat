# scripts/ — CLI tools

## tracker.py

Pure standard-library Python CLI (no external dependencies) to manage the
opportunity tracker in [`data/oportunidades.json`](../data/oportunidades.json).

- Honors the `schema` block of the JSON (does not modify it).
- Validates the **status** against the allowed list read from `schema.estado`.
- Requires Python 3.9+ (uses `list[str]`, `dict | None`).

### Requirements

Nothing beyond standard Python 3. Run from the project root:

```bash
python3 scripts/tracker.py --help
```

### Commands

| Command | Description |
|---|---|
| `add` | Creates a new opportunity from the values passed as flags. |
| `list [--estado X]` | Lists opportunities, with an optional status filter. |
| `set-estado <id> <estado>` | Changes the status (validated against the schema). |
| `score <id> <0-100>` | Sets the compatibility score. |
| `nota <id> <texto>` | Sets the `notas` field (replaces the previous value). |
| `export [--out CSV]` | Dumps every opportunity to CSV (stdout by default). |

### Valid statuses

Enum values are Spanish on purpose (they are data, not prose — do not
translate them):

`descubierta` · `filtrada` · `compatible` · `priorizada` · `cv_adaptado` ·
`cover_lista` · `aprobacion_pendiente` · `enviada` · `respuesta` · `entrevista` ·
`oferta` · `descartada`

Sources: `linkedin` · `indeed` · `web`. Contracts: `laboral` · `b2b` · `desconocido`.

### Examples

```bash
# Create an opportunity
python3 scripts/tracker.py add \
  --fuente linkedin \
  --titulo "Product Designer" \
  --empresa "Acme" \
  --url "https://www.linkedin.com/jobs/view/123" \
  --remoto true \
  --salario "no publicado" \
  --contrato laboral \
  --estado descubierta

# List (all or filtered)
python3 scripts/tracker.py list
python3 scripts/tracker.py list --estado priorizada

# Change status and score
python3 scripts/tracker.py set-estado <id> entrevista
python3 scripts/tracker.py score <id> 72

# Add a note
python3 scripts/tracker.py nota <id> "confirm remote setup before applying"

# Export to CSV
python3 scripts/tracker.py export --out data/oportunidades.csv
```

### Design notes

- If `add` does not receive `--id`, it generates one from the hash of `--url`;
  if there is no URL either, it uses a timestamped id. Duplicate ids are
  rejected.
- `score` validates the 0-100 range in both `add` and `score`.
- `nota` replaces the previous content of `notas`; it does not concatenate.
- `export` writes `skills_match` as JSON and `remoto` as `sí/no/-`; if you use
  `--out`, the summary is printed to stderr so the CSV stays clean.
- Valid statuses, sources and contracts are read from the JSON `schema` block,
  so the script stays in sync with the schema without code changes.
