from __future__ import annotations

from tools.pin_ai_models.pin_ai_models import RunnerSpec, run_apply, run_undo


class Proc:
    returncode = 0
    stdout = "[Excluded]"
    stderr = ""


def ok_preflight():
    return True, ""


def test_addexclusion_and_removeexclusion_use_fixed_path_p_flag(tmp_path):
    calls = []
    def tmutil(argv, *, use_sudo=False):
        calls.append((list(argv), use_sudo))
        return Proc()
    runner = RunnerSpec("ollama", str(tmp_path / "home/.ollama/models"), "ollama")
    ledger = tmp_path / "state.json"
    root = tmp_path / "volume/models"
    assert run_apply([runner], root, ledger, require_mount=False, preflight=ok_preflight, tmutil=tmutil)[0] == 0
    assert run_undo(ledger, tmutil=tmutil)[0] == 0
    assert (["tmutil", "addexclusion", "-p", str(root / "ollama")], True) in calls
    assert (["tmutil", "removeexclusion", "-p", str(root / "ollama")], True) in calls


def test_mutation_missing_p_flag_would_fail(tmp_path):
    calls = []
    def tmutil(argv, *, use_sudo=False):
        calls.append(list(argv))
        return Proc()
    runner = RunnerSpec("ollama", str(tmp_path / "home/.ollama/models"), "ollama")
    assert run_apply([runner], tmp_path / "volume/models", tmp_path / "state.json", require_mount=False, preflight=ok_preflight, tmutil=tmutil)[0] == 0
    add_calls = [c for c in calls if c[:2] == ["tmutil", "addexclusion"]]
    assert add_calls
    assert all(c[2] == "-p" for c in add_calls)
