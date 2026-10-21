## What does this PR do?

<!-- One paragraph. Link the issue it closes: "Closes #123". -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Refactor / internal cleanup
- [ ] CI, tooling or dependencies

## Checklist

- [ ] Tests added or updated for the behaviour change (`pytest tests/ -q`)
- [ ] `ruff check .` passes
- [ ] `ruff format --check .` passes
- [ ] Coverage did not drop below the 30 % gate (`coverage run -m pytest tests/ -q && coverage report --fail-under=30`)
- [ ] Docs updated when behaviour, config or env vars changed (`README.md`, `docs/`, `CHANGELOG.md`)
- [ ] No secrets, `.env` files, `data/perfil.json`, session cookies or personal data in the diff
- [ ] Changes are focused: no unrelated reformatting or drive-by edits

## How was it tested?

<!-- Commands run, scenarios covered, manual checks in the web panel. -->

## Notes for reviewers

<!-- Trade-offs, follow-ups, anything that needs a decision. -->
