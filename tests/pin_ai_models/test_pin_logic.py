from __future__ import annotations

import json
import os
import pathlib

from tools.pin_ai_models.pin_ai_models import RunnerSpec, main, run_apply, run_undo


class Proc:
    def __init__(self, code=0, stdout="ok", stderr=""):
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


def ok_preflight():
    return True, ""


def denied_preflight():
    return False, "sudo preflight failed before mutation; run `sudo -n tmutil version`"


def ok_tmutil(argv, *, use_sudo=False):
    return Proc(0, "[Excluded]" if "isexcluded" in argv else "ok")


def test_dryrun_no_mutation(tmp_path, capsys):
    source = tmp_path / "home/.ollama/models"
    root = tmp_path / "volume/models"
    ledger = tmp_path / "state.json"
    code = main(["--models-root", str(root), "--ledger", str(ledger), "--runner", f"ollama={source}=ollama"])
    out = capsys.readouterr().out
    assert code == 0
    assert "DRY-RUN plan" in out
    assert "No changes made" in out
    assert not source.exists()
    assert not root.exists()
    assert not ledger.exists()


def test_apply_idempotent(tmp_path):
    runner = RunnerSpec("ollama", str(tmp_path / "home/.ollama/models"), "ollama")
    root = tmp_path / "volume/models"
    ledger = tmp_path / "state.json"
    code1, out1 = run_apply([runner], root, ledger, require_mount=False, preflight=ok_preflight, tmutil=ok_tmutil)
    state1 = json.loads(ledger.read_text(encoding="utf-8"))
    code2, out2 = run_apply([runner], root, ledger, require_mount=False, preflight=ok_preflight, tmutil=ok_tmutil)
    state2 = json.loads(ledger.read_text(encoding="utf-8"))
    assert code1 == 0
    assert "created symlink" in out1
    assert code2 == 0
    assert "already pinned, no change" in out2
    assert state1 == state2
    assert pathlib.Path(os.readlink(pathlib.Path(runner.source_default))) == root / "ollama"


def test_undo_restores_real_dir_and_does_not_migrate_external_window_writes(tmp_path):
    source = tmp_path / "home/.cache/huggingface"
    source.mkdir(parents=True)
    (source / "preexisting.txt").write_text("internal", encoding="utf-8")
    runner = RunnerSpec("hf/mlx", str(source), "huggingface")
    root = tmp_path / "volume/models"
    ledger = tmp_path / "state.json"
    assert run_apply([runner], root, ledger, require_mount=False, preflight=ok_preflight, tmutil=ok_tmutil)[0] == 0
    target = root / "huggingface"
    (target / "new-external-model.bin").write_text("external", encoding="utf-8")
    code, out = run_undo(ledger, tmutil=ok_tmutil)
    assert code == 0
    assert "undone moved_and_linked" in out
    assert source.is_dir()
    assert (source / "preexisting.txt").read_text(encoding="utf-8") == "internal"
    assert not (source / "new-external-model.bin").exists()
    assert (target / "new-external-model.bin").read_text(encoding="utf-8") == "external"


def test_preflight_blocks_before_mutation(tmp_path):
    runner = RunnerSpec("ollama", str(tmp_path / "home/.ollama/models"), "ollama")
    root = tmp_path / "volume/models"
    ledger = tmp_path / "state.json"
    code, out = run_apply([runner], root, ledger, require_mount=False, preflight=denied_preflight, tmutil=ok_tmutil)
    assert code != 0
    assert "sudo -n tmutil version" in out
    assert not pathlib.Path(runner.source_default).exists()
    assert not ledger.exists()
