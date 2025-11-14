# Lint & Safety Setup

## Quick start
```bash
pip install -U ruff pre-commit mypy bandit black
pre-commit install
ruff check --fix . && ruff format .
mypy .
bandit -q -r . -x tests,bot_logs,.venv
```
