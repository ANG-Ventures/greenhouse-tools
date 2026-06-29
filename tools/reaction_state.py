"""reaction_state — durable Discord-reaction triage state core (PROTOTYPE).

This is the stdlib-only durable-state core the seed is about: it turns raw
reaction *transitions* into durable triage state in its own SQLite DB, applies
each add/remove atomically and idempotently keyed by message+emoji+user, and
runs a one-time boot reconcile sweep against a WATERMARKED authoritative
snapshot to recover transitions missed while offline.

It does NOT open a Discord gateway connection. `discord.py` (which owns the
raw on_raw_reaction_add/remove events) is non-stdlib, so the live websocket
client is out of scope (a later, separately-specced phase supplies a thin shim
that calls this core). The transport-of-record is a local append-only journal
`reactions.jsonl` — one JSON line per raw reaction transition — that a future
gateway adapter writes; the tool's source of truth is its own SQLite DB.

Hard invariants (see REVERSIBILITY.md):
  * stdlib only; no network calls.
  * Apply is keyed on (channel_id, message_id, emoji, user_id) with NO
    dependence on whether the message was ever cached (INV-2).
  * Apply + ledger append is one atomic transaction; out-of-order events are
    rejected by a per-key monotonic sequence (idempotent / replay-safe).
  * Reconcile is ADD-ONLY by default and WATERMARKED: absence never triggers a
    remove, and a stale snapshot can never clobber a fresher transition.
    Remove-on-absence is available only behind an explicit, snapshot-declared
    opt-in and is off in the nightly entry.
  * --selfcheck (offline deploy probe) is NOT a liveness check; --check-target
    is the real-journal liveness gate (INV-6).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

# Disjoint sequence namespaces so a live-event seq can never be confused with a
# reconcile-derived seq when ordering transitions for the same key (blocker 4).
SEQ_LIVE = "live"
SEQ_RECON = "recon"
_SEQ_NS = {SEQ_LIVE, SEQ_RECON}

ADD = "add"
REMOVE = "remove"
_ACTIONS = {ADD, REMOVE}


# --- DB schema --------------------------------------------------------------
def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the durable-state DB with the schema applied."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS reaction_state (
            channel_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            emoji      TEXT NOT NULL,
            user_id    TEXT NOT NULL,
            present    INTEGER NOT NULL,   -- 1 = reaction present, 0 = removed
            seq        INTEGER NOT NULL,   -- last applied per-key sequence
            updated_at TEXT NOT NULL,
            PRIMARY KEY (channel_id, message_id, emoji, user_id)
        );
        CREATE TABLE IF NOT EXISTS ledger (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            emoji      TEXT NOT NULL,
            user_id    TEXT NOT NULL,
            action     TEXT NOT NULL,
            seq        INTEGER NOT NULL,
            source     TEXT NOT NULL,      -- 'live' or 'recon'
            ts         TEXT NOT NULL
        );
        """
    )
    conn.commit()
    return conn


def _key(ev: dict) -> tuple[str, str, str, str]:
    return (
        str(ev["channel_id"]),
        str(ev["message_id"]),
        str(ev["emoji"]),
        str(ev["user_id"]),
    )


def validate_event(ev: dict) -> dict:
    """Validate one raw transition. Raises ValueError on a malformed event."""
    if not isinstance(ev, dict):
        raise ValueError("event is not an object")
    for field in ("channel_id", "message_id", "emoji", "user_id", "action", "seq"):
        if field not in ev:
            raise ValueError(f"missing field {field!r}")
    if ev["action"] not in _ACTIONS:
        raise ValueError(f"unknown action {ev['action']!r}")
    if not isinstance(ev["seq"], int) or isinstance(ev["seq"], bool):
        raise ValueError("seq must be an int")
    return ev


# --- apply (the cache-independent, atomic, idempotent core) -----------------
def apply_event(
    conn: sqlite3.Connection,
    ev: dict,
    *,
    source: str = SEQ_LIVE,
    ts: str = "",
) -> str:
    """Apply one raw transition atomically. Returns the outcome.

    Outcomes:
      "applied"  — state advanced and a ledger row was appended.
      "stale"    — ev.seq <= the stored seq for this key; no-op (idempotent /
                   replay-safe / out-of-order-safe).

    Keyed solely on (channel_id, message_id, emoji, user_id): there is NO
    branch on whether the message was ever cached, by construction (INV-2).
    The state update and ledger append share ONE transaction (blocker:
    apply+ledger must be atomic).
    """
    if source not in _SEQ_NS:
        raise ValueError(f"unknown source {source!r}")
    validate_event(ev)
    ck, mk, ek, uk = _key(ev)
    seq = int(ev["seq"])
    present = 1 if ev["action"] == ADD else 0
    try:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT seq FROM reaction_state WHERE channel_id=? AND message_id=? "
            "AND emoji=? AND user_id=?",
            (ck, mk, ek, uk),
        ).fetchone()
        if row is not None and seq <= row[0]:
            conn.execute("ROLLBACK")
            return "stale"
        conn.execute(
            "INSERT INTO reaction_state "
            "(channel_id, message_id, emoji, user_id, present, seq, updated_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(channel_id, message_id, emoji, user_id) DO UPDATE SET "
            "present=excluded.present, seq=excluded.seq, updated_at=excluded.updated_at",
            (ck, mk, ek, uk, present, seq, ts),
        )
        conn.execute(
            "INSERT INTO ledger "
            "(channel_id, message_id, emoji, user_id, action, seq, source, ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ck, mk, ek, uk, ev["action"], seq, source, ts),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return "applied"


def current_present(conn: sqlite3.Connection) -> set[tuple[str, str, str, str]]:
    """Set of keys currently present (present=1) in durable state."""
    rows = conn.execute(
        "SELECT channel_id, message_id, emoji, user_id FROM reaction_state "
        "WHERE present=1"
    ).fetchall()
    return {tuple(r) for r in rows}


# --- journal ----------------------------------------------------------------
def read_journal(path: str | Path) -> list[dict]:
    """Parse the append-only reactions.jsonl transport. One event per line.

    Blank lines are skipped. A malformed line raises ValueError.
    """
    events: list[dict] = []
    text = Path(path).read_text(encoding="utf-8")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {lineno}: invalid JSON: {exc}") from exc
        try:
            validate_event(obj)
        except ValueError as exc:
            raise ValueError(f"line {lineno}: {exc}") from exc
        events.append(obj)
    return events


def replay_journal(conn: sqlite3.Connection, events: list[dict]) -> dict:
    """Apply a sequence of live events; return {'applied': n, 'stale': m}."""
    counts = {"applied": 0, "stale": 0}
    for ev in events:
        outcome = apply_event(conn, ev, source=SEQ_LIVE, ts=ev.get("ts", ""))
        counts[outcome] += 1
    return counts


# --- reconcile (add-only by default, watermarked) ---------------------------
def reconcile(
    conn: sqlite3.Connection,
    snapshot: dict,
    *,
    allow_removes: bool = False,
) -> dict:
    """Boot reconcile sweep: diff durable state against a WATERMARKED snapshot.

    `snapshot` shape:
      {
        "watermark": <int>,          # the seq the snapshot is current as of
        "covers_removes": <bool>,    # snapshot DECLARES it enumerates absences
        "reactions": [ {channel_id, message_id, emoji, user_id}, ... ],
      }

    Recovers ADDS that the snapshot asserts present but durable state is
    missing — synthesizing a reconcile event stamped at the watermark, applied
    through the same monotonic-seq guard so a stale snapshot can never clobber a
    fresher live transition (blocker 1: freshness basis).

    Remove-on-absence (synthesizing a remove from a key being ABSENT from the
    snapshot) is the data-loss vector (blocker 2). It is OFF by default and is
    honored ONLY when BOTH the caller opts in (`allow_removes=True`) AND the
    snapshot DECLARES `covers_removes=True` — i.e. it claims to enumerate the
    full present-set, so absence is meaningful. Otherwise absence is never a
    remove. The nightly entry calls with allow_removes=False.
    """
    if "watermark" not in snapshot:
        raise ValueError("snapshot missing 'watermark' (no freshness basis)")
    watermark = snapshot["watermark"]
    if not isinstance(watermark, int) or isinstance(watermark, bool):
        raise ValueError("snapshot 'watermark' must be an int")
    covers_removes = bool(snapshot.get("covers_removes", False))
    snap_keys = {
        (str(r["channel_id"]), str(r["message_id"]), str(r["emoji"]), str(r["user_id"]))
        for r in snapshot.get("reactions", [])
    }
    present = current_present(conn)

    result = {"added": 0, "removed": 0, "stale": 0, "remove_eligible": False}

    # Recover missed ADDS: present in snapshot, absent from durable present-set.
    for ck, mk, ek, uk in sorted(snap_keys - present):
        ev = {
            "channel_id": ck, "message_id": mk, "emoji": ek, "user_id": uk,
            "action": ADD, "seq": watermark,
        }
        outcome = apply_event(conn, ev, source=SEQ_RECON, ts="reconcile")
        result["added" if outcome == "applied" else "stale"] += 1

    # Remove-on-absence: ONLY with explicit opt-in AND snapshot declaring it
    # covers the full present-set. Otherwise absence is never destructive.
    eligible = allow_removes and covers_removes
    result["remove_eligible"] = eligible
    if eligible:
        for ck, mk, ek, uk in sorted(present - snap_keys):
            ev = {
                "channel_id": ck, "message_id": mk, "emoji": ek, "user_id": uk,
                "action": REMOVE, "seq": watermark,
            }
            outcome = apply_event(conn, ev, source=SEQ_RECON, ts="reconcile")
            result["removed" if outcome == "applied" else "stale"] += 1

    return result


# --- --selfcheck deploy health probe (offline, self-built fixture) ----------
def _good_fixture_events() -> list[dict]:
    base = {"channel_id": "C1", "message_id": "M1", "emoji": "👍"}
    return [
        {**base, "user_id": "U1", "action": ADD, "seq": 1, "ts": "t1"},
        {**base, "user_id": "U2", "action": ADD, "seq": 1, "ts": "t2"},
        {**base, "user_id": "U1", "action": REMOVE, "seq": 2, "ts": "t3"},
        {**base, "user_id": "U1", "action": ADD, "seq": 1, "ts": "t4"},  # stale replay
    ]


def selfcheck() -> bool:
    """Offline logic probe. Builds its OWN in-memory fixture, exercises apply +
    reconcile, and asserts the durable invariants. Touches NO real journal.

    Returns True iff the core behaves correctly. This is the DEPLOY health
    check; it says nothing about whether a real journal exists (that is
    --check-target). NOT a liveness check (INV-6).
    """
    conn = connect(":memory:")
    counts = replay_journal(conn, _good_fixture_events())
    if counts != {"applied": 3, "stale": 1}:
        return False
    # After replay: U2 add present, U1 removed (seq 2 beats the stale seq-1 re-add).
    present = current_present(conn)
    if ("C1", "M1", "👍", "U2") not in present:
        return False
    if ("C1", "M1", "👍", "U1") in present:
        return False

    # Reconcile add-only: snapshot asserts a NEW present key; durable recovers it.
    snap = {
        "watermark": 5,
        "covers_removes": False,
        "reactions": [
            {"channel_id": "C1", "message_id": "M1", "emoji": "👍", "user_id": "U2"},
            {"channel_id": "C1", "message_id": "M2", "emoji": "✅", "user_id": "U9"},
        ],
    }
    r = reconcile(conn, snap, allow_removes=False)
    if r["added"] != 1 or r["removed"] != 0 or r["remove_eligible"]:
        return False
    if ("C1", "M2", "✅", "U9") not in current_present(conn):
        return False

    # Add-only refuses to remove the absent U2 even when caller opts in, because
    # the snapshot did NOT declare covers_removes.
    snap_absent = {"watermark": 6, "covers_removes": False, "reactions": []}
    r2 = reconcile(conn, snap_absent, allow_removes=True)
    if r2["removed"] != 0 or r2["remove_eligible"]:
        return False
    conn.close()
    return True


def _run_selfcheck() -> int:
    try:
        ok = selfcheck()
    except Exception as exc:  # pragma: no cover - defensive
        print(f"selfcheck FAIL: {exc}", file=sys.stderr)
        return 1
    if not ok:
        print("selfcheck FAIL: durable-state core violated an invariant", file=sys.stderr)
        return 1
    print("selfcheck OK")
    return 0


# --- --check-target real-journal liveness gate ------------------------------
def check_target(journal_path: str | Path) -> tuple[bool, str]:
    """Assert the ACTUAL reactions.jsonl journal exists, is a regular file, and
    is non-empty AND parses into >=1 valid event. Returns (ok, message).

    The nightly entry runs THIS first so "read nothing" can never be a silent
    exit 0. Unlike --selfcheck this touches the real source.
    """
    p = Path(journal_path)
    if not p.exists():
        return False, f"journal does not exist: {p}"
    if not p.is_file():
        return False, f"journal is not a regular file: {p}"
    if p.stat().st_size == 0:
        return False, f"journal is empty: {p}"
    try:
        events = read_journal(p)
    except (OSError, ValueError) as exc:
        return False, f"journal unreadable/corrupt: {exc}"
    if not events:
        return False, f"journal has no valid reaction events: {p}"
    return True, f"journal OK: {len(events)} event(s) in {p}"


def _run_check_target(journal_path: str) -> int:
    ok, msg = check_target(journal_path)
    if not ok:
        print(f"check-target FAIL: {msg}", file=sys.stderr)
        return 2
    print(f"check-target OK: {msg}")
    return 0


# --- CLI --------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reaction_state",
        description="Durable Discord-reaction triage state core (prototype).",
    )
    parser.add_argument("--journal", help="path to reactions.jsonl journal")
    parser.add_argument("--db", help="path to the durable-state SQLite DB")
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="offline deploy health probe (self-built fixture); NOT a liveness check",
    )
    parser.add_argument(
        "--check-target",
        action="store_true",
        help="real-journal liveness gate: assert the journal exists & is non-empty",
    )
    args = parser.parse_args(argv)

    if args.selfcheck:
        return _run_selfcheck()

    if args.check_target:
        if not args.journal:
            parser.error("--check-target requires --journal")
        return _run_check_target(args.journal)

    if not args.journal or not args.db:
        parser.error("--journal and --db are required unless --selfcheck is given")

    try:
        events = read_journal(Path(args.journal))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    conn = connect(args.db)
    counts = replay_journal(conn, events)
    conn.close()
    print(f"applied {counts['applied']} transition(s), {counts['stale']} stale no-op(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
