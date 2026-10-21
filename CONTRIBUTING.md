# Contributing to jobautomat

Thanks for taking the time to contribute. This document covers the local setup,
the checks that must pass before opening a pull request and the conventions the
project follows.

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to contribute

- **Bugs** — open an issue with the bug report form (include version, OS and steps to reproduce).
- **Features** — open an issue first so the approach can be discussed; large pull requests without a prior issue may be asked to split or wait.
- **Docs** — typo fixes, clearer explanations and translation improvements are always welcome.
- **Code** — bug fixes, new job boards under `service/search/`, tests and coverage.

Please do not open a pull request that includes real personal data,
`data/perfil.json`, `data/linkedin_session.json`, `.env` files or API keys.

## Development setup

Requirements: Python 3.12 and, optionally, Docker for the full stack.

```bash
git clone https://github.com/Kauman189/jobautomat.git
cd jobautomat

python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements-dev.txt      # pytest, flask, ruff, coverage
pip install -r service/requirements.txt  # runtime deps (only needed to run the service)
```

The modules in `service/` use flat imports (`import config`, `import db`), so
tests expect `service/` on `PYTHONPATH`; `tests/conftest.py` adds it
automatically. When running anything by hand, export it explicitly:

```bash
export PYTHONPATH=service
```

## Tests, lint and coverage

All three run in CI on every push and pull request, and locally via
[pre-commit](https://pre-commit.com):

```bash
pip install pre-commit
pre-commit install

pytest tests/ -q                            # tests
ruff check . && ruff format --check .       # lint + format
coverage run -m pytest tests/ -q && coverage report --fail-under=11
```

Rules:

- Tests are required for behaviour changes. New modules should not lower the
  coverage below the 30 % gate.
- `ruff` configuration lives in `pyproject.toml` (`[tool.ruff]`, line length 100,
  target py312, rules `E,F,I,W,UP,B`). Fix anything it reports instead of
  disabling rules; if a rule genuinely does not fit, add a narrow `# noqa: CODE`
  with a one-line reason.
- Do not reformat unrelated files: keep diffs focused.

## Pull requests

1. Fork the repository and create a branch from `main`
   (`fix/short-description`, `feat/short-description`, `docs/short-description`).
2. Make the change, add or update tests, run the checks above.
3. Commit with an imperative, descriptive subject line ("Fix scoring crash on empty salary").
   Keep commits coherent; squash noise before opening the PR.
4. Open the pull request using the template and fill in the checklist.
5. CI (docker build, import smoke test, ruff, pytest + coverage) must be green.
6. A maintainer reviews. Expect questions about edge cases, error handling and
   user-facing impact — automated job-board scraping is fragile and needs care.

Documentation lives in `docs/` (Spanish) and `README.md` (English). If your change
alters configuration, environment variables or the pipeline behaviour, update
the relevant docs in the same pull request.

## Adding a job board

New sources go under `service/search/` and must:

- respect the rate limits and cooldowns already implemented in `service/rate.py`;
- return the same normalized offer shape as the existing boards;
- degrade gracefully (log and return an empty list) when the source is down;
- ship with tests that do not hit the network.

## Security

Do not report vulnerabilities in a public issue. See [SECURITY.md](SECURITY.md).
