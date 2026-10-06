# Contributing

```bash
git clone https://github.com/TBS093A/tdscope && cd tdscope
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,tui]" pre-commit
pre-commit install            # gitleaks, ruff and the dump/private-word checks on every commit
pytest
```

## Never commit real thread dumps

Thread dumps contain host names, URLs, client IPs and sometimes user data. The
`no-thread-dumps` hook rejects any file that looks like a dump outside `tests/fixtures/`.
New fixtures must be synthetic (see `tests/fixtures/make_aemcs_fixtures.py`).

If you analyse dumps of a customer, also list the words that must never reach git
(customer names, host names, project ids) in `.private-patterns`, one case-insensitive
regular expression per line. The file is git-ignored, so the list itself stays private.
The `private-patterns` hook checks every staged file and file name against it.

## Quality gate

A pull request can be merged when the `ci-passed` and `security-passed` checks are green.
See [docs/RELEASING.md](docs/RELEASING.md) for the full pipeline.

| Check | Run locally |
|-------|-------------|
| lint, format, types | `ruff check . && ruff format --check . && mypy` |
| tests | `pytest` |
| secrets | `pre-commit run gitleaks --all-files` |
| SAST | `bandit -c pyproject.toml -r src` (CodeQL runs in CI only) |
| SCA | `pip install -e ".[security]" && pip-audit` |
| dynamic tests | `pip install -e ".[security]" && playwright install chromium && HYPOTHESIS_PROFILE=dast pytest -m dast` |
