# AGENTS.md

Instructions for coding agents (Cursor, Claude Code, Codex, …) working in this
repository. Read this before touching code.

## Overview

jobautomat is a self-hosted job-search assistant for Product / UI-UX designers.
A scheduled pipeline searches LinkedIn, public ATS boards and JobSpy sources,
deduplicates against SQLite, pre-filters and scores each offer against the
candidate profile with an LLM, and drafts a cover letter, a tailored CV (PDF)
and a short recruiter message for the offers that clear the threshold. The
result is reviewed in a Flask web panel or through a Telegram bot, and the
material is left on disk for the user to send. **It never submits an
application on its own** — this is a hard product constraint, not a TODO.

## Architecture

Three containers built from the same image (see `docker-compose.yml`), sharing
the `./data` volume and talking to each other **only** through `state.db` and
the JSON files on that volume. No internal API, no message bus.

| Service | Container | Entry point | Role |
| --- | --- | --- | --- |
| `app` | `jobautomat` | `docker/scheduler.sh` → `python main.py --modo cron` | Nightly pipeline (search → dedupe → filter → score → generate → notify). |
| `web` | `jobautomat-web` | `python web.py` | Flask panel + JSON API on `:8085`, HTTP Basic auth (user `casa`). |
| `bot` | `jobautomat-bot` | `python notify.py` | Telegram long-polling: summaries and Aprobar / Descartar buttons. |

Key modules (`service/`):

- `main.py` — orchestrator and scheduler entry point. `--modo cron` (one
  summary at the end, the Docker mode) or `--modo manual` (one message per
  offer in real time). Holds the `flock` that keeps two searches from
  overlapping.
- `config.py` — the only config loader: `service/.env` overlaid by
  `data/settings.json`, with `_CONFIG_DEFS` declaring type, default, secret
  flags and validation ranges.
- `scoring.py` — hard pre-filter (remote, salary, seniority) plus the 0–100
  five-dimension rubric and the `aplicar` / `dudoso` / `descartar`
  recommendation.
- `generate.py` + `pdf_gen.py` — cover letter, tailored CV and message;
  PDF rendering and the CV length gate.
- `notify.py` — Telegram summary, inline-button bot, generic `WEBHOOK_URL`.
  Approval reuses `notify._enviar_pack`.
- `web.py` — Flask panel, static files under `service/web/`, and the `/api/*`
  JSON API. Endpoints: `GET /api/ofertas?estado=`, `GET /api/oferta/<id>`,
  `POST /api/oferta/<id>/aprobar`, `POST /api/oferta/<id>/descartar`,
  `POST /api/buscar`, `GET /api/buscar/estado`, `GET /api/config`,
  `PUT /api/config`, `GET /api/perfil`, `POST /api/perfil/agregar`,
  `POST /api/perfil/borrar`, `PUT /api/perfil`, `POST /api/preguntar`,
  `GET /api/metrics`, `GET /api/salud`, `GET|POST /api/export|/api/import`,
  `GET /api/log?n=`, `POST /api/fuente/probar`, `GET /healthz`.
- `search/` — `linkedin.py` (patchright + headless Chromium),
  `boards.py` (Workable, BambooHR, Recruitee, Greenhouse, Ashby, Lever,
  Remotive, We Work Remotely), `jobspy.py` (Indeed, ZipRecruiter, Glassdoor).
  A failing source never aborts the run.
- `db.py` — SQLite access (`state.db`) and the `para_confirmar` →
  `aprobada` / `descartada` state machine.
- `health.py` — the `/api/salud` diagnostics (DeepSeek, Telegram, LinkedIn
  session, `/data` writability, Chromium).
- `mcp/server.py` — stdlib-only MCP server that exposes the panel API to
  agents (see below).

## How to run

Docker (the supported deployment):

```bash
cp service/.env.example service/.env   # fill in DEEPSEEK_API_KEY (and TELEGRAM_*)
sudo chown -R 10001:10001 ./data       # container runs as uid 10001
docker compose build
docker compose up -d
docker compose ps                      # app · web · bot
curl -sI http://localhost:8085/ | head -1
```

Panel at <http://localhost:8085> (user `casa`, password `WEB_PASSWORD`; empty
means no auth, LAN only). Force a run without waiting for the schedule:
`docker compose run --rm app once`.

Local development without Docker:

```bash
python3 -m venv service/.venv && source service/.venv/bin/activate
pip install -r service/requirements.txt
patchright install chromium
cp service/.env.example service/.env   # LINKEDIN_HEADLESS=0 to see the browser
python service/web.py                  # panel on :8085
python service/main.py --modo manual   # pipeline with per-offer output
```

A relative `DB_PATH` resolves against `service/`.

## How to test

```bash
service/.venv/bin/python -m pytest tests/ -q
```

58 tests across `tests/` (web API, config loading and validation, scoring,
dedup, health diagnostics). Tests must not hit the network, the LLM or a real
SQLite file: fixtures in `tests/conftest.py` point everything at `tmp_path`.
There is also a manual smoke test that does make one real DeepSeek call and
prints no secrets: `python3 service/smoke_test.py`.

## How to lint

There is no configured linter or formatter; the gate is compilation plus the
test suite:

```bash
python3 -m py_compile service/*.py mcp/server.py
python3 -m py_compile service/search/*.py
```

Keep lines readable (~100 columns) and follow the existing module layout:
module docstring, then constants, then helpers, then routes/handlers.

## Conventions

- **Runtime dependencies stay minimal.** `service/` uses the stdlib plus the
  pinned `service/requirements.txt` (Flask, patchright, JobSpy). Anything that
  can be done with the standard library must be — `mcp/server.py` deliberately
  implements JSON-RPC by hand instead of depending on the `mcp` SDK.
- **Code in English, output in Spanish.** Comments, docstrings, identifiers and
  log prefixes in code are English. User-facing strings stay in Spanish:
  log messages (`[web] límite alcanzado…`), panel labels and LLM prompts
  (`SYSTEM_PREGUNTAR`, `SYSTEM_PERFIL` in `web.py`), because the product is
  Spanish-speaking. Do not "translate" prompts or panel copy when refactoring.
- **Two config layers.** Secrets and host-level values live in
  `service/.env` (never committed; template at `service/.env.example`).
  Everything the user tunes lives in `data/settings.json`, which **wins** over
  the `.env`, is editable from the panel (`GET`/`PUT /api/config`) and is
  written atomically. New keys must be added to `_CONFIG_DEFS` in
  `config.py`, to `service/.env.example` and, when user-facing, to
  `data/settings.json.example`.
- **Secrets are never echoed.** `config.enmascarar_config` blanks them in API
  responses and exports; empty secret values sent to `PUT /api/config` mean
  "leave it as it is".
- **Never auto-submit.** No code path may post an application to an employer.
  `apply_form.py` only drafts answers for the user to paste.
- **Persisted state is on disk.** Anything that must survive a restart goes to
  `/data` (in Docker) via `DB_PATH`, `PERFIL_PATH`, `JOB_LOG_PATH`,
  `CANDIDATURAS_DIR`. Do not write to paths inside the image.
- **Failure isolation.** A source, one offer's generation or an ATS form must
  never abort a whole run or an approval: catch, log, append to `errores`.
- Every module carries docstrings with `Args:` / `Returns:` / `Raises:` on
  non-trivial functions. Keep that style.

## Key files map

| Path | What it is |
| --- | --- |
| `service/main.py` | Pipeline orchestrator + scheduler mode. |
| `service/config.py` | `.env` + `settings.json` loader, `_CONFIG_DEFS`, ranges. |
| `service/web.py` | Flask panel and `/api/*`; approval flow. |
| `service/web/index.html` | Single-page panel (assets and i18n next to it). |
| `service/notify.py` | Telegram bot, summaries, webhook. |
| `service/scoring.py` | Pre-filter + scoring rubric. |
| `service/generate.py`, `service/pdf_gen.py` | Written material and PDFs. |
| `service/search/{linkedin,boards,jobspy}.py` | Offer sources. |
| `service/db.py` | SQLite state and offer state machine. |
| `service/health.py` | `/api/salud` diagnostics. |
| `service/apply_form.py` | ATS form parsing and drafted answers. |
| `mcp/server.py` | MCP server (stdio) wrapping the panel API. |
| `data/perfil.json` | Candidate profile (input to scoring and writing). |
| `data/settings.json` | Editable config layer (ignored by git). |
| `docker/entrypoint.sh`, `docker/scheduler.sh` | Container bootstrap and daily loop. |
| `docs/arquitectura.md` | Block-by-block architecture notes. |

## Using the MCP server

`mcp/server.py` exposes the panel to an agent over stdio using JSON-RPC 2.0:
one JSON message per line on stdin, one JSON response per line on stdout. It
needs only the Python standard library; it talks to a **running** panel.

```json
{
  "mcpServers": {
    "jobautomat": {
      "command": "python3",
      "args": ["/abs/path/to/jobautomat/mcp/server.py"],
      "env": {
        "JOBAUTOMAT_URL": "http://localhost:8085",
        "JOBAUTOMAT_PASSWORD": "the-panel-password"
      }
    }
  }
}
```

Use the same shape for Claude Desktop (`claude_desktop_config.json`) or Cursor
(`.cursor/mcp.json`). Supported methods: `initialize`, `ping`, `tools/list`,
`tools/call`; `notifications/initialized` is accepted without a reply.

Tools: `listar_ofertas(estado)`, `detalle_oferta(id)`, `aprobar_oferta(id)`,
`descartar_oferta(id)`, `buscar_ahora()`, `leer_config()`, `leer_perfil()`,
`preguntar(texto, contexto?)`, `salud()`, `leer_log(n)`.

Environment variables: `JOBAUTOMAT_URL` (default `http://localhost:8085`),
`JOBAUTOMAT_USER` (default `casa`), `JOBAUTOMAT_PASSWORD` (unset = no auth
header), `JOBAUTOMAT_TIMEOUT` (seconds, default 120 — approving runs several
LLM calls).

Check it without an agent:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 mcp/server.py
echo '{"jsonrpc":"2.0","id":2,"method":"initialize","params":{}}' | python3 mcp/server.py
```

`aprobar_oferta` mutates state and spends LLM budget: ask the user before
calling it, and never call `buscar_ahora` in a loop (a manual search holds a
lock and returns 409 while one is running).
