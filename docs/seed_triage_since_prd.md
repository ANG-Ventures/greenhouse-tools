# PRD / Spec — `seed_triage` v0.2b: journal-derived "since" (first-seen verdict age)

**Repo:** `ANG-Ventures/greenhouse-tools` · **Lands in:** `tools/seed_triage.py` + `tests/test_seed_triage.py`
**Status:** APPROVED v0.2b (folded Opus pass-1 BLOCK — 2 blockers confirmed empirically against the real
core + 2 required changes, all folded below) · **Owner:** Apollo · **Python:** 3.11, **stdlib-only**
**Consumes:** the unmodified `tools/reaction_state.py` core (unchanged) + the append-only
`reactions.jsonl` journal the live gateway produces.
**Roadmap origin:** the `seed_triage` adapter PRD (`seed_triage_adapter_prd.md`) deferred a real "since"
column to v0.2b because the naive source (the core's `reconcile()` path) hardcodes `ts="reconcile"`
(D-11). Ace's decision (2026-06-30): **do NOT edit the core for this** — derive "since" from the journal
in the reporter instead.

---

## 0. Ground-Truth (measured live before the design)

1. **The journal carries real timestamps — TODAY.** `~/.hermes/greenhouse/reactions.jsonl` (254 live
   events at spec time) — **254/254 are ISO-8601 `YYYY-MM-DDTHH:MM:SSZ`**, zero non-ISO, actions ∈ {add,
   remove}. ⚠️ **This is a SNAPSHOT of current data, NOT a schema guarantee (Opus B2):** `ts` is NOT in
   the core's `validate_event` required set — a valid journal line can legally have no `ts`. So the helper
   must treat `ts` as optional (`.get("ts")`, skip falsy), never assume it's present. The literal string
   `"reconcile"` lives ONLY in the core's DB (`reconcile()`-synthesized rows), NEVER in the journal.
2. **The core's read model has no usable timestamp.** `current_present(conn)` returns only the 4-key
   tuple `(channel, message, emoji, user)`; the stored `updated_at` is `"reconcile"` on reconcile-written
   rows (D-11). That is why "since" must come from the journal, not the DB. **No core change is made.**
3. **`read_journal(path)` exists in the core and is ALL-OR-NOTHING (Opus B1, verified):** it parses the
   whole journal into validated event dicts, but **RAISES `ValueError` on the FIRST invalid-JSON line** —
   it does NOT skip bad lines. So reusing it (the design choice, for zero new parse surface) means a
   corrupt journal degrades to **all-blank "since"** (caught, INV-4), NOT per-line partial salvage. The
   spec commits to whole-file degrade (D-7a) and does not claim per-line skip.
4. **`report()` is a pure read-model join** (`tools/seed_triage.py:383`): for each seed map it computes
   `_verdict_for_message()` from `current_present(conn)` and appends `{seed_id, title, verdict, emoji}`.
   Adding "since" = one journal-derived lookup keyed by the same `(channel, message, emoji, user)` tuple.
5. **An existing invariant must be SUPERSEDED, not silently broken.** `test_report_row_has_no_fabricated_since`
   currently asserts the literal `"reconcile"` never appears in report output (the D-11 guard). v0.2b
   keeps that guarantee (real ISO ts, never the placeholder) but the column is no longer absent — see §3
   INV-3.

## 1. Summary & Goal

Add an honest, journal-derived **first-seen** time to each triage verdict in `seed_triage report`, so the
report doubles as a light prioritization view ("this ✅ has sat unbuilt for 8 days" vs "you just
reacted"). Strictly a **consumer-layer** change: a new pure helper that reads `reactions.jsonl`, two
field additions in `report()`, a `--journal` flag + render line. **No change to `reaction_state.py`**
(INV-1, the whole reason the core stays trustworthy).

**Semantic decision (Ace, 2026-06-30): FIRST-SEEN**, not last-touched. First-seen surfaces *stale
approvals* (the prioritization signal); last-touched would hide an old ✅ behind a recent re-react. On an
un-react→re-react, first-seen = the timestamp of the **current presence streak's** opening add (see §4
D-3 — a removal resets the streak, so first-seen is "when this *currently-present* reaction began," not
"the first time ever in history").

## 2. Non-Goals (explicitly NOT in v0.2b)

- **No core change.** `reaction_state.py` is untouched; no `reconcile(ts=)`, no `current_present` API
  change (INV-1). A need to touch the core = STOP + its own separate review.
- **No DB schema change**, no new persisted state. "Since" is *derived on read* from the journal.
- **No backfill of pre-journal reactions.** A verdict whose presence was captured ONLY by the REST poller
  (a gateway-down window, before the journal existed) legitimately has no journal evidence → its "since"
  is blank/`None`, never fabricated (§4 D-4).
- **No absolute-timestamp formatting policy beyond a relative age string + the raw ISO in `--json`.**
- **No new network calls.** `report` stays offline (INV-7 of the adapter PRD).
- **No "since" on the un-react path / no age for a removed reaction.** Only currently-present verdicts get
  an age (a removed reaction has no verdict to age).

## 3. Constitution / Invariants

- **INV-1 — Core consumed UNMODIFIED.** *Closeout proof:* `git diff` touches no line of
  `reaction_state.py`; `grep` shows `seed_triage` still only calls `reconcile`/`current_present`/`connect`/
  `read_journal` (read_journal is a pre-existing public read helper, not a new core surface).
- **INV-2 — "Since" is REAL or ABSENT, never fabricated.** The age is derived only from a real,
  **non-empty** ISO `ts` in the journal; a journal line with missing/empty `ts` (legal per the core
  schema, §0.1) is treated as no-evidence and SKIPPED, never set to `""` (Opus B2). If no journal evidence
  exists for a key, "since" is `None` and renders blank — the literal `"reconcile"` (or any placeholder)
  NEVER appears as a time. *Closeout proof:* `test_since_is_real_or_blank_never_reconcile` (DB-only verdict
  → `since=None`, output has no `reconcile`) **and** `test_since_skips_ts_missing_event` (a valid add with
  no `ts` → that key has no since, NOT `""`).
- **INV-3 — first-seen = the CURRENT presence streak's opening add (removal-reset semantics).** For a key
  that went add→remove→add, "since" is the **second** add's ts (the start of the streak that is currently
  present), NOT the original. A reaction that is currently *absent* has no "since". The walk is ordered by
  `seq`, not file position (Opus #4 — a concatenated/rotated journal can't silently mis-order a streak).
  *Closeout proof:* `test_since_first_seen_resets_on_remove`, `test_since_absent_key_has_none`,
  `test_first_seen_orders_by_seq_not_file_position`.
- **INV-4 — report stays OFFLINE and WHOLE-FILE-degraded-safe.** Reading the journal is a local file read;
  a missing/unreadable journal OR a corrupt line (which makes the core's all-or-nothing `read_journal`
  RAISE, §0.3) is caught and degrades to **all-blank "since"** — the rest of the report (the verdicts,
  which come from the DB, the source of truth for *what*) prints intact. There is NO per-line partial
  salvage (that would need a new parser; rejected for surface, D-7a). *Closeout proof:*
  `test_report_since_missing_journal_is_blank_not_crash`, `test_report_corrupt_journal_degrades_all_blank`
  (a garbage line → ALL ages blank, every verdict still printed, exit clean).
- **INV-5 — deterministic + JSON-stable two-field-always schema.** For a fixed journal + DB, the relative-
  age string varies with wall-clock but the underlying `since` ISO value is byte-stable. **`--json` ALWAYS
  emits a `since` key** (value = raw ISO string, or JSON `null` when no evidence) — a stable machine schema,
  NOT an omit-when-null shape (Opus #3). The text path renders a human relative age (blank when null).
  *Closeout proof:* `test_report_json_since_is_raw_iso`, `test_report_json_since_null_when_no_evidence`.

## 4. Resolved Decisions

- **D-1 — Source = the journal, read via the core's `read_journal()` (all-or-nothing, §0.3).** Reuse the
  existing validated parser; no new parsing code. The helper treats `ts` as **optional** (`.get("ts")`,
  skip falsy/missing — Opus B2) and **`str()`-coerces every key field exactly like the core's `_key()`**
  (reaction_state.py:84) so journal keys match `current_present`'s TEXT tuples even if a future line
  carries a non-string id (Opus #2). The journal path defaults to `~/.hermes/greenhouse/reactions.jsonl`
  (matching the gateway's `DISCORD_REACTION_JOURNAL`, env-name verified against the live adapter) and is
  overridable with `--journal PATH`. If the path is unset/absent, "since" is uniformly blank (INV-4).
- **D-2 — first-seen, not last-touched** (Ace, 2026-06-30). Prioritization wants "how long has this
  approval been waiting," which a re-react must not reset away.
- **D-3 — Removal-reset streak semantics (the un-react→re-react case).** Walk the journal in `seq` order;
  for each key track the ts of the add that *opened the currently-present streak* — i.e. reset the
  remembered first-seen to `None` on a `remove`, and set it (only if currently `None`) on an `add`. The
  final remembered value is "since" for keys that end present. This makes first-seen mean "since the
  reaction has been *continuously* present," which is the honest prioritization signal (an 8-day-old ✅
  that you briefly un-reacted and re-added yesterday is genuinely 1 day old as a standing approval).
  *(Rationale: a pure all-time-first would claim an approval is older than it has continuously been —
  misleading for "how long has this been waiting.")*
- **D-4 — No journal evidence → `None`, rendered blank.** A verdict present in the DB but with no
  matching journal add (REST-poller-only capture, or a key whose journal lines predate the file) gets
  `since=None`. The report shows the verdict with an empty age, never a guessed one. This is expected and
  honest, not a bug — surfaced plainly.
- **D-5 — Key match = the EXACT verdict emoji's (channel, message, emoji, user) tuple.** `report` already
  resolves a single highest-precedence verdict emoji per seed (❌>✅>👍); "since" is the first-seen of
  *that specific emoji's* presence streak, so the age matches the verdict shown (not some other emoji the
  user also pressed). *Closeout proof:* `test_since_matches_the_verdict_emoji_not_another`.
- **D-6 — Relative-age rendering is coarse + stdlib-only + UTC-safe.** `now - since` → `just now` /
  `Nm ago` / `Nh ago` / `Nd ago` (minutes/hours/days, integer floor). No third-party date lib. The journal
  `ts` is parsed as **UTC-aware** (`Z`→`+00:00`) and compared against a UTC-aware `now`, so no naive-vs-
  aware `datetime` subtraction can throw (Opus residual). The raw ISO is always available in `--json`.
  Negative/clock-skew (a `ts` in the future) clamps to `just now`.
- **D-7 — Performance: single linear pass, full-file read.** At 254 lines (and realistically hundreds to
  low-thousands), a full `read_journal` + one O(n) pass per `report` is trivially cheap. A tail/rotation
  optimization is a documented future trigger (NON-goal), not built now. The pass is O(events), memory
  O(distinct keys).
- **D-7a — Corrupt/missing journal → WHOLE-FILE degrade to all-blank (resolves Opus B1).** Because the
  core's `read_journal` is all-or-nothing (raises on the first bad line, §0.3), the reporter does NOT
  attempt per-line salvage (which would require a new line-tolerant parser — rejected to keep zero new
  parse surface, D-1). The journal load is wrapped: any `ValueError`/`OSError` → empty first-seen map →
  every row's `since=None`, the report's verdicts print intact. The honest failure mode is "all ages
  blank," not "partial ages." (This is the one AC the original draft contradicted; resolved here.)
- **D-8 — `--json` ALWAYS emits a `since` key (stable schema), resolving the AC-6 byte-identity tension
  (Opus #3).** The machine schema is two-field-stable: `since` is present on every row (raw ISO or JSON
  `null`). This is NOT byte-identical to v0.1 JSON (which had no `since` key) — so **AC-6's "inert" claim
  is scoped to: no *populated* age when `journal_path=None`** (the *text* output is unchanged for the
  no-journal default; the JSON gains a uniform `since:null`). A stable always-present key beats an
  omit-when-null shape that gives `--json` consumers two schemas. The default `report()` call
  (`journal_path=None`) yields all-`null` since, never a fabricated value.

## 5. Architecture / Design

```
  reactions.jsonl (gateway-produced, real ISO ts)
        │  read_journal()  [core, unchanged]
        ▼
  first_seen_map(events) ──► { (channel,message,emoji,user): iso_ts }   ← NEW pure helper, removal-reset
        │
  report(conn, seed_maps, titles, ace, window, *, journal_path)  ← +1 param (default the live journal)
        │   for each verdict row:  since = first_seen_map.get((ch, mid, verdict_emoji, ace))
        ▼
  row = { seed_id, title, verdict, emoji, since }   ← +1 field
        │
  text render: "... ✅ build it   <title>   (3d ago)"      |  --json: row.since = raw ISO (or null)
```

- **New pure function `first_seen_map(events) -> dict`** (~12 lines): walk events ordered by `seq`; per
  key, `remove` → drop the remembered first-seen, `add` → set it iff currently unset. Return the map of
  keys still "open" (present) to their streak-opening ts. Pure, no I/O, fully unit-testable.
- **`report()` gains `journal_path: str | None = None`**: loads events via `read_journal` (guarded —
  missing/corrupt → empty map, INV-4), builds the map once, and attaches `since` per row keyed on the
  verdict emoji (D-5). Default `None` keeps every existing caller's behavior identical until they pass a
  path → so the change is **inert for current callers** (the no-since contract holds unless `journal_path`
  is supplied; the CLI supplies it).
- **CLI `report`**: add `--journal PATH` (default `~/.hermes/greenhouse/reactions.jsonl`, or the
  `DISCORD_REACTION_JOURNAL` env if set, matching the gateway). Thread it into `report(...)`.

## 6. Implementation Phases

### Phase 1 — `first_seen_map()` pure helper (D-2/D-3, removal-reset)
*Ships:* `first_seen_map(events)` returning `{key: iso_ts}` for currently-present keys, streak-opening ts.
- *Unit:* `test_first_seen_simple_add` (single add → its ts), `test_first_seen_first_of_multiple_adds`
  (two adds same key, no remove → the EARLIER ts wins), **`test_since_first_seen_resets_on_remove`**
  (add@t1 → remove@t2 → add@t3 → `since=t3`, INV-3), **`test_since_absent_key_has_none`** (add@t1 →
  remove@t2, no re-add → key NOT in map), **`test_first_seen_orders_by_seq_not_file_position`** (events
  written to the journal OUT of seq order → the helper sorts by `seq` and still opens the streak at the
  right add, INV-3/Opus #4), **`test_first_seen_skips_ts_missing`** (an add with no `ts` key → not counted
  as evidence, INV-2/B2).
- *Verify with:* `python -m pytest tests/test_seed_triage.py -k "first_seen or resets_on_remove or absent_key or orders_by_seq or skips_ts" -q`.

### Phase 2 — thread `since` into `report()` (D-4/D-5, default-inert)
*Ships:* `report(..., journal_path=None)`; per-row `since` keyed on the verdict emoji; guarded journal load.
- *Unit:* `test_report_attaches_since_for_verdict` (a verdict whose emoji has a journal add → row carries
  that ts), **`test_since_matches_the_verdict_emoji_not_another`** (user pressed ✅@t1 AND 👍@t2; verdict
  is ✅ → since=t1, not t2), **`test_report_no_journal_path_is_inert`** (default `journal_path=None` →
  rows carry NO since / `since` absent-or-None, byte-identical to v0.1 for existing callers).
- *Negative/adversarial:* **`test_since_is_real_or_blank_never_reconcile`** (INV-2 — a DB verdict with no
  journal line → `since=None`, output has no `reconcile`), **`test_since_skips_ts_missing_event`** (INV-2/
  B2 — a valid add with no `ts` → key has no since, not `""`), **`test_report_since_missing_journal_is_blank_not_crash`**
  (journal path points at a nonexistent file → report still prints all verdicts, blank ages, exit clean),
  **`test_report_corrupt_journal_degrades_all_blank`** (D-7a — a garbage line → the core's `read_journal`
  raises → caught → ALL ages blank, every verdict still printed; NOT per-line salvage).
- *Verify with:* `python -m pytest tests/test_seed_triage.py -k "since or report_no_journal or corrupt or ts_missing" -q`.

### Phase 3 — render + `--json` + CLI `--journal` (D-6)
*Ships:* coarse relative-age renderer (`_relative_age(since, now)` → `just now`/`Nm`/`Nh`/`Nd ago`,
future-clamps to `just now`); text row appends `(<age>)` when `since` present; `--json` carries raw ISO
`since`; CLI `report --journal PATH` (default the live journal / `DISCORD_REACTION_JOURNAL`).
- *Unit:* `test_relative_age_buckets` (60s→just now, 90m→1h, 50h→2d, future→just now),
  **`test_report_json_since_is_raw_iso`** (INV-5 — `--json` row `since` is the exact ISO string, not the
  rendered age), `test_cli_report_journal_flag_default` (no `--journal` → uses the live default path).
- *E2E/integration:* **`test_report_since_end_to_end`** — a hand-built journal (add✅@old, a second seed
  add👍@recent) + matching seed maps + DB → `report(..., journal_path=tmp_journal)` shows the right
  per-row ages and the JSON carries raw ISO.
- *Verify with:* `python -m pytest tests/test_seed_triage.py -k "relative_age or json_since or report_since_end_to_end or journal_flag" -q`.

### Phase 4 — supersede the old absence-only guard
*Ships:* update `test_report_row_has_no_fabricated_since` → rename/retarget to INV-2's
`test_since_is_real_or_blank_never_reconcile` (the guarantee shifts from "no since column" to "since is
real-or-blank, never the placeholder"). Keep the `"reconcile" not in out` assertion (still true and still
load-bearing).
- *Verify with:* full suite `python -m pytest tests/test_seed_triage.py -q` green; `grep -n "since"
  tools/seed_triage.py` shows journal-derived only.

## 7. Determinism (honestly scoped)

`since` (the raw ISO) is deterministic for a fixed journal. The rendered *relative age* depends on
wall-clock `now`, so byte-identity is claimed for the `since` ISO column (and the JSON), NOT the rendered
age string. The first-seen map is deterministic for a fixed event set: the walk **sorts by `seq`** (not
file position — Opus #4), so a concatenated/rotated/out-of-order journal still yields the same streak
boundaries.

## 8. Security, Privacy, Ops

- **No new surface.** Local file read of an already-existing journal; no network, no creds, no new tool.
- **PII posture unchanged** (already-public Discord IDs + timestamps; no message content).
- **Rollback:** revert the one-file diff; or simply don't pass `--journal` (the feature is inert without
  a journal path, INV default-inert). No state to unwind.

## 9. Risks & Mitigations

- **R-1 — fabricated/placeholder age (the D-11 trap, the whole reason this is v0.2b).** *Mitigated:*
  INV-2 — `since` derives only from real journal ISO; absent → blank; the `"reconcile"` literal can never
  surface (it's not in the journal, and a DB-only key gets `None`).
- **R-2 — first-seen misread as all-time-first across an un-react.** *Mitigated:* D-3 removal-reset
  semantics + `test_since_first_seen_resets_on_remove`; documented in the helper docstring.
- **R-3 — journal corruption/absence crashes the report.** *Mitigated:* INV-4 guarded load; the report's
  *verdicts* come from the DB regardless, "since" degrades to blank.
- **R-4 — wrong emoji's age (user pressed multiple emoji).** *Mitigated:* D-5 keys on the resolved
  verdict emoji + `test_since_matches_the_verdict_emoji_not_another`.
- **R-5 — silently changing existing callers.** *Mitigated:* `journal_path` defaults to `None` → inert;
  `test_report_no_journal_path_is_inert` pins it.
- **R-6 — journal growth.** *Accepted, documented:* full-file pass is trivial at hundreds–low-thousands of
  lines; tail-read is a future trigger (D-7), not built.

## 10. Acceptance Criteria

- [ ] **AC-1 (real first-seen):** a ✅ with a journal add at T shows an age derived from T. Evidence:
  `test_report_attaches_since_for_verdict`.
- [ ] **AC-2 (removal-reset first-seen):** add→remove→add shows the *re-add's* time, not the original.
  Evidence: `test_since_first_seen_resets_on_remove`.
- [ ] **AC-3 (never fabricated):** a DB-only verdict (no journal line) shows blank, never `reconcile`.
  Evidence: `test_since_is_real_or_blank_never_reconcile`.
- [ ] **AC-4 (right emoji):** with ✅ and 👍 both pressed, the ✅ verdict's age is the ✅'s first-seen.
  Evidence: `test_since_matches_the_verdict_emoji_not_another`.
- [ ] **AC-5 (whole-file degraded-safe):** missing OR corrupt journal → report prints all verdicts with
  ALL ages blank, no crash, exit clean (no per-line salvage). Evidence:
  `test_report_since_missing_journal_is_blank_not_crash`, `test_report_corrupt_journal_degrades_all_blank`.
- [ ] **AC-6 (default-inert TEXT):** `report()` with no `journal_path` produces TEXT output byte-identical
  to v0.1 (no age rendered) and a `--json` `since` that is uniformly `null` (never populated/fabricated).
  Evidence: `test_report_no_journal_path_is_inert`.
- [ ] **AC-7 (JSON stable raw-ISO-or-null):** `--json` ALWAYS carries a `since` key = exact ISO string or
  JSON `null`. Evidence: `test_report_json_since_is_raw_iso`, `test_report_json_since_null_when_no_evidence`.
- [ ] **AC-8 (core untouched):** `git diff` shows no change to `reaction_state.py`. Evidence: diff + grep.
- [ ] **AC-9 (live proof):** `report --journal <live reactions.jsonl>` against the real DB shows real
  first-seen ages on real reactions (incl. Ace's ✅ on the gh-2026-06-27 card → its real first-seen).
  Evidence: a live transcript captured at closeout. **Pre-ship coverage check (§0.5):** report what
  fraction of currently-present verdicts get a real `since` vs blank, so the prioritization column isn't
  silently hollow.

## 11. Reversibility

- **Inert by default** (`journal_path=None`), trivially reverted (one-file diff), no persisted state, core
  untouched. Removing the `--journal` flag / the helper removes the feature entirely.

---

## Roadmap note
This is the v0.2b row from `seed_triage_adapter_prd.md`, redirected per Ace's decision: **journal-derived
in the reporter, NEVER a core `reconcile(ts=)` change.** If verdict-age is ever wanted absolutely (not
relative) or across pre-journal history, that's a separate consideration — but the no-core-change
constraint stands.

---

## Review Log
- **Pass-1 (Opus, claude-bpp): BLOCK → folded (all 2 blockers + 2 required changes + residuals).** Each
  empirically re-verified against the real core before folding:
  - **B1 (corrupt-line AC self-contradiction):** `read_journal` is all-or-nothing — RAISES on the first
    bad line (verified: `ValueError line 2: invalid JSON`), it does NOT skip. The original
    `test_report_since_corrupt_line_skipped` ("the rest still produce ages") was impossible while reusing
    the core parser. **Resolved Option A** (whole-file degrade, keep zero new parse surface): §0.3 + D-7a +
    INV-4 now state corrupt→all-blank; test renamed `test_report_corrupt_journal_degrades_all_blank`.
  - **B2 (`ts` not in schema):** `validate_event` does NOT require `ts` (verified: a ts-less add is
    accepted; `ev.get("ts")`→`None`). A naive `ev["ts"]` would KeyError or set `""` (the D-11 fabrication
    trap). **Resolved:** D-1 + INV-2 — helper uses `.get("ts")`, skips falsy, `str()`-coerces key fields
    like the core's `_key()`; new tests `test_since_skips_ts_missing_event` / `test_first_seen_skips_ts_missing`.
  - **#3 (JSON byte-identity vs `since:null`):** **Resolved D-8** — `--json` ALWAYS emits `since`
    (raw ISO or `null`); AC-6 scoped to "no *populated* age + unchanged TEXT" rather than byte-identical JSON.
  - **#4 (file-order ≠ seq-order):** **Resolved** — the walk sorts by `seq`; §7 + INV-3 +
    `test_first_seen_orders_by_seq_not_file_position`.
  - **Residuals folded:** UTC-aware datetime parse (D-6, no naive/aware throw); env-name
    `DISCORD_REACTION_JOURNAL` verified against the live adapter; blank-coverage measurement added as a
    pre-ship check (AC-9/§0.5) so the column isn't hollow; docstring will say "since the journal's earliest
    retained evidence," not "since first ever."
  - **Verified before fold (live):** B1 raises, B2 accepts ts-less, and a live coverage probe (journal-only)
    showed Ace's currently-present ✅ DOES get a real first-seen — so the feature is not hollow for the
    real-time-captured path (REST-reconcile-only keys correctly blank, D-4).
- **Convergence:** all blockers resolved consumer-side, no core change (INV-1 intact); single pass
  sufficient for a small consumer-only feature whose core-change risk was designed out. Status → APPROVED,
  cleared for build.
