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


---

# cron-silent-audit — Reversibility & Operations

**Status:** v0.1, read-only auditor, **off by default.**

## What it is
A nightly, stdlib-only auditor that READS the fleet cron registry
(`$HERMES_HOME/cron/jobs.json`) and the referenced script bodies under
`cron/scripts/`, then prints every *enabled* job that can fail silently
(delivered `local`/nowhere AND with no detectable alert path). It classifies
each into a verdict (`SILENT_SCRIPT_MISSING`, `SILENT_NO_ALERT_SCRIPT`,
`SILENT_NO_ALERT_PROMPT`, `SILENT_EMPTY_BODY`). It NEVER edits the registry,
re-routes delivery, or injects `notify.py`. The fix per job is a human
follow-up (route to `#alerts`/`#logs`, or add a `notify.py` call).

## Reversibility

This tool is reversible by construction.

- **Off by default.** Nothing runs on import. No cron, daemon, launchd job, or
  scheduler entry is shipped enabled. The nightly job is documented but
  disabled; you enable it explicitly if you want it.
- **Read-only — touches no state.** It opens `jobs.json` and script files for
  reading only and prints to stdout/stderr. It writes NO files, no DB, no
  cache, no dotfiles, no temp state that survives a run (the `--selfcheck`
  probe uses an OS temp dir removed on exit). It imports no networking or
  subprocess module in its hot path; it cannot mutate the registry or call any
  remote endpoint.
- **Uninstall = delete files.** To roll back completely:
  1. Remove the tool: `rm -rf tools/cron_silent_audit/`.
  2. Remove its tests: `rm -f tests/test_cron_silent_audit.py`.
  3. If you enabled the nightly job, remove that scheduler/cron entry.
  Nothing else was touched. There is no migration to undo and no remote change
  to revert, because the tool never made one.

## Health & liveness probes (NOT the same thing)
- **`--selfcheck`** — offline logic probe. Builds its own in-memory fixture,
  runs the classifier, asserts the verdicts. Touches NO real source; runs in
  the network-isolated floor. Exit `0` iff the logic is internally correct.
  This is the DEPLOY health check. It says nothing about the real registry.
- **`--check-target`** — real-target liveness. Asserts the ACTUAL registry
  exists, is a dict with a `jobs` list, and is non-empty; loud non-zero exit
  otherwise. The nightly entry runs THIS first, so "read nothing" can never be
  a silent exit 0.

## Usage
```
python -m tools.cron_silent_audit.audit --check-target   # nightly gate
python -m tools.cron_silent_audit.audit                  # flagged candidates
python -m tools.cron_silent_audit.audit --all            # every enabled job
```
Defaults: `--registry` = `$HERMES_HOME/cron/jobs.json` (fallback
`~/.hermes/cron/jobs.json`); `--scripts-dir` = sibling `scripts/`.
