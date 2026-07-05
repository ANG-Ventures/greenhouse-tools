from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_nightly_wrapper_uses_check_target_without_sudo_and_notifies():
    text = (REPO_ROOT / "tools/pin_ai_models/nightly_check.sh").read_text(encoding="utf-8")
    assert "--check-target" in text
    assert "--selfcheck" not in text
    assert "sudo" not in text
    assert "notify" in text
