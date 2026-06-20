# triage-rule-miner — Reversibility & Operations

**Status:** v0.1, propose-only, **off by default.**

## What it is
A nightly, read-only analysis tool. It consumes a `behavior.ndjson` metadata
export (produced out-of-band by the existing `gws` CLI) and writes two files —
`proposals.json` and `proposals.md` — into a directory you choose with `--out`.
It proposes one-tap Gmail filter ideas. It never applies them.

## Reversibility

This tool is reversible by construction.

- **Off by default.** Nothing runs on import. There is no installed cron, daemon,
  launchd job, or scheduler entry shipped enabled. The nightly job is documented
  but disabled; you enable it explicitly if you want it.
- **No state outside its own output dir.** The only writes are
  `proposals.json` and `proposals.md` under the `--out` directory you pass.
  No DB, no cache, no cross-run learning store, no dotfiles, no temp state that
  survives a run (the `--selfcheck` probe uses an OS temp dir that is removed on
  exit). A test asserts the process writes nothing outside `--out`.
- **Never mutates Gmail/Calendar.** The package imports stdlib only and imports no
  networking or subprocess module — it physically cannot call the Gmail API, the
  `gws` CLI, or any filter-create endpoint. Tests assert both invariants via an
  AST walk. The output is a suggestion file; you apply filters by hand, or not.
- **Uninstall = delete files.** To roll back completely:
  1. Delete the proposal files: `rm -f <out>/proposals.json <out>/proposals.md`.
  2. Remove the tool: `rm -rf tools/triage_rule_miner/`.
  3. If you enabled the nightly job, remove that scheduler/cron entry.
  Nothing else was touched. There is no migration to undo and no remote change
  to revert, because the tool never made one.

## Health probe
`python -m tools.triage_rule_miner.miner --selfcheck` exits `0` on the bundled
known-good fixture and non-zero on corrupt input. The nightly cron (when enabled)
should gate on this exit code before trusting a run.

## Usage
```
python -m tools.triage_rule_miner.miner --in behavior.ndjson --out ./out
```
