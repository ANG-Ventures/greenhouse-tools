# greenhouse-tools

The **real, kept** home for tools the Greenhouse autonomous pipeline builds.

This replaces the throwaway `greenhouse-sandbox`: an Ace-approved seed idea is spec'd, reviewed,
built by daedalus-opus, gated by the deterministic floor (pytest + bandit + gitleaks + pip-audit +
mypy), and — when it passes — **merged here for real**. Each tool is a small, self-contained,
stdlib-only Python tool under `tools/`, with a pytest suite under `tests/`, a `--selfcheck` health
flag, and a `## Reversibility` section.

## Layout
- `tools/<name>.py` (or `tools/<name>/`) — the built tools.
- `tests/` — the pytest suites the floor runs (network-isolated).
- Every tool ships reversibility docs (off-by-default, deletable, no state outside its own files).

## Reversibility
Every tool here is reversible by construction. A merge is git-revertable; a tool is removed by
deleting its files. Nothing here activates on import or touches state outside its own directory.
