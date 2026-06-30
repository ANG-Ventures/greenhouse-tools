"""Tests for tools.seed_triage — the Discord-reaction → durable triage adapter.

Offline + stdlib only. The REST layer is driven by an injectable `opener` so the
whole poll→reconcile→report pipeline is exercised without a network. The
load-bearing pass-2 gates are here:
  * test_unreact_then_rereact_shows_verdict_again  (AC-3b — the silent re-react bug)
  * test_next_watermark_counts_removed_rows         (D-3 ⚠️ Variant-B guard)
  * test_covers_removes_false_over_random_fetch_failure_subsets (property test)
  * test_lock_auto_releases_after_holder_exits      (INV-9 flock, no wedge)
"""

from __future__ import annotations

import ast
import json
import multiprocessing
import os
import time
import urllib.error
from pathlib import Path

import pytest

from tools import reaction_state as rs
from tools import seed_triage as st


# --- a fake urllib opener ----------------------------------------------------
class FakeResp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_opener(reaction_map, *, fail=None, raise_exc=None):
    """reaction_map: {(channel,message,emoji): [user_id,...]}. `fail`: a set of
    (channel,message,emoji) whose GET raises a real HTTPError 404. `raise_exc`: a
    callable(url)->exc-or-None for custom failures."""
    fail = fail or set()

    def opener(req, timeout=20):
        url = req.full_url
        path = url.split("?", 1)[0]
        if "/users/@me" in path:
            return FakeResp(json.dumps({"id": "BOT123"}).encode())
        parts = path.split("/")
        ri = parts.index("reactions")
        mid = parts[ri - 1]
        ch = parts[ri - 3]
        import urllib.parse as up
        emoji = up.unquote(parts[ri + 1])
        key = (ch, mid, emoji)
        if raise_exc is not None:
            exc = raise_exc(url)
            if exc is not None:
                raise exc
        if key in fail:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return FakeResp(json.dumps(
            [{"id": u} for u in reaction_map.get(key, [])]).encode())

    return opener


ACE = "117431298246705156"
BOT = "1502226398813618176"
CH = "C1"
M1 = "M1"


def _conn():
    return rs.connect(":memory:")


# === Phase 1 — REST client ===================================================
def test_api_builds_bot_auth_header():
    seen = {}

    def opener(req, timeout=20):
        seen["auth"] = req.headers.get("Authorization")
        return FakeResp(b"{}")

    st.api_get("/users/@me", token="SECRET", opener=opener)
    assert seen["auth"] == "Bot SECRET"


def test_emoji_path_encoding():
    seen = {}

    def opener(req, timeout=20):
        seen["url"] = req.full_url
        return FakeResp(b"[]")

    st.reaction_users(CH, M1, "✅", token="x", opener=opener)
    # the raw emoji must be percent-encoded, not present literally
    assert "%E2%9C%85" in seen["url"]
    assert "✅" not in seen["url"]


def test_reaction_pagination_over_100():
    # page 1 = 100 users (none is Ace), page 2 = Ace. Must paginate to find him.
    page1 = [{"id": f"u{i}"} for i in range(100)]
    page2 = [{"id": ACE}]
    calls = {"n": 0}

    def opener(req, timeout=20):
        calls["n"] += 1
        return FakeResp(json.dumps(page1 if calls["n"] == 1 else page2).encode())

    users = st.reaction_users(CH, M1, "✅", token="x", opener=opener)
    assert ACE in users
    assert calls["n"] == 2


def test_api_unreachable_is_loud():
    def opener(req, timeout=20):
        raise urllib.error.URLError("connection refused")

    with pytest.raises(st.APIError):
        st.api_get("/users/@me", token="x", opener=opener, _retries=2)


def test_token_never_in_output_on_401():
    # A 401 HTTPError whose repr can carry request/headers must never leak the token.
    def opener(req, timeout=20):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    try:
        st.api_get("/users/@me", token="SUPERSECRETTOKEN", opener=opener)
        assert False, "expected APIError"
    except st.APIError as exc:
        assert "SUPERSECRETTOKEN" not in str(exc)


# === Phase 2 — Ace-filter + snapshot + fetch tracking ========================
def test_snapshot_keeps_only_ace():
    opener = make_opener({(CH, M1, "✅"): [BOT, ACE, "999"]})
    snap, fok = st.build_snapshot([(CH, M1)], ACE, 1, token="x", opener=opener)
    keys = {(r["emoji"], r["user_id"]) for r in snap["reactions"]}
    assert keys == {("✅", ACE)}
    assert fok[(CH, M1)] is True


def test_bot_preseed_is_not_triage():
    # Only the bot pre-seeded all three; Ace did nothing → no triage signal.
    opener = make_opener({(CH, M1, "✅"): [BOT],
                          (CH, M1, "👍"): [BOT],
                          (CH, M1, "❌"): [BOT]})
    snap, _ = st.build_snapshot([(CH, M1)], ACE, 1, token="x", opener=opener)
    assert snap["reactions"] == []


def test_other_member_reaction_ignored():
    opener = make_opener({(CH, M1, "✅"): ["someone_else"]})
    snap, _ = st.build_snapshot([(CH, M1)], ACE, 1, token="x", opener=opener)
    assert snap["reactions"] == []


def test_fetch_failure_marks_message_incomplete():
    opener = make_opener({(CH, M1, "✅"): [ACE]}, fail={(CH, M1, "👍")})
    snap, fok = st.build_snapshot([(CH, M1)], ACE, 1, token="x", opener=opener)
    assert fok[(CH, M1)] is False


def test_snapshot_shape_matches_core():
    opener = make_opener({(CH, M1, "✅"): [ACE]})
    snap, _ = st.build_snapshot([(CH, M1)], ACE, 7, token="x", opener=opener)
    # core requires watermark:int + reactions:[{4 keys}]
    assert isinstance(snap["watermark"], int)
    r = snap["reactions"][0]
    assert set(r) == {"channel_id", "message_id", "emoji", "user_id"}


# === Phase 3 — watermark (D-3 ⚠️) ===========================================
def test_next_watermark_empty_db_is_one():
    assert st.next_watermark(_conn()) == 1


def test_next_watermark_counts_removed_rows():
    # A REMOVED row (present=0) at the current max seq MUST still be counted, or a
    # re-react would land <= it and be silently dropped (Variant-B bug).
    c = _conn()
    rs.apply_event(c, {"channel_id": CH, "message_id": M1, "emoji": "✅",
                       "user_id": ACE, "action": "add", "seq": 1})
    rs.apply_event(c, {"channel_id": CH, "message_id": M1, "emoji": "✅",
                       "user_id": ACE, "action": "remove", "seq": 5})
    # max over ALL rows is 5 → next is 6 (NOT 1, which a present=1 filter would give)
    assert st.next_watermark(c) == 6


# === Phase 3 — poll + reconcile end-to-end ===================================
def _poll(db, reaction_map, *, fail=None, seed_maps=None, narrow=None):
    return st.poll(db, seed_maps or {}, ACE, 14,
                   token="x", opener=make_opener(reaction_map, fail=fail),
                   narrow_targets=narrow)


def test_poll_reconcile_end_to_end(tmp_path):
    db = tmp_path / "t.db"
    smaps = {M1: {"channel_id": CH, "seed_id": "gh-#1", "run_dir": "d",
                  "mtime": time.time()}}
    stats = _poll(db, {(CH, M1, "✅"): [ACE]}, seed_maps=smaps)
    assert stats["added"] == 1
    c = rs.connect(str(db))
    assert (CH, M1, "✅", ACE) in rs.current_present(c)
    # survives reopen
    c2 = rs.connect(str(db))
    assert (CH, M1, "✅", ACE) in rs.current_present(c2)


def test_unreact_flips_verdict_to_none(tmp_path):
    db = tmp_path / "t.db"
    smaps = {M1: {"channel_id": CH, "seed_id": "gh-#1", "run_dir": "d",
                  "mtime": time.time()}}
    _poll(db, {(CH, M1, "✅"): [ACE]}, seed_maps=smaps)
    # full clean poll, Ace removed his reaction → covers_removes True → removed
    stats = _poll(db, {}, seed_maps=smaps)
    assert stats["covers_removes"] is True
    assert stats["removed"] == 1
    c = rs.connect(str(db))
    assert (CH, M1, "✅", ACE) not in rs.current_present(c)


def test_unreact_then_rereact_shows_verdict_again(tmp_path):
    """AC-3b — THE pass-2 BLOCK gate. present → un-react → RE-react must recover."""
    db = tmp_path / "t.db"
    smaps = {M1: {"channel_id": CH, "seed_id": "gh-#1", "run_dir": "d",
                  "mtime": time.time()}}
    _poll(db, {(CH, M1, "✅"): [ACE]}, seed_maps=smaps)          # present
    _poll(db, {}, seed_maps=smaps)                               # un-react → gone
    _poll(db, {(CH, M1, "✅"): [ACE]}, seed_maps=smaps)          # RE-react
    c = rs.connect(str(db))
    assert (CH, M1, "✅", ACE) in rs.current_present(c), \
        "re-react silently lost — watermark guard regression (Variant-B bug)"


def test_partial_fetch_failure_does_not_remove(tmp_path):
    db = tmp_path / "t.db"
    smaps = {M1: {"channel_id": CH, "seed_id": "gh-#1", "run_dir": "d",
                  "mtime": time.time()}}
    _poll(db, {(CH, M1, "✅"): [ACE]}, seed_maps=smaps)          # ✅ durable
    # next poll: that message's ✅ GET 404s → covers_removes False → ✅ SURVIVES
    stats = _poll(db, {}, fail={(CH, M1, "✅")}, seed_maps=smaps)
    assert stats["covers_removes"] is False
    assert stats["removed"] == 0
    c = rs.connect(str(db))
    assert (CH, M1, "✅", ACE) in rs.current_present(c)


def test_remove_scope_requires_all_durable_keys(tmp_path):
    db = tmp_path / "t.db"
    smaps = {M1: {"channel_id": CH, "seed_id": "gh-#1", "run_dir": "d",
                  "mtime": time.time()}}
    _poll(db, {(CH, M1, "✅"): [ACE]}, seed_maps=smaps)
    # a narrow --message poll of a DIFFERENT message → add-only, M1's ✅ untouched
    stats = _poll(db, {}, narrow=[(CH, "OTHER")])
    assert stats["covers_removes"] is False
    assert stats["removed"] == 0
    c = rs.connect(str(db))
    assert (CH, M1, "✅", ACE) in rs.current_present(c)


def test_covers_removes_true_only_on_complete_clean_poll(tmp_path):
    db = tmp_path / "t.db"
    smaps = {M1: {"channel_id": CH, "seed_id": "gh-#1", "run_dir": "d",
                  "mtime": time.time()}}
    stats = _poll(db, {(CH, M1, "✅"): [ACE]}, seed_maps=smaps)
    assert stats["covers_removes"] is True


def test_repoll_unchanged_no_db_mutation(tmp_path):
    db = tmp_path / "t.db"
    smaps = {M1: {"channel_id": CH, "seed_id": "gh-#1", "run_dir": "d",
                  "mtime": time.time()}}
    _poll(db, {(CH, M1, "✅"): [ACE]}, seed_maps=smaps)
    c = rs.connect(str(db))
    led1 = c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    seq1 = c.execute("SELECT seq FROM reaction_state").fetchone()[0]
    _poll(db, {(CH, M1, "✅"): [ACE]}, seed_maps=smaps)  # identical
    c2 = rs.connect(str(db))
    led2 = c2.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    seq2 = c2.execute("SELECT seq FROM reaction_state").fetchone()[0]
    assert (led1, seq1) == (led2, seq2)


def test_covers_removes_false_over_random_fetch_failure_subsets(tmp_path):
    """Property test: for many random subsets of fetch failures / narrow targets,
    covers_removes is ALWAYS False AND no durable key is ever removed."""
    import random
    rnd = random.Random(1234)
    # seed 4 durable ✅ keys
    db = tmp_path / "t.db"
    msgs = [f"M{i}" for i in range(4)]
    smaps = {m: {"channel_id": CH, "seed_id": f"gh-#{i}", "run_dir": "d",
                 "mtime": time.time()} for i, m in enumerate(msgs)}
    full = {(CH, m, "✅"): [ACE] for m in msgs}
    st.poll(db, smaps, ACE, 14, token="x", opener=make_opener(full))
    for _ in range(40):
        # randomly fail a non-empty subset of message ✅ fetches
        k = rnd.randint(1, 4)
        failed = set(rnd.sample(msgs, k))
        fail = {(CH, m, "✅") for m in failed} | {(CH, m, "👍") for m in failed} \
            | {(CH, m, "❌") for m in failed}
        stats = st.poll(db, smaps, ACE, 14, token="x",
                        opener=make_opener(full, fail=fail))
        assert stats["covers_removes"] is False
        assert stats["removed"] == 0
        c = rs.connect(str(db))
        present = {mk for (_c, mk, _e, _u) in rs.current_present(c)}
        assert present == set(msgs), "a fetch-fail must never delete a durable key"


# === Phase 3 — flock lock (INV-9) ============================================
def test_second_poll_is_busy_not_partial(tmp_path):
    db = tmp_path / "t.db"
    rs.connect(str(db)).close()  # create the db file
    held = st.poll_lock(db)
    held.__enter__()
    try:
        with pytest.raises(st._FlockBusy):
            with st.poll_lock(db):
                pass
    finally:
        held.__exit__()


def _hold_lock_and_die(db_path, ready_path):
    # acquire the flock in a child, signal ready, then exit (dropping the lock).
    lk = st.poll_lock(db_path)
    lk.__enter__()
    Path(ready_path).write_text("ready")
    os._exit(0)  # die WITHOUT releasing manually → flock must auto-release


def test_lock_auto_releases_after_holder_exits(tmp_path):
    db = tmp_path / "t.db"
    rs.connect(str(db)).close()
    ready = tmp_path / "ready"
    ctx = multiprocessing.get_context("fork")
    p = ctx.Process(target=_hold_lock_and_die, args=(str(db), str(ready)))
    p.start()
    p.join(5)
    # child is dead; its flock must have auto-released → we acquire cleanly,
    # with NO manual unlink of the lockfile.
    with st.poll_lock(db):
        pass  # acquired without _FlockBusy


# === Phase 4 — report ========================================================
def _seed_maps_one(mtime=None):
    return {M1: {"channel_id": CH, "seed_id": "gh-2026-06-27-#1", "run_dir": "d",
                 "mtime": mtime if mtime is not None else time.time()}}


def test_verdict_precedence_x_beats_check_beats_thumb():
    # ✅ AND 👍 BOTH durably present at once → ✅ wins (precedence only matters here).
    c = _conn()
    for e, sq in (("✅", 1), ("👍", 1)):
        rs.apply_event(c, {"channel_id": CH, "message_id": M1, "emoji": e,
                           "user_id": ACE, "action": "add", "seq": sq})
    assert st._verdict_for_message(c, CH, M1, ACE) == "✅"
    # add ❌ too → ❌ wins
    rs.apply_event(c, {"channel_id": CH, "message_id": M1, "emoji": "❌",
                       "user_id": ACE, "action": "add", "seq": 1})
    assert st._verdict_for_message(c, CH, M1, ACE) == "❌"


def test_report_maps_message_to_seed(capsys):
    c = _conn()
    rs.apply_event(c, {"channel_id": CH, "message_id": M1, "emoji": "✅",
                       "user_id": ACE, "action": "add", "seq": 1})
    res = st.report(c, _seed_maps_one(), {"gh-2026-06-27-#1": "Reaction UX"},
                    ACE, 14)
    assert res["rows"][0]["seed_id"] == "gh-2026-06-27-#1"
    assert res["rows"][0]["emoji"] == "✅"


def test_report_row_has_no_fabricated_since(capsys):
    c = _conn()
    rs.apply_event(c, {"channel_id": CH, "message_id": M1, "emoji": "✅",
                       "user_id": ACE, "action": "add", "seq": 1})
    st.report(c, _seed_maps_one(), {}, ACE, 14)
    out = capsys.readouterr().out
    assert "reconcile" not in out  # the core's hardcoded ts must never surface


def test_report_prints_configured_ace_id(capsys):
    st.report(_conn(), {}, {}, ACE, 14)
    assert ACE in capsys.readouterr().out


def test_report_seed_with_no_reaction_shows_none():
    res = st.report(_conn(), _seed_maps_one(), {}, ACE, 14)
    assert res["rows"] == []


def test_report_orphan_key_labeled_unknown_seed():
    c = _conn()
    rs.apply_event(c, {"channel_id": CH, "message_id": "ORPHAN", "emoji": "✅",
                       "user_id": ACE, "action": "add", "seq": 1})
    res = st.report(c, {}, {}, ACE, 14)
    assert any(r["seed_id"] == "unknown-seed" for r in res["rows"])


def test_report_surfaces_aged_out_untriaged_cards():
    # a seed map older than the window with no durable verdict → surfaced (D-12).
    old = time.time() - 30 * 86400
    res = st.report(_conn(), _seed_maps_one(mtime=old), {}, ACE, 14)
    assert "gh-2026-06-27-#1" in res["aged_out_untriaged"]


def test_report_json_shape(capsys):
    res = st.report(_conn(), {}, {}, ACE, 14, as_json=True)
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert set(parsed) == {"ace_user_id", "rows", "aged_out_untriaged"}


def test_report_works_offline():
    # no token/network used at all
    st.report(_conn(), {}, {}, ACE, 14)


def test_seed_map_schema_drift_is_loud(tmp_path, capsys):
    d = tmp_path / "2026-06-27"
    d.mkdir()
    (d / "seed_messages.json").write_text('{"wrong":"shape"}')
    maps = st.load_seed_maps(tmp_path)
    err = capsys.readouterr().err
    assert maps == {}
    assert "WARNING" in err and "malformed" in err


# === Phase 5 — selfcheck / check-target ======================================
def test_selfcheck_offline_passes():
    assert st.selfcheck() is True


def test_selfcheck_does_not_require_token(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    assert st.selfcheck() is True


def test_check_target_fails_without_token(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    ok, msg = st.check_target(ACE)  # no token, default opener → APIError
    assert ok is False


def test_check_target_passes_on_reachable(capsys):
    ok, msg = st.check_target(ACE, token="x", opener=make_opener({}))
    assert ok is True
    assert "BOT123" in msg and ACE in msg


def test_check_target_fails_on_api_error():
    def opener(req, timeout=20):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    ok, _ = st.check_target(ACE, token="x", opener=opener)
    assert ok is False


def test_check_target_is_rest_liveness():
    # D-14: seed_triage must NOT call the core's journal check_target. Grep source.
    src = Path(st.__file__).read_text(encoding="utf-8")
    assert "rs.check_target" not in src
    assert "reaction_state.check_target" not in src


# === Phase 6 — CLI ===========================================================
def test_main_selfcheck_flag_exit_zero():
    assert st.main(["--selfcheck"]) == 0


def test_cli_args_accepted_after_subcommand(tmp_path, monkeypatch, capsys):
    # Regression (caught by the live e2e): the spec's nightly entry writes
    # `poll --db "$D" --seeds-dir "$S" --window-days 14`, i.e. shared args AFTER
    # the subcommand. They must parse, not error "unrecognized arguments".
    db = tmp_path / "t.db"
    monkeypatch.setattr(st, "load_seed_maps", lambda d: {})
    monkeypatch.setattr(st, "poll", lambda *a, **k: {
        "targets": 0, "polled": 0, "fetch_fail": 0, "ace_reactions": 0,
        "added": 0, "removed": 0, "covers_removes": True,
        "uncovered_keys": [], "narrow": False})
    rc = st.main(["poll", "--db", str(db), "--seeds-dir", str(tmp_path),
                  "--window-days", "7"])
    assert rc == 0
    # and report-after-subcommand too
    rc2 = st.main(["report", "--db", str(db), "--seeds-dir", str(tmp_path)])
    assert rc2 == 0


def test_full_poll_covers_removes_false_alerts_with_dead_key(tmp_path, capsys, monkeypatch):
    db = tmp_path / "t.db"
    smaps = _seed_maps_one()
    # seed a durable ✅, then make a full poll where that message's fetch dies.
    st.poll(db, smaps, ACE, 14, token="x", opener=make_opener({(CH, M1, "✅"): [ACE]}))
    # monkeypatch poll to use a failing opener via the CLI path is heavy; assert the
    # stats→alert mapping directly through main by patching st.poll.
    def fake_poll(*a, **k):
        return {"targets": 1, "polled": 1, "fetch_fail": 1, "ace_reactions": 0,
                "added": 0, "removed": 0, "covers_removes": False,
                "uncovered_keys": [(CH, M1)], "narrow": False}
    monkeypatch.setattr(st, "poll", fake_poll)
    monkeypatch.setattr(st, "load_seed_maps", lambda d: smaps)
    rc = st.main(["--db", str(db), "--seeds-dir", str(tmp_path), "poll"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "COVERS_REMOVES_DISABLED" in err
    assert f"{CH}/{M1}" in err


def test_cli_poll_then_report_end_to_end(tmp_path, monkeypatch, capsys):
    db = tmp_path / "t.db"
    smaps = _seed_maps_one()
    monkeypatch.setattr(st, "load_seed_maps", lambda d: smaps)
    monkeypatch.setattr(st, "load_seed_titles", lambda d: {"gh-2026-06-27-#1": "X"})
    # real poll via injected opener (populates the durable DB)
    st.poll(db, smaps, ACE, 14, token="x", opener=make_opener({(CH, M1, "✅"): [ACE]}))
    rc = st.main(["--db", str(db), "--seeds-dir", str(tmp_path), "report"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "gh-2026-06-27-#1" in out and "✅" in out


# === invariants ==============================================================
def test_core_consumed_unmodified():
    # INV-1: seed_triage calls the core's public API, does not redefine it.
    src = Path(st.__file__).read_text(encoding="utf-8")
    assert "from tools import reaction_state" in src
    assert "rs.reconcile" in src and "rs.current_present" in src and "rs.connect" in src


_THIRD_PARTY_FORBIDDEN = {"discord", "requests", "websockets", "aiohttp"}


def test_seed_triage_no_third_party_imports():
    tree = ast.parse(Path(st.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                roots.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    assert not (roots & _THIRD_PARTY_FORBIDDEN), f"third-party import: {roots & _THIRD_PARTY_FORBIDDEN}"
