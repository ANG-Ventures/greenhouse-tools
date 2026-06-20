"""Tests for tools.triage_rule_miner.miner — collect & pass offline, stdlib only."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from tools.triage_rule_miner import miner


# --- fixtures ---------------------------------------------------------------
def _rec(sender, subject, action, ts="2026-06-01T03:10:00Z"):
    return {"sender": sender, "subject": subject, "action": action, "ts": ts}


def _ndjson(records):
    return "\n".join(json.dumps(r) for r in records)


def _write_ndjson(tmp_path, records):
    p = tmp_path / "behavior.ndjson"
    p.write_text(_ndjson(records), encoding="utf-8")
    return p


# --- normalize --------------------------------------------------------------
def test_normalize_collapses_digits_to_shape():
    a = miner.normalize_subject("deploy succeeded #4821")
    b = miner.normalize_subject("deploy succeeded #4822")
    assert a == b
    assert "4821" not in a


def test_normalize_collapses_hex_and_whitespace():
    a = miner.normalize_subject("build   deadbeef00  ok")
    b = miner.normalize_subject("build cafe1234ff ok")
    assert a == b


# --- load -------------------------------------------------------------------
def test_load_skips_blank_lines(tmp_path):
    p = tmp_path / "b.ndjson"
    p.write_text(
        _ndjson([_rec("a@x", "s", "archived_unread")]) + "\n\n", encoding="utf-8"
    )
    assert len(miner.load(p)) == 1


def test_load_rejects_malformed_json(tmp_path):
    p = tmp_path / "b.ndjson"
    p.write_text("{not valid json}\n", encoding="utf-8")
    try:
        miner.load(p)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_load_rejects_unknown_action(tmp_path):
    p = _write_ndjson(tmp_path, [_rec("a@x", "s", "teleported")])
    try:
        miner.load(p)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_load_rejects_missing_field(tmp_path):
    p = tmp_path / "b.ndjson"
    p.write_text(json.dumps({"sender": "a@x", "subject": "s"}) + "\n", encoding="utf-8")
    try:
        miner.load(p)
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- gate (the load-bearing invariant) --------------------------------------
def test_gate_accepts_deterministic_pattern():
    recs = [_rec("deploys@ci", "deploy succeeded #%d" % i, "archived_unread") for i in range(10)]
    proposals = miner.mine(recs)
    assert len(proposals) == 1
    p = proposals[0]
    assert p["confidence"] == 1.0
    assert p["support"] == 10
    assert p["action"] == "archive_unread"


def test_gate_rejects_below_support():
    # 7 events < MIN_SUPPORT(8), all deterministic
    recs = [_rec("deploys@ci", "deploy #%d" % i, "archived_unread") for i in range(7)]
    assert miner.mine(recs) == []


def test_gate_rejects_below_confidence():
    # 12 events, but only 8 archived_unread => 0.666 < 0.90
    recs = [_rec("noisy@x", "ping #%d" % i, "archived_unread") for i in range(8)]
    recs += [_rec("noisy@x", "ping #%d" % i, "replied") for i in range(4)]
    assert miner.mine(recs) == []


def test_gate_boundary_exactly_90_percent_and_min_support():
    # 9 archived_unread of 10 total => 0.90 exactly, support 9 >= 8 => accepted
    recs = [_rec("edge@x", "n #%d" % i, "archived_unread") for i in range(9)]
    recs += [_rec("edge@x", "n #9", "replied")]
    proposals = miner.mine(recs)
    assert len(proposals) == 1
    assert proposals[0]["confidence"] == 0.9


def test_proposals_capped_and_ranked():
    recs = []
    # 12 distinct deterministic senders, each with descending support
    for s in range(12):
        n = 8 + s  # 8..19 support, all >= MIN_SUPPORT, conf 1.0
        recs += [_rec("s%02d@x" % s, "tag", "archived_unread") for _ in range(n)]
    proposals = miner.mine(recs)
    assert len(proposals) == miner.MAX_PROPOSALS
    supports = [p["support"] for p in proposals]
    assert supports == sorted(supports, reverse=True)


# --- render -----------------------------------------------------------------
def test_render_md_empty_is_honest():
    out = miner.render_md([])
    assert "Nothing to propose" in out


def test_render_md_lists_one_tap():
    recs = [_rec("deploys@ci", "deploy #%d" % i, "archived_unread") for i in range(10)]
    out = miner.render_md(miner.mine(recs))
    assert "deploys@ci" in out
    assert "support: 10" in out


# --- selfcheck (deploy health probe) ----------------------------------------
def test_selfcheck_good_input_returns_true():
    assert miner.selfcheck(miner._GOOD_FIXTURE) is True


def test_selfcheck_corrupt_input_raises():
    try:
        miner.selfcheck("{garbage\n")
        assert False, "expected ValueError on corrupt input"
    except ValueError:
        pass


def test_run_selfcheck_exit_codes(capsys):
    assert miner._run_selfcheck() == 0


def test_main_selfcheck_flag_exit_zero():
    assert miner.main(["--selfcheck"]) == 0


# --- invariant: no state written outside --out ------------------------------
def test_no_state_outside_out_dir(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    in_path = src / "behavior.ndjson"
    in_path.write_text(
        _ndjson([_rec("deploys@ci", "deploy #%d" % i, "archived_unread") for i in range(10)]),
        encoding="utf-8",
    )
    before = {p for p in tmp_path.rglob("*") if p.is_file()}

    out_dir = tmp_path / "out"
    rc = miner.main(["--in", str(in_path), "--out", str(out_dir)])
    assert rc == 0

    after = {p for p in tmp_path.rglob("*") if p.is_file()}
    new_files = after - before
    # every new file must live under out_dir
    for f in new_files:
        assert str(f).startswith(str(out_dir)), f"wrote outside --out: {f}"
    assert (out_dir / "proposals.json").exists()
    assert (out_dir / "proposals.md").exists()


def test_proposals_json_well_formed(tmp_path):
    in_path = _write_ndjson(
        tmp_path,
        [_rec("deploys@ci", "deploy #%d" % i, "archived_unread") for i in range(10)],
    )
    out_dir = tmp_path / "out"
    miner.main(["--in", str(in_path), "--out", str(out_dir)])
    data = json.loads((out_dir / "proposals.json").read_text(encoding="utf-8"))
    assert data["version"] == "0.1"
    assert len(data["proposals"]) == 1


# --- invariant: stdlib only (AST walk, no third-party import) ---------------
_STDLIB_OK = {
    "__future__", "argparse", "json", "re", "sys", "collections",
    "pathlib", "tempfile", "ast",
}


def test_miner_imports_stdlib_only():
    src_path = Path(miner.__file__)
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
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


# --- invariant: no network / subprocess / filter-mutation imports -----------
_FORBIDDEN_IMPORT_ROOTS = {
    "requests", "urllib", "urllib2", "http", "socket", "ssl",
    "subprocess", "smtplib", "ftplib", "asyncio",
}


def test_no_network_or_subprocess_imports():
    """The package must not import any networking/subprocess module — proof it
    cannot call out to gws, the Gmail API, or mutate remote state."""
    tree = ast.parse(Path(miner.__file__).read_text(encoding="utf-8"))
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
