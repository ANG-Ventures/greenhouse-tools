# PRD / Spec — `seed_triage`: Discord-reaction → durable triage state adapter

**Repo:** `ANG-Ventures/greenhouse-tools` · **Lands in:** `tools/seed_triage.py` + `tests/test_seed_triage.py`
**Status:** APPROVED v0.3 (folded Opus pass-1 BLOCK + pass-2; pass-2 returned one APPROVE-WITH-CHANGES and one BLOCK — the BLOCK was confirmed EMPIRICALLY against the core and is folded below) · **Owner:** Apollo · **Python:** 3.11, **stdlib-only** (adapter may use `urllib`/`fcntl`; the *core* stays import-restricted)
**Consumes:** the already-built, 37-tests-green `tools/reaction_state.py` durable-state core (public `reconcile()`/`current_present()`/`connect()`).
**Seed:** "Discord reaction UX for agent triage boards" — the named first consumer of the durable-state core.

---

## 0. Ground-Truth (measured live before the design — read first)

Every fact below was probed against the live fleet and the actual core source, not assumed. Several
**falsified the original spec's assumed architecture** (a `discord.py` gateway listener), which is why
this looks different from the core PRD's "future gateway adapter" sketch.

1. **The durable-state core is real and its contract is fixed.** `tools/reaction_state.py` stdlib-only, 37
   tests pass. Public functions this adapter calls: `connect(db)`, `reconcile(conn, snapshot, *,
   allow_removes)`, `current_present(conn)`. Snapshot it consumes:
   ```json
   { "watermark": <int>, "covers_removes": <bool>,
     "reactions": [ {"channel_id","message_id","emoji","user_id"}, ... ] }
   ```
   **This adapter does not modify the core.**

2. **CORE BEHAVIOR, read from source (settles the pass-1 open questions):**
   - `reconcile`'s remove pass is **GLOBAL**: `present = current_present(conn)` returns **every** durable
     present key in the whole DB, and it synthesizes a remove for **every** key in `present − snap_keys`
     when eligible. **A key absent from the snapshot for *any* reason — including a fetch that failed — is
     removed.** (This is the root of CB-1; the fix is §4 D-6/D-9.)
   - `apply_event` on a stale event (`seq <= stored seq`) does `ROLLBACK` and returns `"stale"` — **no row
     write, no ledger append.** A re-poll of *unchanged* state is a true no-op because the set-diff is
     empty (the key is in both `snap_keys` and `present`, so it's in neither the add nor the remove pass).
   - **⚠️ The watermark guard IS load-bearing — do NOT treat it as vestigial (pass-2 BLOCK, confirmed
     empirically against the core).** The earlier draft claimed the guard was "inert / not load-bearing."
     That is FALSE for the **un-react → re-react** cycle, and a plausible misimplementation of the
     watermark silently loses Ace's verdict. Proof (run live against `reaction_state.py`): a card goes
     present (poll 1) → Ace un-reacts, removed on a clean poll (poll 2, `present=0`, `seq=S`) → Ace
     **re-reacts** (poll 3). The re-react synthesizes an `ADD` stamped at `next_watermark`. If
     `next_watermark` is computed as `MAX(seq)+1` **over ALL rows** (Variant A), the new ADD's seq `> S`,
     beats the stale guard, and the verdict re-appears → `(present, gone, present)` ✅. If `next_watermark`
     is computed as `MAX(seq) WHERE present=1 +1` (Variant B — a *plausible* misreading, since
     `current_present` itself filters `present=1`), the removed row at `seq=S` is invisible to the `MAX`,
     so the re-react's ADD lands at `seq <= S` → the core returns `"stale"` → **Ace re-reacts ✅ and
     `report` shows NO verdict. Silent, durable, wrong.** Measured: Variant A → `(True, False, True)`;
     Variant B → `(True, False, False)`. **Contract (D-3, hard): `next_watermark` MUST be
     `COALESCE(MAX(seq),0)+1` over the FULL `reaction_state` table (no `present=1` filter).** The watermark
     strictly exceeding every stored seq is a property the implementation must MAINTAIN, not an inert
     accident — and `test_unreact_then_rereact_shows_verdict_again` (AC-3b) is its load-bearing gate.

3. **The bot is ALREADY on the gateway — a second gateway connection is a hazard, not a feature.** The
   greenhouse `DISCORD_BOT_TOKEN` decodes to bot-user-id `1502226398813618176`, and the live Hermes
   gateway's `discord:` config has `reactions: true` for that same identity. Discord permits **one**
   gateway session per bot token; a standalone `discord.py` listener would fight the live gateway.
   **Therefore v0.1 is a stateless REST poller, not a gateway listener.** No websocket, no `discord.py`,
   no journal.

4. **Discord REST already exposes exactly the snapshot the core wants.** `GET
   /channels/{c}/messages/{m}/reactions/{emoji}` returns the **list of users** who reacted with that
   emoji — an authoritative enumeration. That *is* the core's snapshot, fetched over stdlib `urllib`
   exactly as greenhouse's own poster (`lib/greenhouse_run.py:_api`) already calls the Bot API.

5. **The seed→message mapping already exists.** Greenhouse writes `seeds/<day>/seed_messages.json`:
   `{ "main_message_id", "channel_id", "seed_messages": { "<seed_id>": {"channel_id","message_id"} } }`,
   and **pre-seeds ✅/👍/❌** on every seed card (`_post_seed_thread`). Cards are reaction-ready and
   message↔seed is known.

6. **The bot's own pre-seeded reactions must be filtered out.** Bot pre-adds all three emoji (count = 1).
   Ace's tap makes count = 2 and adds his user-id to the emoji's user list. **Triage signal = a reaction
   by Ace's user-id (`117431298246705156`) only.** The bot's reactions and other members' are NOT triage
   state.

### What this dissolves
The core PRD's biggest residual risk — **"how does the adapter correlate a REST snapshot to a gateway-
stream seq?"** — does not exist here: there is no gateway stream, so nothing to correlate. The watermark
is a per-poll monotonic counter derived from the DB (`COALESCE(MAX(seq),0)+1` over ALL rows); its guard
is **load-bearing for the un-react→re-react cycle** (§0.2 ⚠️), so the implementation must maintain strict
monotonicity. The hard unbuilt-producer problem is designed out, not deferred.

---

## 1. Summary & Goal
Build `tools/seed_triage.py`: a stdlib-only tool that turns **Ace's reactions on greenhouse seed cards**
into **durable, queryable triage state**, by (a) reading the existing `seed_messages.json` maps, (b)
polling each message's reactions over Discord REST (paginated, emoji-encoded), (c) filtering to Ace's
user-id, (d) building the core's watermarked snapshot and folding it via the unmodified `reconcile()`,
(e) printing/exporting the per-seed verdict (`✅ build · 👍 interesting · ❌ not for me · — no verdict`).

**The visible example (what Ace asked for):** Ace reacts ✅ on a seed card → runs `seed_triage report`
(or nightly) → sees a durable verdict list: *"gh-2026-06-30-#2 → ✅ build it (Ace)."* State survives
restarts and is queryable by a future promotion step.

## 2. Non-Goals (explicitly NOT in v0.1)
- **No gateway / websocket / `discord.py` / real-time push.** REST poll only (§0.3).
- **No journal (`reactions.jsonl`).** Drive the core purely through the snapshot/`reconcile()` path.
- **No write-back into greenhouse.** Promoting an ✅'d seed is the first edit to greenhouse's live path →
  **its own version + own review** (roadmap v0.2). v0.1 reads `seed_messages.json` + Discord, writes only
  its own DB.
- **No emoji→action policy beyond ✅/👍/❌.** Custom (`name:id`) emoji out of scope.
- **No multi-user triage.** Only Ace's user-id is a signal.
- **No modification of `reaction_state.py`.** A need to change the core = STOP + separate review.

## 3. Constitution / Invariants

- **INV-1 — Core consumed unmodified.** *Closeout proof:* `git diff` touches no line of
  `reaction_state.py`; `grep` shows `seed_triage` imports & calls `reconcile`/`current_present`/`connect`.
- **INV-2 — Triage signal is Ace-only.** A reaction counts iff its user-id == configured Ace id; the
  bot's pre-seeds and other members never enter the snapshot. *Proof:* `test_bot_preseed_is_not_triage`,
  `test_other_member_reaction_ignored`.
- **INV-3 — Removes require PROVEN-COMPLETE global coverage (no mass-remove, no fetch-fail delete).**
  `covers_removes=True` (the only condition under which the core deletes on absence) is set **iff** the
  poll **successfully and completely enumerated every durable present key's message** — i.e. (a) the
  target set ⊇ every message with a durable present key in the DB, AND (b) *every* per-message,
  per-emoji, fully-paginated fetch succeeded. **Any** fetch failure, or a target set that doesn't cover
  all durable keys → `covers_removes=False` + `allow_removes=False` (add-only) + a loud report line. A
  transient 404/timeout can never delete a verdict. *Proof:* `test_partial_fetch_failure_does_not_remove`,
  `test_remove_scope_requires_all_durable_keys`, `test_unreact_flips_verdict_to_none` (full clean poll).
- **INV-4 — Re-poll of unchanged state mutates nothing; the un-react→re-react cycle recovers the verdict.**
  An unchanged present key produces no ledger row and no `seq` change (empty set-diff; §0.2). Separately,
  the watermark's strict monotonicity (D-3) guarantees a re-react after an un-react re-establishes the
  verdict (NOT a silent `stale`). *Proof:* `test_repoll_unchanged_no_db_mutation` (ledger row-count +
  per-key `seq` byte-identical after a 2nd identical poll) AND `test_unreact_then_rereact_shows_verdict_again`
  (AC-3b — present → un-react clears → re-react shows the verdict again).
- **INV-5 — No state outside the adapter's own DB + read-only inputs.** Writes ONLY `seed_triage.db`;
  `seed_messages.json` + Discord read-only; deleting the DB resets triage. *Proof:* `test_no_writes_outside_db`
  (+ assert `seed_messages.json` mtime unchanged).
- **INV-6 — Secret hygiene.** Token from `DISCORD_BOT_TOKEN` at call time; never logged/persisted.
  *Proof:* `grep` no token literal; `test_token_never_in_output`.
- **INV-7 — `report` (offline) ≠ `poll` (real liveness), and failure PAGES.** `report` reads the DB,
  never networks. `poll` hits real Discord and **fails loud + non-zero** on unreachable API / unreadable
  maps. The nightly entry propagates `poll`'s non-zero and routes it to `fleet_alert` (stdout pages no
  one). *Proof:* `test_report_works_offline`, `test_poll_fails_loud_on_unreachable_api`, and the §6 entry
  uses `set -euo pipefail` + `|| fleet_alert`.
- **INV-8 — seed-map schema is guarded, not assumed.** The adapter validates each `seed_messages.json`
  carries the expected `seed_messages.{id}.{channel_id,message_id}` shape; a malformed/renamed map is
  **skipped with a loud warning**, never a silent empty result. *Proof:* `test_seed_map_schema_drift_is_loud`.
- **INV-9 — Single-writer lock via `fcntl.flock` (auto-released on process death).** `poll` takes an
  exclusive **`fcntl.flock(LOCK_EX|LOCK_NB)`** on `seed_triage.db.lock` so an ad-hoc `--message` poll
  can't interleave with the nightly poll (two `MAX(seq)+1` writers). The lock MUST be `flock` (or
  `O_CREAT|O_EXCL`+pid/TTL stale-reaping), **NEVER a bare presence file** — a presence file is TOCTOU-racy
  AND non-self-releasing, so one SIGKILL'd poll wedges every future nightly into `BUSY=3` + a recurring
  page forever (pass-2 CB-A). With `flock`, a crashed poll's lock auto-releases on fd close / process
  death. A second concurrent `poll` exits non-zero `BUSY`, not a partial write. *Proof:*
  `test_second_poll_is_busy_not_partial`, `test_lock_auto_releases_after_holder_exits` (acquire in a
  subprocess, let it die, assert the next `poll` acquires cleanly — no manual unlink).

## 4. Resolved Decisions

- **D-1 — Architecture = stateless REST snapshot poller** (forced by §0.3). REST is the snapshot source;
  unmodified `reconcile()` is the sink.
- **D-2 — Snapshot path only, no journal** (§2). `reconcile` is add-only + watermarked + tested; correct
  durable state, not millisecond latency, is the requirement.
- **D-3 — Watermark = `COALESCE(MAX(seq),0)+1` over the FULL `reaction_state` table per poll** (DB-derived,
  never wall-clock → no skew regression; **no `present=1` filter** — that filter is the Variant-B silent
  re-react bug, §0.2 ⚠️). The watermark guard IS load-bearing for the un-react→re-react cycle: the
  watermark must strictly exceed every stored seq (present OR removed) so a re-react's synthesized ADD
  beats the prior REMOVE's seq. INV-4 asserts the unchanged-repoll no-op (empty set-diff); AC-3b asserts
  the re-react recovery (the guard's real job). On an empty DB the watermark is `1`
  (`COALESCE(MAX(seq),0)+1`), pinned by `test_next_watermark_empty_db_is_one`.
- **D-4 — Target message set = (every distinct message_id with a present key in the DB) ∪ (messages in
  seed maps newer than `--window-days`, default 14).** The DB-union half is mandatory for INV-3
  completeness; the window half bounds new-card cost.
- **D-5 — Always fetch the three triage emoji's user lists; NO count-gate as a correctness mechanism.**
  (Folds CB-2.) The old `count≥2` gate silently dropped a lone `count==1` Ace reaction if greenhouse ever
  changed its pre-seed. Correctness now does **not** depend on greenhouse's pre-seed: for each target
  message we fetch `reactions/{emoji}` for each of ✅/👍/❌ (3 GETs/message), fully paginated, and filter
  to Ace. The reaction-count summary is used ONLY as a cheap *skip-empty* hint that is **safe by
  construction** (skip an emoji only when its count is 0 — no user could be there); it never skips a
  count≥1 emoji.
- **D-6 — `allow_removes` is enabled ONLY when INV-3's proven-complete condition holds for THIS poll.**
  A full nightly poll over a clean fetch → `allow_removes=True`, so an un-react flips a verdict back to
  none. Any per-message fetch failure, or an ad-hoc `--message` poll (which can't cover all durable keys)
  → add-only, reported. Safety beats convenience; the unsafe combo is refused, not silently run.
- **D-7 — Verdict precedence (multi-emoji):** **❌ > ✅ > 👍** (a "no" is the strongest signal; explicit
  build beats soft "interesting"). Surfaced in the report, never silently collapsed.
- **D-8 — Pagination + emoji-encoding (folds two REST footguns).** `reactions/{emoji}` returns ≤100
  users/page; the adapter paginates via `after=` until a short page, so >100 reactors can't hide Ace. The
  emoji is percent-encoded in the path (`urllib.parse.quote`), exactly as greenhouse's poster does.
- **D-9 — `report` scans ALL seed maps (not the 14-day poll window) for message→seed labels.** A real
  verdict on an old card must still resolve its `seed_id`; a durable key whose message is in no map is
  labeled `unknown-seed`, never dropped.
- **D-10 — Config is flag/env, cwd-decoupled, and IDENTITIES ARE VISIBLE.** `--db`, `--seeds-dir`
  (default `~/.hermes/greenhouse/seeds`), `--ace-user-id` (default `DISCORD_TRIAGE_USER_ID` env → known
  id), `--window-days` (14). Token from `DISCORD_BOT_TOKEN`. **`report` and `--check-target` print the
  configured `ace_user_id` and bot-id** so a mis-set id is visible, not silently wrong. Nightly entry
  passes absolute paths.
- **D-11 — NO "since" timestamp in v0.1 (folds pass-2 CB-B — the fabricated-value trap).** The core's
  `reconcile()` hardcodes `ts="reconcile"` (reaction_state.py L254/L267) and exposes **no `ts` parameter**
  (L207); `current_present()` returns no timestamp. So every adapter-written row's `updated_at` is the
  **literal string `"reconcile"`**, not a time. Surfacing it as a "since" column would render `"reconcile"`
  as a timestamp — exactly the demo-as-live fabrication Ace rejects. Adding a real time source = a `ts`
  arg on `reconcile` = a **core modification**, which INV-1/§2 forbid (→ STOP + separate review). **v0.1
  report row is `seed_id · title · verdict` — no "since".** A real first-seen time is a scoped core change
  deferred to roadmap. *Proof:* `test_report_row_has_no_fabricated_since` (assert the literal `"reconcile"`
  never appears in report output).
- **D-12 — `--window-days` is an explicit CAPTURE HORIZON, surfaced, not a silent miss (folds pass-2
  required change).** Target set = (messages with a present key) ∪ (seed maps newer than `--window-days`).
  A card Ace reacts to *after* it ages past the window has no present key and is no longer polled → his
  reaction would be silently uncaptured. `report` therefore **surfaces "N un-triaged cards aged out of
  the N-day capture horizon"** (seed maps older than the window with zero durable verdict), so an aged-out
  card is visible, never a silent loss. The window is a documented knob, not a hidden cliff. *Proof:*
  `test_report_surfaces_aged_out_untriaged_cards`.
- **D-13 — CHRONIC `covers_removes=False` is alerted, not silently tolerated (folds pass-2 required
  change).** The core's remove eligibility is a single *global binary* flag; the adapter can't scope
  removes per-message without modifying the core. So one permanently-unreachable durable-key message
  (deleted channel, bot kicked) would force `covers_removes=False` on *every* future nightly, silently
  disabling un-react removal (AC-3) DB-wide. A full nightly `poll` that yields `covers_removes=False`
  **emits a loud `COVERS_REMOVES_DISABLED` line + non-zero advisory** naming the offending dead key(s),
  and the documented remediation is purging the dead key from the DB. Silence is the bug, not the
  fail-safe. *Proof:* `test_full_poll_covers_removes_false_alerts_with_dead_key`.
- **D-14 — `seed_triage --check-target` is REST-liveness, NOT the core's journal probe (folds pass-2
  CB-C flag collision).** The core ships `check_target(journal_path)` asserting a `reactions.jsonl`
  exists — but this adapter has **no journal** (§2). `seed_triage`'s `--check-target` is a *different*
  contract under the same flag name: it asserts the token is present AND a real `GET /users/@me` succeeds.
  The adapter is a separate file and owns its own flag; it MUST NOT call `reaction_state.check_target`.
  *Proof:* `grep` shows `seed_triage` never references the core's `check_target`; `test_check_target_is_rest_liveness`.
- **D-15 — REST budget restated honestly (folds pass-2 minor).** D-5's "skip a 0-count emoji" hint needs
  the per-message count summary, which comes free in the list response the target-builder already fetches
  (`GET /channels/{c}/messages`), NOT a per-message extra GET. Worst case (no list summary available):
  3 emoji × paginated per target message. With the D-4/D-12 window the target set is bounded to the
  active card set (tens, not thousands), so the ceiling stays ≲ a few hundred calls/night, well under
  Discord's limits; 429 `Retry-After` is honored (D-8). The budget is a soft ceiling, not a correctness
  dependency.

## 5. Architecture / Design

```
  greenhouse seeds/<day>/seed_messages.json     Discord REST  (Bot token, urllib, stdlib)
        (seed_id -> channel,message)  [schema-guarded INV-8]      │
                 │                                                 │ GET /channels/{c}/messages/{m}/reactions/{emoji}
                 ▼                                                 │   (3 emoji × paginated × encoded — D-5/D-8)
        ┌──────────────── tools/seed_triage.py ────────────────────┼──────────────────────────────────────────────┐
        │  build_targets() ─ D-4 (DB-union ∪ active maps)          ▼                                                │
        │  poll() ─ fetch reactions ─ filter to Ace (INV-2) ─ track per-message fetch success ─► snapshot{          │
        │            watermark=COALESCE(MAX(seq),0)+1 over ALL rows (D-3 ⚠️), reactions:[…],                         │
        │            covers_removes = ALL durable-key msgs fully+successfully enumerated (INV-3) }                  │
        │                                   │                                                                       │
        │       reaction_state.reconcile(conn, snapshot, allow_removes = covers_removes) ◄──── UNMODIFIED core, INV-1│
        │                                   │                                                                       │
        │                                   ▼                                                                       │
        │                           seed_triage.db  (core schema)   [flock single-writer, INV-9]                    │
        │  report() ─ current_present(conn) ⨯ ALL seed maps (D-9) ⨯ D-7 precedence ─► per-seed verdict table        │
        └───────────────────────────────────────────────────────────────────────────────────────────────────────┘

  CLI:  poll [--message C/M]   |   report [--json]   |   --check-target   |   --selfcheck
```

**Read-model (`report`):** join `current_present(conn)` (Ace-filtered at poll time) against ALL seed maps
(D-9), apply D-7 precedence per seed, print `seed_id · title · verdict` (**NO "since" — D-11**, the core
exposes no real timestamp on the reconcile path), prefaced by the configured `ace_user_id`/bot-id (D-10),
and append the D-12 "N un-triaged cards aged out of the capture horizon" advisory. `--json` for a future
promoter.

## 6. Implementation Phases

### Phase 1 — REST client: pagination, emoji-encoding, rate-limits (D-8)
*Ships:* `_api(method, path)` (urllib, `Authorization: Bot`, 429 `Retry-After` honored), `reaction_users(c,m,emoji)` paginating `after=` until a <100 page, percent-encoding the emoji.
- *Unit:* `test_api_builds_bot_auth_header`, `test_429_retry_after_honored`, **`test_emoji_path_encoding`** (✅/👍/❌ percent-encoded), **`test_reaction_pagination_over_100`** (mock 2 pages → Ace on page 2 is found).
- *E2E/integration:* `test_poll_one_real_seed_message` — real seed message id from a real `seed_messages.json`; assert structure (user list incl. bot). Skip-with-loud-reason if token absent (never silent green).
- *Negative/adversarial:* `test_api_unreachable_is_loud` (raises the **real** `urllib.error.URLError`, not a custom sentinel), `test_malformed_reaction_json_rejected`, **`test_token_never_in_output_on_401`** (a `urllib.error.HTTPError` 401 whose repr could leak the request → assert the token literal never appears in the error path, INV-6).
- *Verify with:* `python -m pytest tests/test_seed_triage.py -k "api or encoding or pagination or poll_one or unreachable or token" -q`.

### Phase 2 — Ace-filter + snapshot build + fetch-success tracking (INV-2, INV-3)
*Ships:* `build_snapshot(targets, ace_user_id, watermark)` → fetches all 3 triage emoji per message (D-5), keeps ONLY Ace, and returns `(snapshot, fetch_ok_per_message)` so the caller can compute `covers_removes`.
- *Unit:* `test_snapshot_keeps_only_ace`, `test_snapshot_shape_matches_core`, `test_count_zero_emoji_skipped_safely` (D-5: a 0-count emoji is skipped; a 1-count Ace emoji is NEVER skipped).
- *Negative/adversarial:* **`test_bot_preseed_is_not_triage`**, **`test_other_member_reaction_ignored`**, **`test_fetch_failure_marks_message_incomplete`** (one emoji GET raises → that message flagged not-fully-enumerated).
- *Verify with:* `python -m pytest tests/test_seed_triage.py -k "snapshot or preseed or other_member or fetch_failure or count_zero" -q`.

### Phase 3 — Targets + watermark + SAFE reconcile fold (INV-3/4/9, D-3/D-4/D-6)
*Ships:* `build_targets(conn, seeds_dir, window_days)`, `next_watermark(conn)` (`COALESCE(MAX(seq),0)+1` over ALL rows — D-3 ⚠️), `poll(...)` that builds the snapshot, computes `covers_removes` per INV-3, takes the **`fcntl.flock`** INV-9 lock, and calls the **unmodified** `reaction_state.reconcile`.
- *Unit:* `test_targets_union_db_and_active_maps`, `test_repoll_unchanged_no_db_mutation` (INV-4: ledger count + per-key seq byte-identical), `test_covers_removes_true_only_on_complete_clean_poll`, **`test_next_watermark_empty_db_is_one`** (D-3), **`test_next_watermark_counts_removed_rows`** (a `present=0` row at the current max seq IS counted — the Variant-B guard).
- *E2E/integration:* `test_poll_reconcile_end_to_end` — mocked REST where Ace ✅ one card → `current_present` shows exactly that key, `report` shows ✅; then mock Ace removing ✅ on a full clean poll → **`test_unreact_flips_verdict_to_none`**; then **`test_unreact_then_rereact_shows_verdict_again`** (AC-3b — Ace re-reacts ✅ on a 3rd poll → the verdict RE-APPEARS, NOT a silent `stale`; this is the pass-2 BLOCK's load-bearing gate).
- *Negative/adversarial:* **`test_partial_fetch_failure_does_not_remove`** (DB has ✅ for card A; next poll, card A's emoji GET raises a real `urllib.error.HTTPError` 404 → `covers_removes=False` → A's ✅ SURVIVES), **`test_remove_scope_requires_all_durable_keys`** (`--message`-style narrow poll → add-only, other durable keys untouched), **`test_covers_removes_false_over_random_fetch_failure_subsets`** (property/parametrized: for many random subsets of failed fetches + narrow target sets, `covers_removes` is ALWAYS False and NO durable key is removed — the safety-critical completeness fn can't be fooled), **`test_second_poll_is_busy_not_partial`** + **`test_lock_auto_releases_after_holder_exits`** (INV-9 flock).
- *Verify with:* `python -m pytest tests/test_seed_triage.py -k "targets or watermark or covers or reconcile or unreact or rereact or partial or remove_scope or busy or lock" -q`.

### Phase 4 — `report` read-model: all-maps scan + precedence + visible identity (D-7/9/10/11/12)
*Ships:* `report(conn, seeds_dir, as_json)` joining present keys ⨯ ALL seed maps ⨯ precedence, prefaced by configured ids, with NO fabricated "since" (D-11) and the D-12 aged-out advisory.
- *Unit:* `test_report_maps_message_to_seed`, **`test_verdict_precedence_x_beats_check_beats_thumb`** (assert the case where ✅ AND 👍 are BOTH durably present on the same card *simultaneously* → ✅ wins; precedence only matters with multiple present at once), `test_report_json_shape`, `test_report_prints_configured_ace_id` (D-10), **`test_report_row_has_no_fabricated_since`** (the literal `"reconcile"` never appears in output — D-11).
- *E2E/integration:* `test_report_works_offline` (no token/network → exit 0; proves INV-7).
- *Negative/adversarial:* `test_report_seed_with_no_reaction_shows_none`, `test_report_orphan_key_labeled_unknown_seed` (D-9), `test_seed_map_schema_drift_is_loud` (INV-8), **`test_report_surfaces_aged_out_untriaged_cards`** (a seed map older than `--window-days` with zero durable verdict → report surfaces "N aged out of capture horizon" — D-12, not a silent miss).
- *Verify with:* `python -m pytest tests/test_seed_triage.py -k "report or precedence or schema_drift or since or aged_out" -q`.

### Phase 5 — `--check-target` (REST liveness, visible ids) ≠ `--selfcheck` (offline) (INV-7/10, D-14)
*Ships:* `--check-target` asserts the token is present AND a real `GET /users/@me` succeeds, printing the bot-id + configured ace-id → loud non-zero otherwise; `--selfcheck` runs an offline mocked poll→reconcile→report over a self-built fixture and asserts the verdict, touching NO network. **`seed_triage --check-target` is REST-liveness, NOT the core's journal `check_target` (D-14).**
- *Unit:* `test_selfcheck_offline_passes`, `test_check_target_fails_without_token`, **`test_check_target_is_rest_liveness`** (grep/AST: `seed_triage` never calls `reaction_state.check_target`; the flag hits `GET /users/@me`, not a journal file — D-14).
- *Negative/adversarial:* `test_selfcheck_does_not_require_token`, `test_check_target_fails_on_api_error`.
- *Verify with:* `python tools/seed_triage.py --selfcheck` → `SELFCHECK OK` exit 0; `--check-target` exit 0 only on a real reachable token.

### Phase 6 — CLI + nightly entry (FAILURE PAGES) + observability (INV-7, CB-3 fold, D-13)
*Ships:* `argparse` (`poll`, `report`, `--check-target`, `--selfcheck`). Exit codes: 0 ok, 1 logic, 2 liveness/usage, 3 BUSY. `poll` prints `targets=N polled=M fetch_fail=F ace_reactions=K added=A removed=R covers_removes=<bool>`; a poll that reaches 0 reachable targets when DB/maps are non-empty exits non-zero (`POLL ANOMALY`); a **full** poll yielding `covers_removes=False` emits `COVERS_REMOVES_DISABLED` naming the dead key(s) (D-13).
- *Unit:* `test_cli_dispatch`, `test_poll_anomaly_on_zero_reachable`, **`test_full_poll_covers_removes_false_alerts_with_dead_key`** (D-13).
- *E2E/integration:* `test_cli_poll_then_report_end_to_end` over the mocked layer.
- *Nightly entry (ABSOLUTE paths incl. `$T`; liveness first; EVERY step pages via fleet_alert; safe removes; runs AFTER greenhouse posts today's `seed_messages.json`):*
  ```bash
  set -euo pipefail
  REPO=/Users/alexgierczyk/.hermes/greenhouse/worktrees/.../  # absolute repo root
  D=/Users/alexgierczyk/.hermes/greenhouse/seed_triage.db
  S=/Users/alexgierczyk/.hermes/greenhouse/seeds
  T="$REPO/tools/seed_triage.py"   # ABSOLUTE — never relative (pass-2: cron cwd ≠ repo root)
  python "$T" --check-target               || { fleet_alert warn seed-triage "check-target failed";        exit 2; }
  python "$T" poll   --db "$D" --seeds-dir "$S" --window-days 14 \
      || { rc=$?; fleet_alert warn seed-triage "poll failed rc=$rc"; exit $rc; }
  python "$T" report --db "$D" --seeds-dir "$S" \
      || { rc=$?; fleet_alert warn seed-triage "report failed rc=$rc"; exit $rc; }
  ```
- *Verify with:* the nightly entry runs clean against the live channel (the real-Discord proof, §10 AC-7).

## 7. Determinism (honestly scoped)
Read-model + verdict deterministic for a fixed DB + fixed seed maps. D-7 is a total order; report ordering
pinned by `ORDER BY seed_id`. The poll snapshot is deterministic for a fixed set of Discord reactions at
poll time; `watermark`/`updated_at` vary by run (D-3), so byte-identity is claimed only for `(present,
key, verdict)`, never timestamps.

## 8. Security, Privacy, Ops, Observability
- **Token** from env at call time, never logged/persisted (INV-6). HTTPS to `discord.com/api/v10`, the
  same surface greenhouse's poster uses nightly.
- **PII:** stores already-public Discord IDs, specifically only Ace's own user-id as reactor; no message
  content beyond the seed↔message map greenhouse already persists. DB local-only.
- **Rate limits:** D-5 fetches only 3 emoji/message; D-8 honors 429 `Retry-After`; the poll shares the
  live bot's *identity* for REST, so rate-limit exhaustion is a (low) blast-radius onto the live bot. The
  D-4/D-12 window bounds targets to the active card set (tens), keeping the ceiling ≲ a few hundred
  calls/night (D-15 restates the budget honestly: the 0-skip hint is free from the list response, not an
  extra GET).
- **Observability:** `poll` one-line counts incl. `fetch_fail`/`covers_removes`; `POLL ANOMALY` non-zero
  on all-unreachable; **chronic `covers_removes=False` emits `COVERS_REMOVES_DISABLED` (D-13)**; `report`
  appends the D-12 aged-out advisory; **nightly failures route to `fleet_alert`, not stdout** (INV-7/CB-3).
- **Rollback:** delete `seed_triage.db`. Removing the tool + test removes the feature; nothing imports it;
  greenhouse is a read-only input, unaffected.

## 9. Risks & Mitigations
- **R-1 — Bot pre-seed counted as approval.** *Mitigated:* INV-2 Ace-only + `test_bot_preseed_is_not_triage`.
- **R-2 — Mass-remove / fetch-fail delete.** *Mitigated:* INV-3 proven-complete `covers_removes` +
  add-only fallback + `test_partial_fetch_failure_does_not_remove` (the CB-1 fold).
- **R-3 — Second gateway connection.** *Designed out:* REST only (§0.3/D-1).
- **R-4 — Greenhouse pre-seed coupling.** *Designed out:* D-5 no longer depends on the pre-seed for
  correctness (the CB-2 fold).
- **R-5 — Unbounded REST cost.** *Mitigated:* D-4 window + D-5 3-emoji ceiling (~<300 calls); window is a knob.
- **R-6 — >100 reactors hide Ace.** *Mitigated:* D-8 pagination + `test_reaction_pagination_over_100`.
- **R-7 — Discord API drift.** *Mitigated:* reuses greenhouse's nightly-exercised endpoints; `--check-target` catches auth/endpoint breaks loudly.
- **R-8 — seed_messages.json schema drift.** *Mitigated:* INV-8 schema guard + loud skip + test.
- **R-9 — Concurrent pollers.** *Mitigated:* INV-9 single-writer lock.
- **R-10 — Clock-skew watermark regression.** *Designed out:* DB-derived `max(seq)+1` (D-3).

## 10. Acceptance Criteria
- [ ] **AC-1 (Ace-only):** bot-only card → no verdict. Evidence: `test_bot_preseed_is_not_triage`.
- [ ] **AC-2 (durable approve):** Ace ✅ → `report` shows ✅; survives DB re-open. Evidence: `test_poll_reconcile_end_to_end` + reopen.
- [ ] **AC-3 (un-react flips back):** Ace removes ✅ on a full clean poll → verdict none. Evidence: `test_unreact_flips_verdict_to_none`.
- [ ] **AC-3b (re-react RECOVERS the verdict — pass-2 BLOCK gate):** after AC-3, Ace re-reacts ✅ on the next full poll → `report` shows ✅ again (NOT a silent `stale` no-op). Evidence: `test_unreact_then_rereact_shows_verdict_again` + `test_next_watermark_counts_removed_rows`.
- [ ] **AC-4 (NO fetch-fail delete):** a card whose poll fetch fails keeps its verdict. Evidence: `test_partial_fetch_failure_does_not_remove` + `test_covers_removes_false_over_random_fetch_failure_subsets` (property test).
- [ ] **AC-5 (NO narrow-poll mass-remove):** a `--message` poll never removes other durable keys. Evidence: `test_remove_scope_requires_all_durable_keys`.
- [ ] **AC-6 (precedence on SIMULTANEOUS multi-emoji):** ✅ and 👍 both durably present on one card → ❌>✅>👍 picks ✅. Evidence: `test_verdict_precedence_x_beats_check_beats_thumb`.
- [ ] **AC-7 (pagination):** Ace on reaction page 2 (>100 reactors) is still found. Evidence: `test_reaction_pagination_over_100`.
- [ ] **AC-8 (liveness ≠ selfcheck):** `--selfcheck` exit 0 w/o token; `--check-target` non-zero w/o reachable token; both print the configured ids; `--check-target` is REST-liveness, not the core's journal probe. Evidence: `test_selfcheck_does_not_require_token`, `test_check_target_fails_without_token`, `test_report_prints_configured_ace_id`, `test_check_target_is_rest_liveness`.
- [ ] **AC-9 (nightly failure pages):** a failed `poll` OR `report` propagates non-zero and the entry calls `fleet_alert` (not a swallowed exit 0). Evidence: the §6 entry uses `set -euo pipefail` + `|| fleet_alert` on every step with an absolute `$T`; `test_poll_fails_loud_on_unreachable_api`.
- [ ] **AC-10 (real Discord proof):** against the live #self-improvement seed thread, a real reaction by
  Ace on a real seed card is read by `poll` and shown by `report`. Evidence: a live run transcript (poll
  counts + report row) captured at closeout — the staged-for-Ace empirical proof. *(Happy-path live; the
  failure modes above are proven by the mocked adversarial suite, not live.)*
- [ ] **AC-11 (core untouched; no third-party imports):** `git diff` no change to `reaction_state.py`; `seed_triage.py` imports ONLY stdlib (allowed incl. `urllib`/`fcntl`; AST test forbids third-party); full suite green. Evidence: diff + `test_seed_triage_no_third_party_imports` + `pytest -q`.
- [ ] **AC-12 (no fabricated "since"):** report output never contains the literal `"reconcile"` posing as a timestamp. Evidence: `test_report_row_has_no_fabricated_since`.
- [ ] **AC-13 (flock auto-release):** a crashed/killed poll's lock auto-releases; the next poll acquires cleanly with no manual unlink. Evidence: `test_lock_auto_releases_after_holder_exits`.
- [ ] **AC-14 (chronic covers_removes alerted):** a full poll with a permanently-dead durable key emits `COVERS_REMOVES_DISABLED` naming it, not silent. Evidence: `test_full_poll_covers_removes_false_alerts_with_dead_key`.
- [ ] **AC-15 (aged-out cards surfaced):** an un-triaged card past the capture horizon appears in the report advisory, not silently dropped. Evidence: `test_report_surfaces_aged_out_untriaged_cards`.

## 11. Reversibility
- **Off by default.** A CLI that does nothing unless invoked; nothing scheduled until the §6 entry is
  wired (separate explicit step). No code runs on import.
- **Deletable.** Remove `tools/seed_triage.py` + test → feature gone; leaf consumer, no importers.
- **No external state.** All durable state = `seed_triage.db` (+ WAL). `seed_messages.json` + Discord
  read-only. Full reset = delete the DB.

---

## Review Log
- **Pass-1 (Opus, claude-bpp): BLOCK.** Folded:
  - **CB-1** (remove + partial-fetch-failure silently deletes verdicts) — *confirmed by reading core
    source: reconcile's remove pass is global over `current_present`*. Fix: INV-3 `covers_removes` now
    requires every durable-key message fully+successfully enumerated; any fetch failure → add-only +
    loud. New `test_partial_fetch_failure_does_not_remove` (AC-4).
  - **CB-2** (count-gate is load-bearing, not optimization; couples to greenhouse pre-seed) — Fix: D-5
    always fetches the 3 triage emoji; count used only as a *safe* 0-skip. Correctness no longer depends
    on pre-seed.
  - **CB-3** (nightly entry masks `poll` failure) — Fix: §6 entry `set -euo pipefail` + `|| fleet_alert`;
    AC-9.
  - **Required changes folded:** pagination (D-8/AC-7), emoji-encoding (D-8/test), INV-4 reframed to the
    real no-DB-mutation mechanism + dropped the core-test masquerading as adapter coverage, `report`
    all-maps scan (D-9), visible identities (D-10), single-writer lock (INV-9), seed-map schema guard
    (INV-8).
  - **Open questions answered from core source** and recorded in §0.2 (global remove; stale=ROLLBACK-no-write).
- **Pass-2 (Opus, TWO independent runs — read all, harshest wins per the pipeline skill):**
  - **`claude-bpp`: APPROVE WITH CHANGES.** Confirmed all 3 pass-1 blockers genuinely resolved against core source.
  - **`claude-api-proxy`: BLOCK** — and the BLOCK won. It ran an EMPIRICAL test against the real core and found a silent re-react data-loss bug the milder review missed.
  - **CB-A (BLOCK, confirmed empirically by me before folding):** the spec's "watermark guard is vestigial / not load-bearing" framing invited **Variant B** (`next_watermark = MAX(seq) WHERE present=1`), under which un-react→re-react silently fails (Ace re-reacts ✅, `report` shows nothing). I reproduced both readings against `reaction_state.py`: Variant A → `(present, gone, present)` ✅; Variant B → `(present, gone, GONE)` ✗. **Fixed:** §0.2 ⚠️ + D-3 now mandate `COALESCE(MAX(seq),0)+1` over ALL rows and call the guard load-bearing; new AC-3b `test_unreact_then_rereact_shows_verdict_again` + `test_next_watermark_counts_removed_rows` are the load-bearing gates.
  - **CB-B (the "since" column is unbuildable through the unmodified-core contract):** `reconcile()` hardcodes `ts="reconcile"` and exposes no `ts` param, so a "since" timestamp would render the literal string `"reconcile"` — a demo-as-live fabrication. **Fixed:** D-11 drops "since" from v0.1; AC-12 `test_report_row_has_no_fabricated_since` guards it; a real first-seen time is a scoped core change deferred to roadmap.
  - **CB-A (bpp's, the lock):** INV-9 lock mechanism unspecified → a presence-file lock wedges nightly forever on crash. **Fixed:** INV-9 now mandates `fcntl.flock` (auto-release on death); AC-13 `test_lock_auto_releases_after_holder_exits`.
  - **CB-C (`--check-target` flag collision with the core's journal probe):** **Fixed:** D-14 + `test_check_target_is_rest_liveness`.
  - **Required changes folded:** capture-horizon silent-miss → D-12 + `test_report_surfaces_aged_out_untriaged_cards`; chronic `covers_removes=False` → D-13 `COVERS_REMOVES_DISABLED` + test; property-test over random fetch-failure subsets → `test_covers_removes_false_over_random_fetch_failure_subsets`; real urllib exception types in mocks + 401 token-redaction → Phase 1; absolute `$T` + `|| fleet_alert` on `report` + greenhouse-ordering note → §6; `next_watermark` empty-DB=1 → `test_next_watermark_empty_db_is_one`; simultaneous multi-emoji precedence → AC-6; honest REST budget → D-15.
  - **Convergence:** all pass-2 blockers + required changes are foldable in-spec (no architectural rewrite). Status → APPROVED v0.3, cleared for build.

## Roadmap (each row = its own version + its own review)
| Version | What ships | Trigger | Maps to |
|---|---|---|---|
| **v0.1 (this)** | REST poll → reconcile → durable triage read-model (read-only w.r.t. greenhouse) | now | §6 P1–6 |
| v0.2 | **Write-back: promote an ✅'d seed into a greenhouse build / post a confirm** — first edit to greenhouse's LIVE path, own PRD + Opus passes (default-unchanged invariant applies) | a verdict exists & Ace wants auto-promotion | new PRD |
| v0.2b | **Real first-seen timestamp ("since" column)** — needs a scoped `ts` parameter on the core's `reconcile()`, so it's a core change with its own review (D-11) | Ace wants verdict age in the report | new PRD (core change) |
| v0.3 | Live-push listener (only if sub-minute latency is ever needed) — must consume the live gateway's reaction events, not a second connection | nightly latency proves insufficient | new PRD |
