# Changelog

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The project does not publish numbered stable releases yet: everything below is
what is on `main` since the first working panel.

## [Unreleased]

### Added

- New **Configuration** view backed by `data/settings.json`, an editable layer on
  top of `service/.env` (the panel wins). Writes are atomic and use `0600`
  permissions.
- `GET /api/config` returns the effective configuration and `PUT /api/config`
  updates it with type and range validation (`SCORE_THRESHOLD`, `TEMPERATURA`,
  `MAX_ANTIGUEDAD_DIAS`, `LINKEDIN_COOLDOWN_SEGUNDOS`…).
- Configuration keys really drive the search: `ROLES`, `TOPICS`, `UBICACIONES`,
  `MODALIDAD`, `KEYWORDS_NEGATIVAS`, `HORA_BUSQUEDA` and `FUENTES_*` change the
  queries and the scheduler time.
- New sources: Recruitee, Workable, BambooHR, EURES and InfoJobs, on top of
  Remotive, We Work Remotely, Ashby, LinkedIn and JobSpy.
- A **Test** button per source (`POST /api/fuente/probar`): fires a real request
  at a board and reports whether it answers, without touching `state.db`.
- **Metrics** view (`GET /api/metrics`): counts by state and by source, a compact
  header, built-in help and an ES/EN interface.
- **My profile** section reading `data/perfil.json` (`GET /api/perfil`).
- Add profile data in natural language (`POST /api/perfil/agregar`): the LLM
  places the fact in the right section of the profile.
- Delete by contextual icon and by a checklist of sections/lines
  (`POST /api/perfil/borrar`), without having to touch JSON paths.
- Technical mode: `PUT /api/perfil` replaces the whole profile with JSON
  validation, for bulk edits.
- A deterministic gate after rendering the CV: the PDF text is extracted again
  and hard profile facts (name, email, phone, companies, titles) are checked.
  The result lands in `job["pdf_check"]` (`ok`, `faltantes`, `paginas`,
  `motivo`) and the panel shows it next to the application.
- Anti-truncation: if the tailored CV comes out incomplete, the generator falls
  back to the full base CV instead of delivering a cut document.
- `GET /healthz` on the panel: a minimal `{"ok": true}` response for Docker's
  `healthcheck` (every 60 s, with a `start_period`).
- `GET /api/salud` and `service/health.py`: diagnostics of the five things that
  usually break in a self-host — DeepSeek, Telegram, LinkedIn session, writing to
  `/data` and Chromium — with a global `ok` key. `health.py` uses the standard
  library only and also runs standalone inside the container
  (`python health.py`).
- `WEBHOOK_URL` pushes the same notice to a generic webhook (ntfy, Discord,
  Slack…) as plain text, capped at ~1900 characters and with a short timeout.
- `TELEGRAM_HABILITADO=false` allows running with webhook and panel only; the
  Telegram token stops being mandatory when a webhook is configured.
- Proactive warning when the LinkedIn session is about to expire
  (`ALERTAR_SESION_LINKEDIN`, `SESION_LINKEDIN_MAX_DIAS`).
- `GET /api/export` downloads profile and `settings` as a single JSON file, with
  secrets masked (useful to move configuration between instances).
- `POST /api/import` restores profile and/or `settings` from that export; masked
  values are not written, so keys are filled in by hand at the destination. Both
  routes go through the rate limit and the origin filter.
- Image published on **GHCR**: `ghcr.io/kauman189/jobautomat:latest`, used by
  default in `docker-compose.yml` (`pull_policy: missing`, with a local build as
  a fallback when the registry does not answer).
- `JOBAUTOMAT_IMAGE` pins another tag or a local build without editing the
  compose file.
- CI that builds the image, checks that it starts and smoke-tests importing the
  service modules.
- README with a *Quick start* (pull + `.env` + up), a Mermaid architecture
  diagram and a TLS guide with Caddy.
- `docs/arquitectura.md`: diagram and responsibility of each component.
- `scripts/backup.sh`: copies `state.db`, `perfil.json`, `settings.json`,
  `linkedin_session.json`, `candidaturas/` and `logs/` into
  `./backups/jobautomat-<date>.tar.gz`, with a consistent SQLite snapshot when
  `sqlite3` is available.
- A minimal **MCP** server (`mcp/server.py`) that exposes the panel to coding
  agents: JSON-RPC 2.0 over stdio, one message per line, standard library only
  (the `mcp` SDK is not required). Each tool wraps a panel route, configured
  with `JOBAUTOMAT_URL`, `JOBAUTOMAT_USER`, `JOBAUTOMAT_PASSWORD` and
  `JOBAUTOMAT_TIMEOUT`, so it can be registered in Claude Desktop or Cursor.
- `AGENTS.md`: instructions for AI coding agents working in the repository
  (architecture and data flow, the hard "never submit an application"
  constraint, how to run, test and lint, and how to use the MCP server).
- `pyproject.toml` with project metadata (name, `version = "1.0.0"`,
  `requires-python >= 3.12`, dependency list, MIT license) and the tooling
  config: `[tool.ruff]` (line length 100, `py312`, rules `E`, `F`, `I`, `W`,
  `UP`, `B`) and `[tool.coverage.*]` (branch coverage over `service` and
  `mcp`).
- `service/version.py` as the single source of truth for `__version__`
  (`1.0.0`), kept in sync with `pyproject.toml`.
- **Lint** job in CI (`ruff check`, `ruff format --check` and `py_compile` of
  `service/`) and a **test** job running `pytest` under `coverage` with a
  `--fail-under=11` gate, so style and coverage regressions fail the build.
- `.pre-commit-config.yaml`: ruff (lint and format) plus a local pytest hook,
  so the same checks run before every commit.
- `SECURITY.md` with the supported-version policy and a private vulnerability
  reporting channel.
- `.github/dependabot.yml` for pip and GitHub Actions updates.
- MIT license (`LICENSE`), copyright K.189.
- `.env.example` and `data/settings.json.example` translated to English and
  extended with the new keys `IDIOMA_SCORING`, `TELEGRAM_HABILITADO`,
  `WEBHOOK_URL`, `ALERTAR_SESION_LINKEDIN` and `SESION_LINKEDIN_MAX_DIAS`.

### Changed

- Secrets (`DEEPSEEK_API_KEY`, `TELEGRAM_BOT_TOKEN`, `WEB_PASSWORD`) are always
  returned masked; they never leave the API in cleartext.
- Password comparison now uses `hmac.compare_digest` instead of `==`.
- Every profile change is reflected in the next round of letters and CVs.
- Any webhook or Telegram delivery failure is logged and the run continues; it
  never breaks the pipeline.
- Configuration and startup errors print key names only, never secret values.
- Role-agnostic behaviour: the cover-letter, CV-tailoring and recruiter-message
  prompts and the scoring rubric no longer hardcode a role or a design
  background; what to emphasise comes from the profile and the configured
  `ROLES`/`TOPICS`. Product / UI-UX design remains only the shipped default.
- Neutral search defaults: `ROLES` defaults to `[]` and the `QUERIES` fallback
  is empty, so the terms come from `ROLES` + `TOPICS`. When both are empty the
  run warns, skips LinkedIn, the boards and JobSpy instead of searching a
  hardcoded role, and says how to fix it in `data/settings.json`.
- Personal data moved out of the repository: profile, base CVs (ES/EN),
  cover-letter templates, master profile and the CV adaptation guide now ship
  as `.example` files, and `.gitignore` keeps the real ones local. Prompts were
  rewritten without personal facts, so a clone starts from placeholders.

### Removed

- `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md` and the issue/PR templates: they were
  added as boilerplate and dropped again in the same round, so `main` only keeps
  the governance that is actually maintained (`SECURITY.md`, dependabot).

### Security

- In-memory per-IP rate limit (`RATE_LIMIT_PREGUNTAS`, `RATE_LIMIT_ACCIONES` per
  `RATE_LIMIT_VENTANA`) on routes that spend LLM calls or write state; when
  exceeded it responds `429` with `Retry-After`.
- CSRF/same-origin check: `POST`/`PUT` with an `Origin`/`Referer` from another
  host receive `403`. Without those headers (curl, scripts) they are allowed,
  because the real authorisation is Basic auth. `ORIGENES_PERMITIDOS` adds
  origins.
- The per-IP limit uses the real connection IP, not `X-Forwarded-For`, which a
  client can forge.

### Fixed

- The PDF gate **does not block** or raise: a missing PDF or a checker failure is
  recorded as a warning and the application moves on.
