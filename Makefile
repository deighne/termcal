.PHONY: lint test fix hooks install-dev

# Lint + format check + unit tests (same as the pre-commit hook)
lint:
	ruff check
	ruff format --check
	python3 -m unittest -q

test:
	python3 -m unittest -v

# Auto-fix what ruff can, then format
fix:
	ruff check --fix
	ruff format

# Install locally in editable mode with pipx
install-dev:
	pipx install --editable .

# Enable the repo pre-commit hook after a fresh clone
hooks:
	git config core.hooksPath .githooks
