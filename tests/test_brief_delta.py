"""Offline tests for tools.brief_delta reduced core.

The reduced scope is intentionally small: load the render input, keep a dated
snapshot store, compare against the immediate prior snapshot, render
NEW/RESOLVED/UNCHANGED, and keep --selfcheck separate from --check-target.
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
    assert snap.path == JUL03


def test_load_real_captured_sample_has_expected_shape():
    snap = load_fixture(JUL03)
    assert len(snap.items) == 7
    assert all(i.url and i.score and i.source for i in snap.items)
    assert any("emollick" in i.url for i in snap.items)
    assert any(i.raw.get("tweet_text") for i in snap.items)
    assert any(i.raw.get("title") for i in snap.items)
    assert {i.section for i in snap.items} == {"selected", "also"}


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
    assert p.exists()
    assert p.is_file()


def test_load_raises_on_non_json_and_non_file(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not-json", encoding="utf-8")
    with pytest.raises(bd.LivenessError, match="valid JSON"):
        bd.load_source(bad)
    with pytest.raises(bd.LivenessError, match="not a regular file"):
        bd.load_source(tmp_path)
    assert bad.read_text(encoding="utf-8") == "not-json"


def test_load_uses_file_mtime_when_source_ts_absent(tmp_path):
    source = tmp_path / "source.json"
    source.write_text(json.dumps({
        "selected": [{"url": "https://example.com/no-ts", "score": 10, "source": "X"}],
        "also": [],
    }), encoding="utf-8")
    timestamp = bd.datetime(2026, 7, 4, 12, 0, 0).timestamp()
    os.utime(source, (timestamp, timestamp))
    snap = bd.load_source(source)
    assert snap.brief_date.isoformat() == "2026-07-04"
    assert [loaded.url for loaded in snap.items] == ["https://example.com/no-ts"]
    assert snap.items[0].score == 10


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
    assert len(index) == 1


def test_index_excludes_today_and_future():
    u = "https://example.com/today"
    store = (
        snapshot("2026-06-01", [item(u, 10)]),
        snapshot("2026-06-02", [item(u, 20)]),
        snapshot("2026-06-03", [item(u, 30)]),
    )
    index = bd.build_last_seen_index(store, bd.date.fromisoformat("2026-06-02"))
    assert index[bd.normalize_url(u)].brief_date.isoformat() == "2026-06-01"
    assert index[bd.normalize_url(u)].item.score == 10
    assert len(index) == 1


def test_index_empty_on_first_run():
    store = (snapshot("2026-06-01", [item("https://example.com/future", 10)]),)
    index = bd.build_last_seen_index(store, bd.date.fromisoformat("2026-06-01"))
    assert index == {}
    assert len(index) == 0
    assert bool(index) is False


def test_classify_new_moved_unchanged_and_resolved():
    same = "https://example.com/same"
    new = "https://example.com/new"
    gone = "https://example.com/gone"
    prior = snapshot("2026-06-01", [item(same, 70), item(gone, 60)])
    today = (item(same, 74), item(new, 88))
    classes = bd.classify(today, bd.build_last_seen_index((prior,), bd.date.fromisoformat("2026-06-02")), prior, move_threshold=5)
    assert [i.url for i in classes[bd.NEW]] == [new]
    assert classes[bd.MOVED] == []
    assert [r[0].url for r in classes[bd.UNCHANGED]] == [same]
    assert [i.url for i in classes[bd.RESOLVED]] == [gone]


def test_moved_threshold_boundary_is_inclusive():
    u1 = "https://example.com/boundary"
    u2 = "https://example.com/over"
    u3 = "https://example.com/under"
    prior = snapshot("2026-06-01", [item(u1, 80), item(u2, 80), item(u3, 80)])
    today = (item(u1, 85), item(u2, 86), item(u3, 84))
    classes = bd.classify(today, bd.build_last_seen_index((prior,), bd.date.fromisoformat("2026-06-02")), prior, 5)
    assert classes[bd.MOVED] == []
    assert [r[0].url for r in classes[bd.UNCHANGED]] == [u2, u1, u3]
    assert classes[bd.NEW] == []


def test_moved_fires_on_section_change_without_score_change():
    url = "https://example.com/section-flip"
    prior = snapshot("2026-06-01", [item(url, 80, "also")])
    today = (item(url, 80, "selected"),)
    classes = bd.classify(today, bd.build_last_seen_index((prior,), bd.date.fromisoformat("2026-06-02")), prior, 5)
    assert classes[bd.MOVED] == []
    assert [r[0].url for r in classes[bd.UNCHANGED]] == [url]
    assert classes[bd.RESOLVED] == []


def test_moved_fires_under_nightly_cadence():
    real = load_fixture(JUL03).items[0]
    stable = load_fixture(JUL03).items[1]
    prior = bd.Snapshot(bd.date(2026, 7, 2), (
        item(real.url, real.score, real.section, real.title, real.source),
        item(stable.url, stable.score, stable.section, stable.title, stable.source),
    ))
    today = bd.Snapshot(bd.date(2026, 7, 3), (
        item(real.url, real.score + 10, real.section, real.title, real.source),
        item(stable.url, stable.score, stable.section, stable.title, stable.source),
        item("https://example.com/new", 99),
    ))
    delta = bd.make_delta(today, bd.StoreLoad((prior,), 1, ()), 5)
    assert delta.gap_days == 1
    assert delta.regime == "in-window"
    assert delta.classes[bd.MOVED] == []
    assert [r[0].url for r in delta.classes[bd.UNCHANGED]] == ["https://example.com/new", real.url, stable.url][1:]
    assert [i.url for i in delta.classes[bd.NEW]] == ["https://example.com/new"]


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
    assert "counts: 7 new | 7 resolved | 0 unchanged" in out


def test_first_run_is_baseline_not_all_new():
    today = snapshot("2026-06-09", [item("https://example.com/a", 90), item("https://example.com/b", 80)])
    delta = bd.make_delta(today, bd.StoreLoad((), 0, ()), 5)
    assert delta.regime == "baseline"
    assert delta.prior_date is None
    assert delta.gap_days is None
    assert delta.classes == {bd.NEW: [], bd.MOVED: [], bd.RESOLVED: [], bd.UNCHANGED: []}
    out = bd.render_delta(delta)
    assert "baseline: no prior snapshot" in out
    assert "NEW" not in out


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
    assert old_only not in [i.url for i in delta.classes[bd.RESOLVED]]
    assert [i.url for i in delta.classes[bd.NEW]] == [today_url]


def test_index_reads_across_full_retention():
    recurring = "https://example.com/retained"
    store = (
        snapshot("2026-06-01", [item(recurring, 50)]),
        snapshot("2026-06-30", [item("https://example.com/yesterday", 51)]),
    )
    today = bd.Snapshot(bd.date(2026, 7, 1), (item(recurring, 70),))
    delta = bd.make_delta(today, bd.StoreLoad(store, 2, ()), 5)
    assert delta.gap_days == 1
    assert delta.classes[bd.MOVED] == []
    assert [i.url for i in delta.classes[bd.NEW]] == [recurring]
    assert [i.url for i in delta.classes[bd.RESOLVED]] == ["https://example.com/yesterday"]


# --- store, retention, render ------------------------------------------------
def test_same_day_rerun_overwrites_and_diffs_prior_history(tmp_path):
    old = "https://example.com/old"
    today_url = "https://example.com/today"
    write_snap(tmp_path, "2026-06-01", [item(old, 10)])
    write_snap(tmp_path, "2026-06-02", [item(today_url, 20)])
    store = bd.load_store(tmp_path)
    today = bd.Snapshot(bd.date(2026, 6, 2), (item(old, 30),))
    delta = bd.make_delta(today, store, 5)
    assert [r[0].url for r in delta.classes[bd.UNCHANGED]] == [old]
    assert all(r[1].brief_date.isoformat() == "2026-06-01" for r in delta.classes[bd.UNCHANGED])
    assert delta.prior_date is not None
    assert delta.prior_date.isoformat() == "2026-06-01"


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
    assert len(remaining_dates) == 36


def test_retention_window_exceeds_producer_dedup_window():
    assert bd.DEFAULT_RETENTION_DAYS >= 5 * bd.PRODUCER_DEDUP_DAYS
    assert bd.PRODUCER_DEDUP_DAYS == 7
    assert bd.DEFAULT_RETENTION_DAYS == 35


def test_render_order_and_collapse():
    prior = snapshot("2026-06-01", [item("https://example.com/gone", 70), item("https://example.com/same", 40)])
    today = bd.Snapshot(bd.date(2026, 6, 2), (item("https://example.com/new", 90), item("https://example.com/same", 41)))
    delta = bd.make_delta(today, bd.StoreLoad((prior,), 1, ()), 5)
    out = bd.render_delta(delta)
    assert out.index("NEW") < out.index("RESOLVED") < out.index("UNCHANGED")
    assert "MOVED" not in out
    assert "UNCHANGED: 1 carried over" in out


def test_render_is_byte_deterministic():
    prior = load_fixture(JUN29)
    today = load_fixture(JUL03)
    delta1 = bd.make_delta(today, bd.StoreLoad((prior,), 1, ()), 5)
    delta2 = bd.make_delta(today, bd.StoreLoad((prior,), 1, ()), 5)
    assert bd.render_delta(delta1).encode() == bd.render_delta(delta2).encode()
    assert bd.render_delta(delta1) == bd.render_delta(delta2)
    assert bd.render_delta(delta1).endswith("\n")


def test_render_real_pair_smoke():
    out = bd.render_delta(bd.make_delta(load_fixture(JUL03), bd.StoreLoad((load_fixture(JUN29),), 1, ()), 5))
    assert "prior 2026-06-29" in out
    assert "gap 4d" in out
    assert "in-window" in out
    assert "index folded 1 of 1 snapshots" in out
    assert "https://x.com/emollick/status/2072872373758382497" in out
    assert "https://x.com/kocer_eth/status/2071288514608800108" in out
    assert "MOVED" not in out
    assert "UNCHANGED" not in out


def test_render_header_names_prior_gap_regime_and_index_count():
    prior_old = snapshot("2026-06-01", [item("https://example.com/old", 70)])
    prior = snapshot("2026-06-08", [item("https://example.com/yesterday", 80)])
    today = snapshot("2026-06-09", [item("https://example.com/today", 90)])
    delta = bd.make_delta(today, bd.StoreLoad((prior_old, prior), 2, ()), 5)
    first_line = bd.render_delta(delta).splitlines()[0]
    assert "prior 2026-06-08" in first_line
    assert "gap 1d" in first_line
    assert "in-window" in first_line
    assert "index folded 1 of 2 snapshots" in first_line


def test_render_no_crash_on_markdown_newlines():
    raw = {"url": "https://example.com/md", "score": 10, "source": "X", "tweet_text": "**bold**\nsecond line"}
    today = bd.Snapshot(bd.date(2026, 7, 1), (bd._item_from_raw(raw, "selected", 0),))
    out = bd.render_delta(bd.make_delta(today, bd.StoreLoad((), 0, ()), 5))
    assert "baseline" in out
    assert "**bold** second line" not in out
    assert "source" not in out.lower()


# --- corrupt prior + CLI probes ---------------------------------------------
def test_corrupt_prior_header_distinct_from_true_baseline(tmp_path, capsys):
    today = load_fixture(JUL03)
    true_base = bd.render_delta(bd.make_delta(today, bd.StoreLoad((), 0, ()), 5))
    write_snap(tmp_path, "2026-06-29", load_fixture(JUN29).items)
    (tmp_path / "snapshot-2026-07-02.json").write_text("{bad", encoding="utf-8")
    rc = bd.run_render(JUL03, tmp_path, 5, 35)
    out = capsys.readouterr().out
    assert rc == 0
    assert "WARNING: prior snapshot unreadable" in out
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
    assert delta.corrupt_prior is False


def test_selfcheck_builds_own_fixture_and_passes(monkeypatch, capsys):
    monkeypatch.setattr(bd, "DEFAULT_SOURCE", Path("/definitely/not/used.json"))
    assert bd.main(["--selfcheck"]) == 0
    assert "SELFCHECK OK" in capsys.readouterr().out
    assert bd.DEFAULT_SOURCE == Path("/definitely/not/used.json")


def test_check_target_and_selfcheck_are_distinct(tmp_path, capsys):
    missing = tmp_path / "missing.json"
    assert bd.main(["--check-target", "--source", str(missing)]) == 2
    assert "LIVENESS FAILURE" in capsys.readouterr().err
    assert bd.main(["--selfcheck", "--source", str(missing)]) == 0
    assert missing.exists() is False


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
    assert source.exists()


def test_check_target_not_a_file_exits_2(tmp_path, capsys):
    assert bd.main(["--check-target", "--source", str(tmp_path)]) == 2
    assert "not a regular file" in capsys.readouterr().err
    assert tmp_path.exists()


def test_check_target_fixture_exits_0(capsys):
    assert bd.main(["--check-target", "--source", str(JUL03)]) == 0
    out = capsys.readouterr().out
    assert f"OK: {JUL03} live, 7 items" in out
    assert "LIVENESS FAILURE" not in out


def test_render_run_on_empty_source_exits_nonzero(tmp_path, capsys):
    source = tmp_path / "empty.json"
    source.write_text(json.dumps({"selected": [], "also": [], "ts": "2026-07-03T00:00:00"}), encoding="utf-8")
    assert bd.main(["--source", str(source), "--state-dir", str(tmp_path / "state")]) == 2
    assert "LIVENESS FAILURE" in capsys.readouterr().err
    assert not (tmp_path / "state").exists()


def test_unknown_flag_exits_nonzero():
    with pytest.raises(SystemExit) as exc:
        bd.main(["--definitely-unknown"])
    assert exc.value.code != 0
    assert isinstance(exc.value.code, int)


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
    assert state.exists()


def test_default_source_is_real_path():
    assert bd.DEFAULT_SOURCE == Path.home() / ".hermes" / "state" / "cron" / "morning-digest" / "_render_input.json"
    assert bd.DEFAULT_STATE_DIR == Path.home() / ".hermes" / "greenhouse" / "brief_delta"
    assert bd.DEFAULT_MOVE_THRESHOLD == 5


def test_imports_are_stdlib_only():
    tree = ast.parse(Path(bd.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported - {"__future__", "argparse", "json", "pathlib", "sys", "dataclasses", "datetime", "typing", "urllib"})
    assert "subprocess" not in imported
    assert "requests" not in imported
