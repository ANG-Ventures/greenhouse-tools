from __future__ import annotations

import json
import os
import pathlib

from tools.pin_ai_models.pin_ai_models import RunnerSpec, run_apply, run_undo


class Proc:
    returncode = 0
    stdout = "[Excluded]"
    stderr = ""


def ok_preflight():
    return True, ""


def tmutil(argv, *, use_sudo=False):
    return Proc()


def apply_one(tmp_path, runner):
    return run_apply([runner], tmp_path / "volume/models", tmp_path / "state.json", require_mount=False, preflight=ok_preflight, tmutil=tmutil)


def test_missing_source_links_and_records(tmp_path):
    source = tmp_path / "home/missing"
    runner = RunnerSpec("missing", str(source), "missing")
    code, out = apply_one(tmp_path, runner)
    assert code == 0
    assert source.is_symlink()
    assert pathlib.Path(os.readlink(source)) == tmp_path / "volume/models/missing"
    records = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["runs"]
    assert records[0]["action"] == "created_symlink"


def test_already_our_symlink_is_noop(tmp_path):
    root = tmp_path / "volume/models"
    source = tmp_path / "home/models"
    target = root / "ollama"
    target.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    source.symlink_to(target, target_is_directory=True)
    runner = RunnerSpec("ollama", str(source), "ollama")
    code, out = run_apply([runner], root, tmp_path / "state.json", require_mount=False, preflight=ok_preflight, tmutil=tmutil)
    assert code == 0
    assert "already pinned" in out
    assert not (tmp_path / "state.json").exists()


def test_foreign_symlink_recorded_and_restored(tmp_path):
    source = tmp_path / "home/models"
    old = tmp_path / "old-target"
    old.mkdir()
    source.parent.mkdir(parents=True)
    source.symlink_to(old, target_is_directory=True)
    runner = RunnerSpec("ollama", str(source), "ollama")
    ledger = tmp_path / "state.json"
    code, out = run_apply([runner], tmp_path / "volume/models", ledger, require_mount=False, preflight=ok_preflight, tmutil=tmutil)
    assert code == 0
    rec = json.loads(ledger.read_text(encoding="utf-8"))["runs"][0]
    assert rec["action"] == "replaced_foreign_symlink"
    assert rec["prior_link_target"] == str(old)
    assert pathlib.Path(os.readlink(source)) == tmp_path / "volume/models/ollama"
    assert run_undo(ledger, tmutil=tmutil)[0] == 0
    assert source.is_symlink()
    assert os.readlink(source) == str(old)


def test_collision_aborts_zero_mutation(tmp_path):
    source = tmp_path / "home/models"
    target = tmp_path / "volume/models/ollama"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    (source / "source.bin").write_text("source", encoding="utf-8")
    (target / "target.bin").write_text("target", encoding="utf-8")
    runner = RunnerSpec("ollama", str(source), "ollama")
    code, out = apply_one(tmp_path, runner)
    assert code != 0
    assert "ABORT collision" in out
    assert source.is_dir()
    assert (source / "source.bin").exists()
    assert not (tmp_path / "state.json").exists()


def test_real_dir_moves_to_pinbak_and_links(tmp_path):
    source = tmp_path / "home/models"
    source.mkdir(parents=True)
    (source / "source.bin").write_text("source", encoding="utf-8")
    runner = RunnerSpec("ollama", str(source), "ollama")
    code, out = apply_one(tmp_path, runner)
    assert code == 0
    assert source.is_symlink()
    assert (tmp_path / "home/models.pin-bak/source.bin").read_text(encoding="utf-8") == "source"


def test_preexisting_pinbak_aborts(tmp_path):
    source = tmp_path / "home/models"
    source.mkdir(parents=True)
    (source / "x").write_text("x", encoding="utf-8")
    backup = tmp_path / "home/models.pin-bak"
    backup.mkdir(parents=True)
    (backup / "old").write_text("old", encoding="utf-8")
    runner = RunnerSpec("ollama", str(source), "ollama")
    code, out = apply_one(tmp_path, runner)
    assert code != 0
    assert "pre-existing backup" in out
    assert source.is_dir()
    assert (backup / "old").exists()
    assert not (backup / "models").exists()


def test_plain_file_aborts_zero_mutation(tmp_path):
    source = tmp_path / "home/models"
    source.parent.mkdir(parents=True)
    source.write_text("not a directory", encoding="utf-8")
    runner = RunnerSpec("ollama", str(source), "ollama")
    code, out = apply_one(tmp_path, runner)
    assert code != 0
    assert "unsupported source state" in out
    assert source.read_text(encoding="utf-8") == "not a directory"
    assert not (tmp_path / "state.json").exists()
