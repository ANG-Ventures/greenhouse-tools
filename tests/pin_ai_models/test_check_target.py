from __future__ import annotations

import json
import os
import pathlib

from tools.pin_ai_models.pin_ai_models import DEFAULT_RUNNERS, RunnerSpec, run_check_target

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tools/pin_ai_models/fixtures/ace_ai_shape/manifest.json"


class Proc:
    def __init__(self, stdout="[Excluded]"):
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


def excluded(argv, *, use_sudo=False):
    assert not use_sudo
    assert "addexclusion" not in argv
    assert "removeexclusion" not in argv
    return Proc("[Excluded] /target")


def not_excluded(argv, *, use_sudo=False):
    return Proc("[Not Excluded] /target")


def write_ledger(path, runner, source, target):
    path.write_text(json.dumps({"version": 1, "runs": [{
        "runner": runner,
        "action": "created_symlink",
        "source_default": str(source),
        "target": str(target),
        "symlink_created": True,
        "backup_moved": False,
        "prior_link_target": None,
        "tm_excluded": True,
        "ts": 1.0,
    }]}), encoding="utf-8")


def mounted(monkeypatch, volume):
    monkeypatch.setattr(os.path, "ismount", lambda p: pathlib.Path(p) == volume)


def test_check_target_ok_with_ledger_pinned_runner(tmp_path, monkeypatch):
    volume = tmp_path / "volume"
    root = volume / "models"
    target = root / "ollama"
    source = tmp_path / "home/.ollama/models"
    target.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    source.symlink_to(target, target_is_directory=True)
    ledger = tmp_path / "state.json"
    write_ledger(ledger, "ollama", source, target)
    mounted(monkeypatch, volume)
    code, out = run_check_target([RunnerSpec("ollama", str(source), "ollama"), RunnerSpec("lmstudio", str(tmp_path / "home/lm"), "lmstudio")], root, ledger, tmutil=excluded)
    assert code == 0
    assert "lmstudio not in ledger" in out


def test_unmounted_volume_is_loud_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "ismount", lambda p: False)
    code, out = run_check_target(DEFAULT_RUNNERS, tmp_path / "missing/models", tmp_path / "state.json", tmutil=excluded)
    assert code != 0
    assert "external models volume not mounted" in out


def test_ledger_pinned_runner_drift_pages(tmp_path, monkeypatch):
    volume = tmp_path / "volume"
    root = volume / "models"
    source = tmp_path / "home/.ollama/models"
    target = root / "ollama"
    root.mkdir(parents=True)
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    ledger = tmp_path / "state.json"
    write_ledger(ledger, "ollama", source, target)
    mounted(monkeypatch, volume)
    code, out = run_check_target([RunnerSpec("ollama", str(source), "ollama")], root, ledger, tmutil=excluded)
    assert code != 0
    assert "UNPINNED" in out


def test_ledger_target_not_excluded_pages(tmp_path, monkeypatch):
    volume = tmp_path / "volume"
    root = volume / "models"
    target = root / "ollama"
    source = tmp_path / "home/.ollama/models"
    target.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    source.symlink_to(target, target_is_directory=True)
    ledger = tmp_path / "state.json"
    write_ledger(ledger, "ollama", source, target)
    mounted(monkeypatch, volume)
    code, out = run_check_target([RunnerSpec("ollama", str(source), "ollama")], root, ledger, tmutil=not_excluded)
    assert code != 0
    assert "being backed up" in out


def test_empty_ledger_exits_zero_info(tmp_path, monkeypatch):
    volume = tmp_path / "volume"
    root = volume / "models"
    root.mkdir(parents=True)
    (tmp_path / "state.json").write_text('{"version": 1, "runs": []}', encoding="utf-8")
    mounted(monkeypatch, volume)
    code, out = run_check_target(DEFAULT_RUNNERS, root, tmp_path / "state.json", tmutil=excluded)
    assert code == 0
    assert "no runners pinned yet" in out


def test_check_target_is_unprivileged(tmp_path, monkeypatch):
    calls = []
    def recorder(argv, *, use_sudo=False):
        calls.append((list(argv), use_sudo))
        return Proc("[Excluded]")
    volume = tmp_path / "volume"
    root = volume / "models"
    target = root / "ollama"
    source = tmp_path / "home/.ollama/models"
    target.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    source.symlink_to(target, target_is_directory=True)
    ledger = tmp_path / "state.json"
    write_ledger(ledger, "ollama", source, target)
    mounted(monkeypatch, volume)
    assert run_check_target([RunnerSpec("ollama", str(source), "ollama")], root, ledger, tmutil=recorder)[0] == 0
    assert calls
    assert all(use_sudo is False for _argv, use_sudo in calls)
    assert all("addexclusion" not in argv and "removeexclusion" not in argv for argv, _ in calls)


def test_real_shape_fixture_present():
    assert FIXTURE.exists(), "real-shape fixture absent — run `capture_ace_ai_shape.sh` on ACE-AI and commit `fixtures/ace_ai_shape/manifest.json`; do NOT hand-invent it."
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["captured_by"].endswith("capture_ace_ai_shape.sh")
    assert data["models_root_basename"] == "Models SSD 4TB"
    assert "runners" in data and data["runners"]


def test_e2e_over_real_shape_fixture(tmp_path, monkeypatch):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    volume = tmp_path / "Models SSD 4TB"
    root = volume / "models"
    mounted(monkeypatch, volume)
    # Materialize one ledger-pinned runner and one default-map runner absent from ledger.
    source = tmp_path / "home/.ollama/models"
    target = root / "ollama"
    target.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    source.symlink_to(target, target_is_directory=True)
    ledger = tmp_path / "state.json"
    write_ledger(ledger, "ollama", source, target)
    runners = [RunnerSpec("ollama", str(source), "ollama"), RunnerSpec("lmstudio", str(tmp_path / "home/.cache/lm-studio/models"), "lmstudio")]
    ok, ok_out = run_check_target(runners, root, ledger, tmutil=excluded)
    assert ok == 0
    assert "lmstudio not in ledger" in ok_out
    source.unlink()
    source.mkdir()
    drift, drift_out = run_check_target(runners, root, ledger, tmutil=excluded)
    assert drift != 0
    assert "UNPINNED" in drift_out
    assert data["runners"]["lmstudio"]["kind"] in {"missing", "dir", "symlink", "file"}
