"""Offline tests for tools.brief_delta.

The important pass-3 gate is CB-2: MOVED/UNCHANGED must be reachable under a
normal nightly cadence where the immediate prior is one day old. Recurrence is
found through the whole-store last-seen index, while RESOLVED remains scoped to
the immediate prior only.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from tools import brief_delta as bd

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
JUL03 = FIXTURES / "render_input_2026-07-03.json"
JUN29 = FIXTURES / "render_input_2026-06-29.json"


def load_fixture(path: Path) -> bd.Snapshot:
    return bd.load_source(path)


def item(url: str, score: int, section: str = "selected", title: str = "fixture item", source: str = "X") -> bd.Item:
    raw = {"url": url, "score": score, "source": source, "title": title}
    return bd._item_from_raw(raw, section, 0)


def snapshot(day: str, items: list[bd.Item]) -> bd.Snapshot:
    return bd.Snapshot(bd.date.fromisoformat(day), tuple(items))


def write_snap(state_dir: Path, day: str, items) -> Path:
    return bd.write_snapshot(state_dir, snapshot(day, items))


# --- source loading ----------------------------------------------------------
def test_load_flattens_and_tags_section():
    snap = load_fixture(JUL03)
    assert snap.brief_date.isoformat() == "2026-07-03"
    assert len(snap.items) == 7
    assert [i.section for i in snap.items].count("selected") == 5
    assert [i.section for i in snap.items].count("also") == 2


def test_load_real_captured_sample_has_expected_shape():
    snap = load_fixture(JUL03)
    assert len(snap.items) == 7
    assert all(i.url and i.score and i.source for i in snap.items)
    assert any("emollick" in i.url for i in snap.items)
    assert any(i.raw.get("tweet_text") for i in snap.items)
    assert any(i.raw.get("title") for i in snap.items)


@pytest.mark.parametrize("doc, reason", [
    ({"selected": [], "also": [], "ts": "2026-07-03T00:00:00"}, "zero"),
    ({"selected": [], "also": [{"score": 1, "source": "X"}], "ts": "2026-07-03T00:00:00"}, "url"),
    ({"selected": []}, "selected and also"),
])
def test_load_raises_on_bad_shape(tmp_path, doc, reason):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(bd.LivenessError, match=reason):
        bd.load_source(p)


def test_load_raises_on_non_json_and_non_file(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not-json", encoding="utf-8")
    with pytest.raises(bd.LivenessError, match="valid JSON"):
        bd.load_source(bad)
    with pytest.raises(bd.LivenessError, match="not a regular file"):
        bd.load_source(tmp_path)


# --- index + classifier ------------------------------------------------------
def test_index_folds_to_most_recent_prior_occurrence():
    u = "https://Example.com/a/"
    store = (
        snapshot("2026-06-01", [item(u, 10)]),
        snapshot("2026-06-03", [item(u, 30, "also")]),
    )
    index = bd.build_last_seen_index(store, bd.date.fromisoformat("2026-06-04"))
    ref = index[bd.normalize_url(u)]
    assert ref.brief_date.isoformat() == "2026-06-03"
    assert ref.item.score == 30
    assert ref.item.section == "also"


def test_index_excludes_today_and_future():
    u = "https://example.com/today"
    store = (
        snapshot("2026-06-01", [item(u, 10)]),
        snapshot("2026-06-02", [item(u, 20)]),
        snapshot("2026-06-03", [item(u, 30)]),
    )
    index = bd.build_last_seen_index(store, bd.date.fromisoformat("2026-06-02"))
    assert index[bd.normalize_url(u)].brief_date.isoformat() == "2026-06-01"


def test_classify_new_moved_unchanged_and_resolved():
    moved = "https://example.com/moved"
    same = "https://example.com/same"
    new = "https://example.com/new"
    gone = "https://example.com/gone"
    store = (snapshot("2026-06-01", [item(moved, 80), item(same, 70), item(gone, 60)]),)
    today = (item(moved, 90), item(same, 74), item(new, 88))
    index = bd.build_last_seen_index(store, bd.date.fromisoformat("2026-06-09"))
    classes = bd.classify(today, index, store[0], move_threshold=5)
    assert [i.url for i in classes[bd.NEW]] == [new]
    assert [r[0].url for r in classes[bd.MOVED]] == [moved]
    assert [r[0].url for r in classes[bd.UNCHANGED]] == [same]
    assert [i.url for i in classes[bd.RESOLVED]] == [gone]


def test_moved_threshold_boundary():
    u1 = "https://example.com/boundary"
    u2 = "https://example.com/over"
    store = (snapshot("2026-06-01", [item(u1, 80), item(u2, 80)]),)
    today = (item(u1, 85), item(u2, 86))
    classes = bd.classify(today, bd.build_last_seen_index(store, bd.date.fromisoformat("2026-06-09")), None, 5)
    assert [r[0].url for r in classes[bd.UNCHANGED]] == [u1]
    assert [r[0].url for r in classes[bd.MOVED]] == [u2]


def test_moved_fires_under_nightly_cadence():
    """Daily-adjacent store, newest prior is 1 day old; recurrence is D-8."""
    real = load_fixture(JUL03).items[0]
    stable = load_fixture(JUL03).items[1]
    store = []
    for offset in range(8):
        day = bd.date(2026, 6, 1) + bd.timedelta(days=offset)
        items = [item(f"https://example.com/nightly-{offset}", 40 + offset)]
        if offset == 0:
            items.extend([
                item(real.url, real.score, real.section, real.title, real.source),
                item(stable.url, stable.score, stable.section, stable.title, stable.source),
            ])
        store.append(bd.Snapshot(day, tuple(items)))
    today = bd.Snapshot(bd.date(2026, 6, 9), (
        item(real.url, real.score + 10, real.section, real.title, real.source),
        item(stable.url, stable.score, stable.section, stable.title, stable.source),
        item("https://example.com/new", 99),
    ))
    delta = bd.make_delta(today, bd.StoreLoad(tuple(store), len(store), ()), 5)
    assert delta.gap_days == 1
    assert delta.regime == "in-window"
    assert [r[1].brief_date.isoformat() for r in delta.classes[bd.MOVED]] == ["2026-06-01"]
    assert [r[0].url for r in delta.classes[bd.MOVED]] == [real.url]
    assert [r[0].url for r in delta.classes[bd.UNCHANGED]] == [stable.url]


def test_delta_across_two_real_days_is_in_window_changelog():
    prior = load_fixture(JUN29)
    today = load_fixture(JUL03)
    delta = bd.make_delta(today, bd.StoreLoad((prior,), 1, ()), 5)
    assert delta.gap_days == 4
    assert delta.gap_days <= bd.PRODUCER_DEDUP_DAYS
    assert delta.regime == "in-window"
    assert len(delta.classes[bd.NEW]) == 7
    assert len(delta.classes[bd.RESOLVED]) == 7
    assert len(delta.classes[bd.MOVED]) == 0
    assert len(delta.classes[bd.UNCHANGED]) == 0
    out = bd.render_delta(delta)
    assert "in-window" in out
    assert "nothing resurfaced" in out


def test_resolved_does_not_flood_from_old_store():
    old_only = "https://example.com/old-only"
    immediate_only = "https://example.com/yesterday"
    today_url = "https://example.com/today"
    store = (
        snapshot("2026-06-10", [item(old_only, 60)]),
        snapshot("2026-06-29", [item(immediate_only, 70)]),
    )
    today = bd.Snapshot(bd.date(2026, 6, 30), (item(today_url, 80),))
    delta = bd.make_delta(today, bd.StoreLoad(store, 2, ()), 5)
    assert [i.url for i in delta.classes[bd.RESOLVED]] == [immediate_only]


def test_index_reads_across_full_retention():
    recurring = "https://example.com/retained"
    store = (
        snapshot("2026-06-01", [item(recurring, 50)]),
        snapshot("2026-06-30", [item("https://example.com/yesterday", 51)]),
    )
    today = bd.Snapshot(bd.date(2026, 7, 1), (item(recurring, 70),))
    delta = bd.make_delta(today, bd.StoreLoad(store, 2, ()), 5)
    assert delta.gap_days == 1
    assert [r[0].url for r in delta.classes[bd.MOVED]] == [recurring]
    assert [r[1].brief_date.isoformat() for r in delta.classes[bd.MOVED]] == ["2026-06-01"]


# --- store, retention, render ------------------------------------------------
def test_same_day_rerun_overwrites_and_diffs_prior_history(tmp_path):
    old = "https://example.com/old"
    today_url = "https://example.com/today"
    write_snap(tmp_path, "2026-06-01", [item(old, 10)])
    write_snap(tmp_path, "2026-06-02", [item(today_url, 20)])
    store = bd.load_store(tmp_path)
    today = bd.Snapshot(bd.date(2026, 6, 2), (item(old, 30),))
    delta = bd.make_delta(today, store, 5)
    assert [r[0].url for r in delta.classes[bd.MOVED]] == [old]
    assert all(r[1].brief_date.isoformat() == "2026-06-01" for r in delta.classes[bd.MOVED])


def test_store_prunes_to_retention_window(tmp_path):
    for offset in range(40):
        day = bd.date(2026, 1, 1) + bd.timedelta(days=offset)
        write_snap(tmp_path, day.isoformat(), [item(f"https://example.com/{offset}", offset)])
    removed = bd.prune_store(tmp_path, bd.date(2026, 2, 9), 35)
    remaining = [bd._date_from_snapshot_path(p) for p in tmp_path.glob("snapshot-*.json")]
    remaining_dates = sorted(d.isoformat() for d in remaining if d is not None)
    assert removed
    assert remaining_dates[0] == "2026-01-05"
    assert remaining_dates[-1] == "2026-02-09"


def test_retention_window_exceeds_producer_dedup_window():
    assert bd.DEFAULT_RETENTION_DAYS >= 5 * bd.PRODUCER_DEDUP_DAYS


def test_render_order_and_collapse():
    prior = snapshot("2026-06-01", [item("https://example.com/gone", 70), item("https://example.com/same", 40)])
    today = bd.Snapshot(bd.date(2026, 6, 9), (item("https://example.com/new", 90), item("https://example.com/same", 41)))
    delta = bd.make_delta(today, bd.StoreLoad((prior,), 1, ()), 5)
    out = bd.render_delta(delta)
    assert out.index("⭐ NEW") < out.index("✓ RESOLVED") < out.index("… 1 unchanged")
    assert "↕ MOVED" not in out


def test_render_is_byte_deterministic():
    prior = load_fixture(JUN29)
    today = load_fixture(JUL03)
    delta1 = bd.make_delta(today, bd.StoreLoad((prior,), 1, ()), 5)
    delta2 = bd.make_delta(today, bd.StoreLoad((prior,), 1, ()), 5)
    assert bd.render_delta(delta1).encode() == bd.render_delta(delta2).encode()


def test_render_real_pair_smoke():
    out = bd.render_delta(bd.make_delta(load_fixture(JUL03), bd.StoreLoad((load_fixture(JUN29),), 1, ()), 5))
    assert "prior 2026-06-29" in out
    assert "gap 4d" in out
    assert "in-window" in out
    assert "index folded 1 of 1 snapshots" in out
    assert "https://x.com/emollick/status/2072872373758382497" in out
    assert "https://x.com/kocer_eth/status/2071288514608800108" in out
    assert "↕ MOVED" not in out
    assert "unchanged (carried over)" not in out


def test_render_no_crash_on_markdown_newlines():
    raw = {"url": "https://example.com/md", "score": 10, "source": "X", "tweet_text": "**bold**\nsecond line"}
    today = bd.Snapshot(bd.date(2026, 7, 1), (bd._item_from_raw(raw, "selected", 0),))
    out = bd.render_delta(bd.make_delta(today, bd.StoreLoad((), 0, ()), 5))
    assert "baseline" in out


# --- corrupt prior + CLI probes ---------------------------------------------
def test_corrupt_prior_header_distinct_from_true_baseline(tmp_path, capsys):
    today = load_fixture(JUL03)
    true_base = bd.render_delta(bd.make_delta(today, bd.StoreLoad((), 0, ()), 5))
    write_snap(tmp_path, "2026-06-29", load_fixture(JUN29).items)
    (tmp_path / "snapshot-2026-07-02.json").write_text("{bad", encoding="utf-8")
    rc = bd.run_render(JUL03, tmp_path, 5, 35)
    out = capsys.readouterr().out
    assert rc == 0
    assert "⚠ prior snapshot unreadable" in out
    assert "corrupt-prior" in out
    assert out.splitlines()[0] != true_base.splitlines()[0]


def test_corrupt_non_immediate_snapshot_is_counted_not_fatal(tmp_path):
    write_snap(tmp_path, "2026-06-29", load_fixture(JUN29).items)
    (tmp_path / "snapshot-2026-06-30.json").write_text("not json", encoding="utf-8")
    write_snap(tmp_path, "2026-07-02", [item("https://example.com/yesterday", 1)])
    store = bd.load_store(tmp_path)
    delta = bd.make_delta(load_fixture(JUL03), store, 5)
    assert delta.regime == "in-window"
    assert delta.corrupt_count == 1
    assert "skipped 1 unreadable non-immediate" in bd.render_delta(delta)


def test_selfcheck_builds_own_fixture_and_passes(monkeypatch, capsys):
    monkeypatch.setattr(bd, "DEFAULT_SOURCE", Path("/definitely/not/used.json"))
    assert bd.main(["--selfcheck"]) == 0
    assert "SELFCHECK OK" in capsys.readouterr().out


def test_check_target_and_selfcheck_are_distinct(tmp_path, capsys):
    missing = tmp_path / "missing.json"
    assert bd.main(["--check-target", "--source", str(missing)]) == 2
    assert "LIVENESS FAILURE" in capsys.readouterr().err
    assert bd.main(["--selfcheck", "--source", str(missing)]) == 0


@pytest.mark.parametrize("payload, message", [
    ("not-json", "valid JSON"),
    (json.dumps({"selected": [], "also": [], "ts": "2026-07-03T00:00:00"}), "zero"),
    (json.dumps({"selected": [{"score": 1, "source": "X"}], "also": [], "ts": "2026-07-03T00:00:00"}), "url"),
    (json.dumps({"selected": [], "ts": "2026-07-03T00:00:00"}), "selected and also"),
])
def test_check_target_loud_fail_matrix(tmp_path, capsys, payload, message):
    source = tmp_path / "source.json"
    source.write_text(payload, encoding="utf-8")
    assert bd.main(["--check-target", "--source", str(source)]) == 2
    err = capsys.readouterr().err
    assert "LIVENESS FAILURE" in err
    assert message in err


def test_check_target_not_a_file_exits_2(tmp_path, capsys):
    assert bd.main(["--check-target", "--source", str(tmp_path)]) == 2
    assert "not a regular file" in capsys.readouterr().err


def test_check_target_fixture_exits_0(capsys):
    assert bd.main(["--check-target", "--source", str(JUL03)]) == 0
    assert f"OK: {JUL03} live, 7 items" in capsys.readouterr().out


def test_render_run_on_empty_source_exits_nonzero(tmp_path, capsys):
    source = tmp_path / "empty.json"
    source.write_text(json.dumps({"selected": [], "also": [], "ts": "2026-07-03T00:00:00"}), encoding="utf-8")
    assert bd.main(["--source", str(source), "--state-dir", str(tmp_path / "state")]) == 2
    assert "LIVENESS FAILURE" in capsys.readouterr().err


def test_unknown_flag_exits_nonzero():
    with pytest.raises(SystemExit) as exc:
        bd.main(["--definitely-unknown"])
    assert exc.value.code != 0


def test_normal_run_writes_only_under_state_dir_and_source_read_only(tmp_path, capsys):
    source = tmp_path / "source.json"
    source.write_bytes(JUL03.read_bytes())
    before = source.read_bytes()
    os.chmod(source, 0o444)
    state = tmp_path / "state"
    try:
        assert bd.main(["--source", str(source), "--state-dir", str(state)]) == 0
    finally:
        os.chmod(source, 0o644)
    assert source.read_bytes() == before
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()) == [
        "source.json",
        "state/snapshot-2026-07-03.json",
    ]
    assert "baseline" in capsys.readouterr().out


def test_default_source_is_real_path():
    assert bd.DEFAULT_SOURCE == Path.home() / ".hermes" / "state" / "cron" / "morning-digest" / "_render_input.json"


def test_imports_are_stdlib_only():
    tree = ast.parse(Path(bd.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported - {"__future__", "argparse", "json", "pathlib", "sys", "dataclasses", "datetime", "typing", "urllib"})
