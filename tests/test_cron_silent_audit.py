"""Tests for cron_silent_audit. Stdlib + pytest only; collects offline."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

from tools.cron_silent_audit.audit import (
    DEFAULT_ALERT_TOKENS, OK_DELIVERS_REMOTE, OK_HAS_ALERT, SILENT_EMPTY_BODY,
    SILENT_NO_ALERT_PROMPT, SILENT_NO_ALERT_SCRIPT, SILENT_SCRIPT_MISSING,
    VERDICT_RANK, audit, build_parser, classify_job, default_registry, main,
    parse_registry, run_check_target, run_selfcheck,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE = "tools.cron_silent_audit.audit"


@pytest.fixture
def scripts_dir(tmp_path):
    sd = tmp_path / "scripts"
    sd.mkdir()
    (sd / "noalert.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (sd / "withalert.sh").write_text("#!/bin/sh\nnotify.py x\n", encoding="utf-8")
    return sd


@pytest.mark.parametrize("job,expected,flagged", [
    ({"name": "j", "enabled": True, "deliver": "local", "script": "gone.sh"}, SILENT_SCRIPT_MISSING, True),
    ({"name": "j", "enabled": True, "deliver": "local", "script": "noalert.sh"}, SILENT_NO_ALERT_SCRIPT, True),
    ({"name": "j", "enabled": True, "deliver": "local", "script": "withalert.sh"}, OK_HAS_ALERT, False),
    ({"name": "j", "enabled": True, "deliver": "none", "script": None, "prompt": "do work"}, SILENT_NO_ALERT_PROMPT, True),
    ({"name": "j", "enabled": True, "deliver": "", "script": None, "prompt": "on error post #alerts"}, OK_HAS_ALERT, False),
    ({"name": "j", "enabled": True, "deliver": None, "script": None, "prompt": "   "}, SILENT_EMPTY_BODY, True),
    ({"name": "j", "enabled": True, "deliver": "discord:9", "script": None, "prompt": "x"}, OK_DELIVERS_REMOTE, False),
])
def test_classify_verdicts(scripts_dir, job, expected, flagged):
    v = classify_job(job, scripts_dir)
    assert v.verdict == expected
    assert v.flagged is flagged


def test_disabled_returns_none(scripts_dir):
    assert classify_job({"name": "j", "enabled": False, "deliver": "local", "script": None}, scripts_dir) is None


def test_custom_tokens(tmp_path):
    sd = tmp_path / "s"
    sd.mkdir()
    (sd / "s.sh").write_text("send PAGER alert\n", encoding="utf-8")
    job = {"name": "j", "enabled": True, "deliver": "local", "script": "s.sh"}
    assert classify_job(job, sd).verdict == SILENT_NO_ALERT_SCRIPT
    assert classify_job(job, sd, tokens=("PAGER",)).verdict == OK_HAS_ALERT


def test_audit_filtering_and_order(scripts_dir):
    jobs = [
        {"name": "ok", "enabled": True, "deliver": "discord:1", "prompt": "x"},
        {"name": "p", "enabled": True, "deliver": "local", "script": None, "prompt": "x"},
        {"name": "m", "enabled": True, "deliver": "local", "script": "gone.sh"},
    ]
    flagged = audit(jobs, scripts_dir)
    assert [v.name for v in flagged] == ["m", "p"]
    assert [v.rank for v in flagged] == sorted(v.rank for v in flagged)
    assert {v.name for v in audit(jobs, scripts_dir, include_all=True)} == {"ok", "p", "m"}


def test_parse_registry_dict():
    assert parse_registry({"jobs": [{"name": "a"}], "updated_at": "t"}) == [{"name": "a"}]


@pytest.mark.parametrize("raw", [[{"name": "a"}], {"updated_at": "t"}, "nope", 5])
def test_parse_registry_bad_shapes_rejected(raw):
    with pytest.raises(ValueError):
        parse_registry(raw)


def test_selfcheck_passes():
    assert run_selfcheck() == 0
    assert main(["--selfcheck"]) == 0


def test_selfcheck_touches_no_real_source(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert main(["--selfcheck"]) == 0


def test_check_target_missing_file_fails(tmp_path):
    assert run_check_target(tmp_path / "nope.json") != 0


def test_check_target_bad_shape_fails(tmp_path):
    reg = tmp_path / "jobs.json"
    reg.write_text(json.dumps([{"name": "a"}]), encoding="utf-8")
    assert run_check_target(reg) != 0


def test_check_target_empty_jobs_fails(tmp_path):
    reg = tmp_path / "jobs.json"
    reg.write_text(json.dumps({"jobs": [], "updated_at": "t"}), encoding="utf-8")
    assert run_check_target(reg) != 0


def test_check_target_good_passes(tmp_path):
    reg = tmp_path / "jobs.json"
    reg.write_text(json.dumps({"jobs": [{"name": "a", "enabled": True}], "updated_at": "t"}), encoding="utf-8")
    assert run_check_target(reg) == 0


def test_garbage_flag_nonzero():
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--definitely-not-a-flag"])
    assert exc.value.code != 0


def test_selfcheck_subprocess_exit0():
    r = subprocess.run([sys.executable, "-m", MODULE, "--selfcheck"], cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_garbage_flag_subprocess_nonzero():
    r = subprocess.run([sys.executable, "-m", MODULE, "--bogus"], cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert r.returncode != 0


def test_default_tokens_are_real_signals():
    assert DEFAULT_ALERT_TOKENS == ("notify.py", "#alerts", "#logs")


def test_verdict_ranks_strict_order():
    ranks = [VERDICT_RANK[v] for v in (SILENT_SCRIPT_MISSING, SILENT_NO_ALERT_SCRIPT, SILENT_NO_ALERT_PROMPT, SILENT_EMPTY_BODY)]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)


def test_default_registry_resolves_under_hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert default_registry() == tmp_path / "cron" / "jobs.json"
