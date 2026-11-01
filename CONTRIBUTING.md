# Contributing to TermCal

Contributions and bug reports are welcome!

## Architecture & Principles

1. **caldir owns the data**: TermCal is a fast keyboard-driven terminal UI built on top of [caldir](https://caldir.org). Caldir handles local `.ics` storage, provider connections, and synchronization. TermCal reads local state directly, invokes caldir for writes/syncs, and stays responsive.
2. **Standard library only**: The runtime has zero third-party Python dependencies—just Python 3.11+ and curses.
3. **Single-module codebase**: The core application lives in `termcal.py` with unit tests in `test_termcal.py`.

## Development Setup

Requirements:
- Python 3.11+
- [caldir](https://caldir.org) (`curl -sSf https://caldir.org/install.sh | sh`)
- [ruff](https://docs.astral.sh/ruff/) (`brew install ruff` or `pip install ruff`)

```bash
# Enable the git pre-commit hook (runs lint + tests on commit)
make hooks

# Run checks
make lint
make test

# Auto-fix formatting and lint issues
make fix

# Install in editable mode via pipx
make install-dev
```

## Submitting Pull Requests

- Keep changes focused and cleanly formatted.
- Ensure all tests pass (`make lint && make test`).
- Add unit tests in `test_termcal.py` for any new parser logic, modal interaction, or ICS handling.
