# brief-delta — Reversibility & Operations

**Status:** v0.3 reduced standalone read-side delta tool, **off by default.**

## What it is
A stdlib-only Python 3.11 tool that reads the morning-digest structured
`_render_input.json`, builds a bounded local snapshot store, folds the retained
store into a last-seen URL index, and prints NEW / MOVED / RESOLVED /
UNCHANGED. MOVED is local and deterministic: same URL seen earlier in the
retained store with a section change or score movement at/above
`--move-threshold` (default 5). RESOLVED stays scoped to the immediate prior
snapshot so old history does not flood the dropped-off lane. It does not post to
Discord, edit the producer, or wire itself into cron.

## Reversibility

This tool is reversible by construction.

- **Off by default.** Nothing runs on import. No cron, daemon, launchd job, or
  scheduler entry is installed or enabled by this build. A human must invoke the
  tool explicitly.
- **State it touches:** only JSON snapshots under the caller-selected
  `--state-dir` (default `~/.hermes/greenhouse/brief_delta/`). It reads the
  producer's `_render_input.json` but never mutates it. It never touches
  `ai-news-seen.json`, Discord, cron, prompts, or any shared producer state.
- **Bounded store.** After each render run it prunes `snapshot-YYYY-MM-DD.json`
  files to `--retention-days` (default 35), keeping the newest snapshot. The
  reduced delta folds all retained snapshots older than today into the last-seen
  index, while RESOLVED compares only against the immediate prior valid
  snapshot. This keeps the nightly floor deterministic and bounded.
- **Uninstall / rollback = delete files.** To remove completely:
  1. Remove state if desired: `rm -rf ~/.hermes/greenhouse/brief_delta/`.
  2. Remove the tool and tests: `rm -f tools/brief_delta.py tests/test_brief_delta.py`.
  3. Remove captured fixtures if desired: `rm -rf tests/fixtures/`.
  4. If a later version adds a scheduler/cron entry, remove that entry too.
  Nothing else was touched. There is no migration to undo and no remote change
  to revert.

## Health & liveness probes (NOT the same thing)
- **`--selfcheck`** — offline deploy health probe. Builds its own synthetic
  immediate-prior fixture, reads no real source, and exits `0` only if
  NEW/MOVED/RESOLVED/UNCHANGED classify correctly behind a 1-day-old prior.
- **`--check-target`** — real-source liveness gate. Asserts the configured
  `_render_input.json` exists, is a regular file, parses as JSON, has selected
  and also lists, and contains at least one item with a URL. It exits non-zero
  with `LIVENESS FAILURE` on missing/empty/malformed input, so "read nothing"
  cannot be a silent success.

## Usage
```
python -m tools.brief_delta --selfcheck
python -m tools.brief_delta --check-target
python -m tools.brief_delta --source ~/.hermes/state/cron/morning-digest/_render_input.json --state-dir ~/.hermes/greenhouse/brief_delta/
```

---

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


---

# reaction_state — Reversibility & Operations

**Status:** v0.1, PROTOTYPE of the durable-state core, **off by default.**

## What it is
A stdlib-only Python 3.11 tool that turns raw Discord reaction *transitions*
into durable triage state in **its own SQLite DB**. It applies each add/remove
event atomically and idempotently keyed on `(channel_id, message_id, emoji,
user_id)` — with no dependence on whether the message was ever cached — and runs
a one-time boot reconcile sweep against a **watermarked** authoritative snapshot
to recover transitions missed while offline. Its transport-of-record is a local
append-only journal `reactions.jsonl` (one JSON line per raw transition) that a
future, non-stdlib gateway adapter would write.

It does **not** open a Discord gateway connection. `discord.py` (which owns the
raw `on_raw_reaction_add/remove` events) is non-stdlib; the live websocket client
is out of scope and is the seam where a later phase attaches. This tool defines
and tests that seam — it never opens a socket.

## Reversibility

This tool is reversible and bounded by construction.

- **Off by default.** Nothing runs on import. No cron, daemon, launchd job, or
  scheduler entry is shipped enabled. The nightly job is documented but
  disabled; you enable it explicitly if you want it.
- **State it touches:** exactly one file — the SQLite DB you name with `--db`.
  No dotfiles, no global state, no network. The `--selfcheck` probe uses an
  in-memory `:memory:` DB and touches no disk at all. Reconcile is **add-only by
  default**: absence in a snapshot never deletes durable state, so a bad or
  stale snapshot cannot destroy triage actions. Remove-on-absence is honored
  only when BOTH the caller opts in AND the snapshot declares it enumerates the
  full present-set; the nightly entry never opts in.
- **Cannot mutate Discord or call out.** It imports stdlib only and imports no
  networking, subprocess, websocket, or `discord` module. Tests assert both via
  an AST walk. It can only read its journal and write its own DB.
- **Uninstall = delete files.** To roll back completely:
  1. Delete the durable-state DB: `rm -f <the --db path>`.
  2. Remove the tool: `rm -f tools/reaction_state.py`.
  3. Remove its tests: `rm -f tests/test_reaction_state.py`.
  4. If you enabled the nightly job, remove that scheduler/cron entry.
  Nothing else was touched. There is no migration to undo and no remote change
  to revert, because the tool never made one.

## Health & liveness probes (NOT the same thing)
- **`--selfcheck`** — offline deploy health probe. Builds its OWN in-memory
  fixture, exercises apply + reconcile, and asserts the durable invariants.
  Touches NO real journal; runs in the network-isolated floor. Exit `0` iff the
  core logic is internally correct. It says nothing about whether a real journal
  exists. A garbage/unknown flag exits non-zero (real argv dispatch).
- **`--check-target`** — real-journal liveness gate. Asserts the ACTUAL
  `reactions.jsonl` exists, is a regular file, is non-empty, AND parses into
  >=1 valid event; loud non-zero exit otherwise. The nightly entry runs THIS
  first, so "read nothing" can never be a silent exit 0.

## Usage
```
python -m tools.reaction_state --selfcheck                          # deploy probe
python -m tools.reaction_state --check-target --journal reactions.jsonl  # nightly gate
python -m tools.reaction_state --journal reactions.jsonl --db state.db    # replay
```


---

# offsite-restore-drill — Reversibility & Operations

**Status:** v0.1 prototype, propose-only, **off by default.**

## What it is
A stdlib-only Python 3.11 adapter that proves the `fleet-offsite2:` encrypted
backup tar is usable by pulling the newest dated `hermes-fleet-encrypted-*.tar`
into a throwaway scratch root, extracting it with a tar path-escape guard, then
invoking the existing `restore-drill.py` unchanged with `FBRD_BACKUP_ROOT` pointed
at the extracted offsite copy. `--selfcheck` is an offline health probe using a
self-built fixture. `--check-target` is separate and validates the real remote is
reachable and non-empty.

## Reversibility

This tool is reversible by construction.

- **Off by default.** Nothing runs on import. No launchd job is installed. The
  shipped `launchd/ai.hermes.offsite-restore-drill.plist.proposed` is inert until
  a human installs it; `RunAtLoad` is false. A `DISABLED` sentinel under
  `~/.hermes/state/offsite-restore-drill/DISABLED` makes the proposed launchd
  command exit 0 before checking or pulling anything.
- **State it touches:** only its configured scratch/state roots, defaulting to
  `~/.hermes/state/offsite-restore-drill/`. Success writes `offsite-drill.ok` and
  `backup/last-success-offsite-restore-drill` under that state dir. Failed runs
  move the scratch dir under that same state dir's `quarantine/`. It never writes
  to `~/.ai-agent-backups`, never mutates the rclone remote, and never edits the
  reused local restore drill's state.
- **Real target liveness fails loud.** Until `rclone` and the `fleet-offsite2:`
  remote are provisioned, `--check-target` exits non-zero. That is intentional;
  `--selfcheck` remains offline and independent.
- **Uninstall = delete files.** To roll back completely:
  1. If manually installed, remove the launchd job with
     `launchctl bootout gui/$(id -u)/ai.hermes.offsite-restore-drill` and delete
     the installed plist.
  2. Delete state if desired: `rm -rf ~/.hermes/state/offsite-restore-drill`.
  3. Remove the tool and tests: `rm -rf tools/offsite_restore_drill tests/offsite_restore_drill`.
  4. Remove the proposed plist artifact:
     `rm -f launchd/ai.hermes.offsite-restore-drill.plist.proposed`.
  Nothing else was touched.

## Usage
```
python -m tools.offsite_restore_drill.offsite_restore_drill --selfcheck
python -m tools.offsite_restore_drill.offsite_restore_drill --check-target
python -m tools.offsite_restore_drill.offsite_restore_drill --run
```


---

# restore-canary -- Reversibility & Operations

**Status:** RESPEC v4, off by default, read-mostly drill.

## What it is
A nightly restore-drill for an encrypted `restic` home-lab backup. It decrypts +
restores ONE known sentinel file from the latest matching snapshot, hashes it
against a recorded sha256, and emits a secret-free PASS/FAIL artifact -- proving
backups are *restorable*, not merely consistent. The committed module ships only
pure logic + restic/`op` command builders and parsers; the live `--run` path is
host-only. Tests and both health probes exercise the builders/parsers over
self-built fixtures and never touch the network.

## Reversibility

This tool is reversible by construction and **off by default.**

- **Off by default.** Nothing runs on import. No cron, launchd, daemon, or
  scheduler entry is shipped enabled. The nightly job and the off-host heartbeat
  monitor are documented but disabled; you enable them explicitly.
- **Restore is to a throwaway target.** The live `--run` restores the single
  canary file into a fresh temp `--target` dir, hashes it, then the operator
  discards that dir. It never restores over live data, never writes into the
  backup repo, and never runs restic forget/prune (no destructive restic verbs
  are built anywhere in this module).
- **No state outside its own artifact.** The only durable write is the PASS/FAIL
  artifact JSON at the path you choose. No DB, no cache, no cross-run store.
- **Secrets stay scoped (B-3).** OP_SERVICE_ACCOUNT_TOKEN is delivered ONLY to
  the scoped `op` subprocess env -- never to restic's env, never to argv, never to
  the artifact, log, stdout/stderr, exception, or heartbeat. A scrub pass strips
  any known secret value before any string is emitted. Tests assert all of this.
- **Uninstall = delete files.** To roll back completely:
  1. Delete any artifact JSON you generated.
  2. `rm -rf tools/restore_canary/`.
  3. If you enabled the nightly cron or the off-host heartbeat monitor, remove
     those entries.
  Nothing else was touched; there is no migration to undo and no remote change to
  revert, because the tool never made one.

## Health & liveness probes (NOT the same thing)
- **`--selfcheck`** -- offline deploy health probe. Builds its OWN fixture
  (config + captured-shape snapshots --json + a real temp canary file), verifies
  B-1/B-1r/B-3/R-3/R-4 invariants, and exits 0 iff the core logic is internally
  correct. Touches NO real source; runs in the network-isolated floor. A
  garbage/unknown flag exits non-zero (real argv dispatch).
- **`--check-target` / `--check-vault`** -- real-source liveness gate. Asserts the
  ACTUAL config file exists, is a regular non-empty file, validates, the required
  secret env (OP_SERVICE_ACCOUNT_TOKEN) is present, and the repo mount_type is
  durable (R-5). Loud non-zero otherwise. The nightly entry runs THIS first, so
  "read nothing" can never be a silent exit 0.
- **`--audit-artifact <f>`** -- on-host artifact-integrity check (B-2, demoted):
  flags a stale / FAIL / unparseable artifact WHILE the host is up. It explicitly
  does NOT cover host-down -- that is the off-host PUSH heartbeat's job.

## Usage

    python -m tools.restore_canary.canary --selfcheck                          # deploy probe
    python -m tools.restore_canary.canary --check-target --config canary.json  # nightly gate
    python -m tools.restore_canary.canary --audit-artifact last-run.json       # on-host integrity


---

# alias_expand — Reversibility & Operations

**Status:** v0.1, PURE text->text expander, propose-only, **off by default.**

## What it is
A stdlib-only Python 3.11 tool that borrows the HA/hassil alternation grammar
(`( | )` alternation, `[ ]` optional) into HACR capability aliases. It reads a
curated `capabilities/*.yaml` alias list, expands any alias containing grammar
tokens into the FULL deterministic cross-product of plain literal strings, and
emits the expanded list. This lets an author write ONE phrase —
`turn (off|out) [the] lights` — instead of hand-maintaining the 6-9
near-duplicate literals the router's `_match_capability` needs today.

It is a text-in / text-out fan-out. It NEVER routes, NEVER actuates, NEVER edits
the safety policy. The router matcher is unchanged — it still sees flat literal
aliases and runs its existing normalize + substring-contains logic. The safety
layer (camera-floodlight exclusion, confirmation gating, room remap, the
curated-YAML allowlist) is NOT touched. This tool does NOT migrate device control
to HA Assist and must never be read as doing so.

## Reversibility

This tool is reversible by construction.

- **Off by default.** Nothing runs on import. No cron, daemon, launchd job, or
  scheduler entry is shipped enabled. The nightly job is documented but disabled;
  you enable it explicitly if you want it.
- **State it touches: NONE.** The expander is a pure function; the CLI reads the
  capabilities YAML for reading only and prints the expanded aliases to stdout.
  It writes no file, no DB, no cache, no dotfiles, no temp state. It does not
  edit the capability files it reads — expansion output is emitted to stdout for
  a human to review and paste, or not.
- **Cannot mutate anything or call out.** It imports stdlib only (no PyYAML, no
  hassil, no `requests`) and imports no networking, subprocess, or HA/websocket
  module. A test asserts this via an AST walk. It cannot touch the router, the
  safety policy, or any remote endpoint.
- **Reversible by identity.** The output is exactly the literal alias strings a
  human would otherwise have typed; adopting an expansion is a no-op change to
  the matcher's behavior. To un-adopt, revert the capability YAML edit you made
  by hand — there is nothing else to undo.
- **Uninstall = delete files.** To roll back completely:
  1. Remove the tool: `rm -f tools/alias_expand.py`.
  2. Remove its tests: `rm -f tests/test_alias_expand.py`.
  3. If you enabled the nightly job, remove that scheduler/cron entry.
  Nothing else was touched. There is no migration to undo and no remote change to
  revert, because the tool never made one.

## Health & liveness probes (NOT the same thing)
- **`--selfcheck`** — offline logic probe. Builds its OWN in-memory
  `(pattern -> expected expansion)` fixtures, asserts the expander produces the
  exact deterministic cross-product, that determinism holds within the run, and
  that a no-token literal passes through byte-identical. Touches NO real repo,
  path, or the `ha-command-router` checkout; runs in the network-isolated floor.
  Exit `0` iff the logic is internally correct. It is NOT a liveness signal —
  a green `--selfcheck` says nothing about whether the real capabilities dir
  exists. A garbage/unknown flag exits non-zero (real argv dispatch).
- **`--check-target`** — real-source liveness gate. Asserts the ACTUAL
  `--capabilities-dir` exists, is a directory, contains >=1 capability `*.yaml`
  (excluding `policy.yaml`/`groups.yaml`/`llm_providers.yaml`), AND that at least
  one capability carries a non-empty `aliases` list — i.e. there is real input to
  expand. Each failure emits one alert-worthy stderr line prefixed
  `ALIAS_EXPAND_LIVENESS_FAIL:` and exits non-zero. The nightly entry runs THIS
  first, so an empty / renamed / unmounted capabilities dir screams instead of
  silently exiting 0.

## Usage

    python -m tools.alias_expand --selfcheck                                        # deploy probe
    python -m tools.alias_expand --check-target --capabilities-dir /path/to/capabilities  # nightly gate
    python -m tools.alias_expand --capabilities-dir /path/to/capabilities           # emit expanded aliases


---

# repo-test-cmd-probe — Reversibility & Operations

**Status:** v0.1, read-only nightly registry probe, **off by default.**

## What it is

A stdlib-only Python 3.11 tool that scans local git repo directories for repos
that appear to ship tests but lack a README-documented one-command test path. It
prints HIT records and unified-diff README patch drafts. It never edits a target
repo; patch application is a later human/reviewer action.

## Reversibility

This tool is reversible by construction.

- **Off by default.** Nothing runs on import. No cron, daemon, launchd job, or
  scheduler entry is installed or enabled by this build. A human must invoke the
  module or wire a nightly entry explicitly.
- **State it touches: NONE.** The scanner reads local directories and README-like
  files, then writes only stdout/stderr. It creates no DB, cache, dotfile, temp
  state that survives a run, and no patch file. The `--selfcheck` fixture uses an
  OS temp dir that is removed before exit.
- **No network and no repo mutation.** It imports stdlib only and does not import
  networking or subprocess modules in its production path. It never shells out to
  git, package managers, or test runners. README fixes are emitted as draft
  unified diffs only.
- **Uninstall = delete files.** To roll back completely:
  1. Remove the tool: `rm -f tools/repo_test_cmd_probe.py`.
  2. Remove its tests: `rm -f tests/test_repo_test_cmd_probe.py`.
  3. If you enabled a nightly job, remove that scheduler/cron/launchd entry and
     its log files.
  Nothing else was touched. There is no migration to undo and no remote change to
  revert, because the tool never made one.

## Health & liveness probes (NOT the same thing)

- **`--selfcheck`** — offline deploy health probe. Builds its own temporary
  three-repo fixture, proves a fenced `pytest .` and `pytest -q` are documented,
  proves fenced `pytest is our runner` is a HIT, and exits `0` only when the
  fixture classifies as expected. It reads no real target.
- **`--check-target`** — real-target liveness gate. Asserts the configured
  `--target` exists, is a directory, and contains at least one git repo. It exits
  non-zero with `LIVENESS FAILURE` on missing/empty/wrong-kind targets, so a
  nightly run cannot silently scan nothing.
- **`--probe`** — prints launchd wiring requirements, including
  `StandardErrorPath`, so stderr from the nightly entry is durable.

## Usage

    python -m tools.repo_test_cmd_probe --selfcheck
    python -m tools.repo_test_cmd_probe --check-target --target /path/to/repos
    python -m tools.repo_test_cmd_probe --target /path/to/repos --limit 25

---

# fleet-doc-audit — Reversibility & Operations

**Status:** v0.1 read-only auditor, **off by default.**

## What it is
A stdlib-only Python 3.11 tool that reads the canonical fleet roster doc and
reports the bounded drift facts from the approved spec: the `lcm-agents` scope
fence vs live profile/gateway/config inputs, the fleet-ownership reference link,
and the `last_verified` / `verified_body_sha256` body-stamp freshness contract.
It never edits the Obsidian doc. `--emit-stamp` prints a digest line for a human
to paste; it writes nothing.

## Reversibility

This tool is reversible by construction.

- **Off by default.** Nothing runs on import. No cron, daemon, launchd job, or
  scheduler entry is installed or enabled by this build. A human must invoke the
  tool or wire a nightly entry explicitly.
- **Read-only — touches no state.** The audit path, `--check-target`,
  `--emit-stamp`, and `--selfcheck` only read files or synthetic in-memory
  fixtures and print to stdout/stderr. There is no DB, cache, migration, network
  call, subprocess, or persistent state.
- **Doc prep remains human-owned.** The `verified_body_sha256` frontmatter line,
  the `lcm-agents` fence, and the one-line prose de-scoping edit are not written
  by this tool. Removing the digest degrades R-STAMP to WARN; removing the fence
  makes R-LCM fail closed with LOCATOR_MISSING; reintroducing a prose scope list
  is flagged loudly by the AC-19 guard.
- **Uninstall / rollback = delete files.** To remove completely:
  1. Remove the tool: `rm -f tools/fleet_doc_audit.py`.
  2. Remove its tests: `rm -f tests/test_fleet_doc_audit.py`.
  3. If you enabled a nightly job, remove that cron/scheduler entry.
  Nothing else was touched. There is no migration to undo and no remote change
  to revert.

## Health & liveness probes (NOT the same thing)
- **`--selfcheck`** — offline deploy health probe. Builds synthetic known-good
  and stale fixtures, reads no real source, and exits `0` only if the core audit
  behavior is internally consistent.
- **`--check-target`** — real-target liveness gate. Asserts the actual
  `Agents.md` exists, is a regular non-empty file, has the required locators
  (including the `lcm-agents` fence), and has no reintroduced prose scope
  enumeration. It intentionally does not run staleness oracles, so corrected
  values never fail the liveness gate.

## Usage

    python -m tools.fleet_doc_audit --selfcheck
    python -m tools.fleet_doc_audit --check-target "$HOME/Obsidian/Ace Place/AI/Agents.md"
    python -m tools.fleet_doc_audit --emit-stamp "$HOME/Obsidian/Ace Place/AI/Agents.md"
    python -m tools.fleet_doc_audit --live-json tests/fixtures/fixture_live.json "$HOME/Obsidian/Ace Place/AI/Agents.md"
