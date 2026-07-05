from __future__ import annotations

import pytest

from tools.pin_ai_models.pin_ai_models import main, run_selfcheck


def test_selfcheck_offline_fixture_passes():
    code, out = run_selfcheck()
    assert code == 0
    assert "SELF CHECK PASS" in out


def test_main_selfcheck_exits_zero(capsys):
    assert main(["--selfcheck"]) == 0
    assert "SELF CHECK PASS" in capsys.readouterr().out


def test_unknown_flag_exits_nonzero():
    with pytest.raises(SystemExit) as exc:
        main(["--garbage"])
    assert exc.value.code != 0
