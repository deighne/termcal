# Agent Guidelines for TermCal

These instructions apply to all AI coding agents working on this repository.

---

## 1. Branching & Release Workflow (STRICT)

**NEVER push directly to `main`.**

All development must follow this tiered branching model:

```text
feature/<name> / fix/<name>
         │  (PR / merge)
         ▼
      develop
         │  (batch release PR / merge)
         ▼
       main (releases & tags)
```

### The Branches
- **`main`**: Production release branch. Only updated by merging `develop` into `main` after batches of work are completed, fully tested, and ready for a release tag (e.g. `v0.2.0`).
- **`develop`**: The primary integration branch. All active feature and fix branches merge into `develop`.
- **`feature/<name>`** or **`fix/<name>`**: Isolated branches for each individual task, feature, or bug fix.

### Step-by-Step Task Workflow
1. **Start from `develop`**:
   ```bash
   git checkout develop
   git pull origin develop
   ```
2. **Create a dedicated feature branch**:
   ```bash
   git checkout -b feature/<feature-name>
   ```
3. **Develop & Test**:
   - Implement the feature in `termcal.py`.
   - Add comprehensive unit tests in `test_termcal.py`.
   - Verify checks pass cleanly:
     ```bash
     make lint && make test
     ```
4. **Merge to `develop`**:
   - Commit changes to the feature branch.
   - Push feature branch to origin: `git push -u origin feature/<feature-name>`.
   - Open a PR targeting `develop` (or merge into `develop` if working locally):
     ```bash
     git checkout develop
     git merge --no-ff feature/<feature-name>
     git push origin develop
     ```
5. **Batch Release from `develop` -> `main`**:
   - Once a batch of features is completed, tested, and ready for release:
     ```bash
     git checkout main
     git pull origin main
     git merge --no-ff develop
     git push origin main
     ```
   - Tag the release (e.g. `git tag -a v0.2.0 -m "Release v0.2.0"` and `git push origin v0.2.0`).
   - Update Homebrew tap formula in `deighne/homebrew-tap`.

---

## 2. Architecture & Code Constraints

1. **caldir owns the data**: TermCal is a fast terminal UI built on top of [caldir](https://caldir.org). caldir handles local `.ics` storage, sync, and remote calendar connectivity. TermCal reads local state directly, invokes caldir for writes/syncs, and never blocks the UI.
2. **Zero runtime dependencies**: Only standard library modules (`curses`, `datetime`, `subprocess`, `pathlib`, `json`, `webbrowser`, etc.). Never add third-party dependencies to `pyproject.toml` runtime dependencies.
3. **Single-module codebase**: The core application lives in `termcal.py`. Do not split into multiple packages unless explicitly requested.
4. **Documentation integrity**: Keep comments and docstrings intact. Update `README.md` whenever keybindings or user-facing views change.
5. **Code formatting & linting**: Line length limit is 110. Always run `make fix` and `make lint` before committing.

---

## 3. Sandboxed Git Notes

In sandbox execution environments:
- Prefix git commands with `GIT_CONFIG_GLOBAL=/dev/null` if system git configs cause permission denials.
- Commits and pushes require bypass sandbox permissions because writing to `.git/` is privileged.
