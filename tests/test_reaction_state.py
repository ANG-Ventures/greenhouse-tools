"""Tests for tools.reaction_state — collect & pass offline, stdlib only.

Covers the approved core (apply: cache-independent, atomic, idempotent,
out-of-order-safe) and the re-specced reconcile (add-only by default,
watermarked, remove-on-absence gated) plus the two-probe contract
(--selfcheck deploy probe vs --check-target real liveness).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from tools import reaction_state as rs


# --- helpers ----------------------------------------------------------------
def _ev(user_id, action, seq, channel="C1", message="M1", emoji="👍", ts=""):
    return {
        "channel_id": channel,
        "message_id": message,
        "emoji": emoji,
        "user_id": user_id,
        "action": action,
        "seq": seq,
        "ts": ts,
    }


def _conn():
    return rs.connect(":memory:")


# --- validate ---------------------------------------------------------------
def test_validate_rejects_missing_field():
    bad = {"channel_id": "C", "message_id": "M", "emoji": "x", "user_id": "U", "action": "add"}
    try:
        rs.validate_event(bad)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_validate_rejects_unknown_action():
    try:
        rs.validate_event(_ev("U1", "teleport", 1))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_validate_rejects_non_int_seq():
    try:
        rs.validate_event(_ev("U1", "add", "1"))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_validate_rejects_bool_seq():
    # bool is an int subclass — must be rejected explicitly.
    try:
        rs.validate_event(_ev("U1", "add", True))
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- apply: cache-independent, atomic, idempotent ---------------------------
def test_apply_add_sets_present():
    c = _conn()
    assert rs.apply_event(c, _ev("U1", "add", 1)) == "applied"
    assert ("C1", "M1", "👍", "U1") in rs.current_present(c)


def test_apply_remove_clears_present():
    c = _conn()
    rs.apply_event(c, _ev("U1", "add", 1))
    assert rs.apply_event(c, _ev("U1", "remove", 2)) == "applied"
    assert ("C1", "M1", "👍", "U1") not in rs.current_present(c)


def test_apply_is_idempotent_on_replay():
    c = _conn()
    assert rs.apply_event(c, _ev("U1", "add", 5)) == "applied"
    # exact replay (same seq) is a stale no-op
    assert rs.apply_event(c, _ev("U1", "add", 5)) == "stale"
    # ledger has exactly one row for that key
    n = c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    assert n == 1


def test_apply_rejects_out_of_order_lower_seq():
    c = _conn()
    rs.apply_event(c, _ev("U1", "remove", 10))
    # an older add (seq 3) arrives late — must NOT resurrect the reaction
    assert rs.apply_event(c, _ev("U1", "add", 3)) == "stale"
    assert ("C1", "M1", "👍", "U1") not in rs.current_present(c)


def test_apply_unaffected_by_message_caching():
    # The key never references cache state; a brand-new (never-seen) message id
    # applies just as cleanly as a familiar one. INV-2 by construction.
    c = _conn()
    out = rs.apply_event(c, _ev("U7", "add", 1, message="never_cached_999"))
    assert out == "applied"
    assert ("C1", "never_cached_999", "👍", "U7") in rs.current_present(c)


def test_apply_distinct_keys_are_independent():
    c = _conn()
    rs.apply_event(c, _ev("U1", "add", 1, emoji="👍"))
    rs.apply_event(c, _ev("U1", "add", 1, emoji="✅"))
    present = rs.current_present(c)
    assert ("C1", "M1", "👍", "U1") in present
    assert ("C1", "M1", "✅", "U1") in present


def test_apply_rejects_unknown_source():
    c = _conn()
    try:
        rs.apply_event(c, _ev("U1", "add", 1), source="bogus")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_apply_atomic_ledger_matches_state():
    # Every applied transition leaves exactly one ledger row; stale leaves none.
    c = _conn()
    rs.apply_event(c, _ev("U1", "add", 1))
    rs.apply_event(c, _ev("U1", "add", 1))      # stale
    rs.apply_event(c, _ev("U1", "remove", 2))   # applied
    n = c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    assert n == 2


# --- journal ----------------------------------------------------------------
def test_read_journal_skips_blank_lines(tmp_path):
    p = tmp_path / "reactions.jsonl"
    p.write_text(json.dumps(_ev("U1", "add", 1)) + "\n\n", encoding="utf-8")
    assert len(rs.read_journal(p)) == 1


def test_read_journal_rejects_malformed(tmp_path):
    p = tmp_path / "reactions.jsonl"
    p.write_text("{not json}\n", encoding="utf-8")
    try:
        rs.read_journal(p)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_replay_journal_counts(tmp_path):
    c = _conn()
    events = [_ev("U1", "add", 1), _ev("U2", "add", 1), _ev("U1", "add", 1)]
    counts = rs.replay_journal(c, events)
    assert counts == {"applied": 2, "stale": 1}


# --- reconcile: watermark required ------------------------------------------
def test_reconcile_requires_watermark():
    c = _conn()
    try:
        rs.reconcile(c, {"reactions": []})
        assert False, "expected ValueError on missing watermark"
    except ValueError:
        pass


def test_reconcile_recovers_missed_add():
    c = _conn()
    snap = {
        "watermark": 10,
        "covers_removes": False,
        "reactions": [
            {"channel_id": "C1", "message_id": "M1", "emoji": "👍", "user_id": "U1"}
        ],
    }
    r = rs.reconcile(c, snap)
    assert r["added"] == 1
    assert r["removed"] == 0
    assert ("C1", "M1", "👍", "U1") in rs.current_present(c)


# --- reconcile: add-only is the DATA-LOSS DEFENSE ---------------------------
def test_reconcile_add_only_never_removes_on_absence():
    # Durable state has a present reaction the snapshot does NOT list. Default
    # (allow_removes=False) must NOT destroy it.
    c = _conn()
    rs.apply_event(c, _ev("U1", "add", 1))
    snap = {"watermark": 99, "covers_removes": True, "reactions": []}
    r = rs.reconcile(c, snap)  # default allow_removes=False
    assert r["removed"] == 0
    assert r["remove_eligible"] is False
    assert ("C1", "M1", "👍", "U1") in rs.current_present(c)


def test_reconcile_opt_in_without_snapshot_declaration_is_safe():
    # Caller opts in to removes, but the snapshot does NOT declare it enumerates
    # absences (covers_removes=False). Absence must still NOT remove.
    c = _conn()
    rs.apply_event(c, _ev("U1", "add", 1))
    snap = {"watermark": 99, "covers_removes": False, "reactions": []}
    r = rs.reconcile(c, snap, allow_removes=True)
    assert r["removed"] == 0
    assert r["remove_eligible"] is False
    assert ("C1", "M1", "👍", "U1") in rs.current_present(c)


def test_reconcile_remove_requires_both_optin_and_declaration():
    # Only when BOTH allow_removes AND covers_removes hold does absence remove.
    c = _conn()
    rs.apply_event(c, _ev("U1", "add", 1))
    snap = {"watermark": 99, "covers_removes": True, "reactions": []}
    r = rs.reconcile(c, snap, allow_removes=True)
    assert r["remove_eligible"] is True
    assert r["removed"] == 1
    assert ("C1", "M1", "👍", "U1") not in rs.current_present(c)


# --- reconcile: stale snapshot cannot clobber a fresher live transition -----
def test_reconcile_stale_snapshot_cannot_clobber_fresh_remove():
    # Live: user added then removed at seq 5. A STALE snapshot (watermark 3)
    # still lists the reaction as present. Add-only reconcile must NOT resurrect
    # it, because the watermark (3) is older than the live remove (5).
    c = _conn()
    rs.apply_event(c, _ev("U1", "add", 1))
    rs.apply_event(c, _ev("U1", "remove", 5))
    snap = {
        "watermark": 3,
        "covers_removes": False,
        "reactions": [
            {"channel_id": "C1", "message_id": "M1", "emoji": "👍", "user_id": "U1"}
        ],
    }
    r = rs.reconcile(c, snap)
    assert r["added"] == 0
    assert r["stale"] == 1
    assert ("C1", "M1", "👍", "U1") not in rs.current_present(c)


def test_reconcile_fresh_snapshot_recovers_over_old_remove():
    # Same shape but the snapshot watermark (9) is NEWER than the live remove
    # (5): the snapshot legitimately re-establishes presence.
    c = _conn()
    rs.apply_event(c, _ev("U1", "add", 1))
    rs.apply_event(c, _ev("U1", "remove", 5))
    snap = {
        "watermark": 9,
        "covers_removes": False,
        "reactions": [
            {"channel_id": "C1", "message_id": "M1", "emoji": "👍", "user_id": "U1"}
        ],
    }
    r = rs.reconcile(c, snap)
    assert r["added"] == 1
    assert ("C1", "M1", "👍", "U1") in rs.current_present(c)


# --- selfcheck (deploy health probe) ----------------------------------------
def test_selfcheck_returns_true():
    assert rs.selfcheck() is True


def test_run_selfcheck_exit_zero():
    assert rs._run_selfcheck() == 0


def test_main_selfcheck_flag_exit_zero():
    assert rs.main(["--selfcheck"]) == 0


def test_unknown_flag_exits_nonzero():
    # Real argv dispatch: a garbage flag must NOT exit 0.
    try:
        rs.main(["--definitely-not-a-flag"])
        assert False, "expected SystemExit on unknown flag"
    except SystemExit as exc:
        assert exc.code != 0


# --- check-target (real liveness gate) --------------------------------------
def test_check_target_missing_journal_fails(tmp_path):
    ok, msg = rs.check_target(tmp_path / "nope.jsonl")
    assert ok is False
    assert "does not exist" in msg


def test_check_target_empty_journal_fails(tmp_path):
    p = tmp_path / "reactions.jsonl"
    p.write_text("", encoding="utf-8")
    ok, msg = rs.check_target(p)
    assert ok is False
    assert "empty" in msg


def test_check_target_blank_only_journal_fails(tmp_path):
    # Non-empty bytes but zero valid events: liveness must still fail loudly.
    p = tmp_path / "reactions.jsonl"
    p.write_text("\n\n", encoding="utf-8")
    ok, msg = rs.check_target(p)
    assert ok is False
    assert "no valid reaction events" in msg


def test_check_target_good_journal_passes(tmp_path):
    p = tmp_path / "reactions.jsonl"
    p.write_text(json.dumps(_ev("U1", "add", 1)) + "\n", encoding="utf-8")
    ok, msg = rs.check_target(p)
    assert ok is True
    assert "OK" in msg


def test_run_check_target_nonzero_on_missing(tmp_path):
    rc = rs._run_check_target(str(tmp_path / "nope.jsonl"))
    assert rc != 0


def test_run_check_target_zero_on_good(tmp_path):
    p = tmp_path / "reactions.jsonl"
    p.write_text(json.dumps(_ev("U1", "add", 1)) + "\n", encoding="utf-8")
    assert rs._run_check_target(str(p)) == 0


def test_check_target_flag_requires_journal():
    # --check-target without --journal must error out (non-zero), not pass.
    try:
        rs.main(["--check-target"])
        assert False, "expected SystemExit"
    except SystemExit as exc:
        assert exc.code != 0


# --- end-to-end via main ----------------------------------------------------
def test_main_replays_journal_into_db(tmp_path):
    journal = tmp_path / "reactions.jsonl"
    journal.write_text(
        "\n".join(json.dumps(_ev("U%d" % i, "add", 1)) for i in range(3)),
        encoding="utf-8",
    )
    db = tmp_path / "state.db"
    rc = rs.main(["--journal", str(journal), "--db", str(db)])
    assert rc == 0
    assert db.exists()
    c = rs.connect(str(db))
    assert len(rs.current_present(c)) == 3


# --- selfcheck != liveness (INV-6): selfcheck touches NO real journal -------
def test_selfcheck_does_not_read_real_journal(tmp_path, monkeypatch):
    # selfcheck must pass with no journal present anywhere; it builds its own
    # fixture. Run it from an empty dir to prove it reads nothing on disk.
    monkeypatch.chdir(tmp_path)
    assert rs.selfcheck() is True


# --- invariant: stdlib only (AST walk) --------------------------------------
_STDLIB_OK = {
    "__future__", "argparse", "json", "sqlite3", "sys", "tempfile", "pathlib",
}


def test_reaction_state_imports_stdlib_only():
    tree = ast.parse(Path(rs.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    third_party = roots - _STDLIB_OK
    assert third_party == set(), f"unexpected non-stdlib import(s): {third_party}"


# --- invariant: no network / subprocess imports -----------------------------
_FORBIDDEN_IMPORT_ROOTS = {
    "requests", "urllib", "http", "socket", "ssl", "subprocess",
    "smtplib", "ftplib", "asyncio", "websockets", "discord",
}


def test_no_network_or_subprocess_imports():
    tree = ast.parse(Path(rs.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    leaked = roots & _FORBIDDEN_IMPORT_ROOTS
    assert leaked == set(), f"forbidden import root(s) present: {leaked}"
