"""Tests for repo_test_cmd_probe. Stdlib + pytest only; collects offline."""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

from tools.repo_test_cmd_probe import (
    LAUNCHD_STDERR,
    detect_test_suite,
    documented_test_command,
    format_result,
    is_documented_test_command_line,
    is_prose_structured_command,
    main,
    scan,
)

MODULE = "tools.repo_test_cmd_probe"


def write(path: pathlib.Path, text: str = "") -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def git_repo(root: pathlib.Path, name: str) -> pathlib.Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    return repo


def add_pytest_file(repo: pathlib.Path, rel: str = "tests/test_sample.py") -> None:
    write(repo / rel, "def test_ok():\n    assert True\n")


# --------------------------------------------------------------------------- #
# CB-1: prose guard applies regardless of fence context without false-HITing
# real commands that contain trailing punctuation/path args.
# --------------------------------------------------------------------------- #
def test_fenced_pytest_dot_is_documented(tmp_path):
    repo = git_repo(tmp_path, "documented-dot")
    add_pytest_file(repo)
    readme = write(repo / "README.md", "# x\n\n```sh\npytest .\n```\n")

    assert is_documented_test_command_line("pytest .") is True
    assert is_prose_structured_command("pytest .") is False
    assert documented_test_command(readme) == "pytest ."
    result = scan(tmp_path)
    assert result.repos_with_tests == 1
    assert result.documented == 1
    assert result.findings == ()


def test_fenced_pytest_q_is_documented(tmp_path):
    repo = git_repo(tmp_path, "documented-q")
    add_pytest_file(repo)
    readme = write(repo / "README.md", "# x\n\n```sh\npytest -q\n```\n")

    assert is_documented_test_command_line("pytest -q") is True
    assert is_prose_structured_command("pytest -q") is False
    assert documented_test_command(readme) == "pytest -q"
    assert scan(tmp_path).findings == ()


def test_fenced_pytest_is_our_runner_is_prose_hit(tmp_path):
    repo = git_repo(tmp_path, "prose-fence")
    add_pytest_file(repo)
    readme = write(repo / "README.md", "# x\n\n```\npytest is our runner\n```\n")

    assert is_prose_structured_command("pytest is our runner") is True
    assert is_documented_test_command_line("pytest is our runner") is False
    assert documented_test_command(readme) is None
    result = scan(tmp_path)
    assert len(result.findings) == 1
    assert pathlib.Path(result.findings[0].repo).name == "prose-fence"
    assert result.findings[0].suggested_command == "pytest -q"


def test_interior_sentence_terminal_is_prose_but_trailing_dot_alone_is_not():
    assert is_prose_structured_command("pytest. run this next") is True
    assert is_prose_structured_command("pytest .") is False
    assert is_documented_test_command_line("pytest tests/ -v") is True


# --------------------------------------------------------------------------- #
# Repo/test detection and output.
# --------------------------------------------------------------------------- #
def test_basename_pattern_matches_in_subdir(tmp_path):
    repo = git_repo(tmp_path, "nested")
    write(repo / "pkg" / "unit" / "test_widget.py", "def test_widget():\n    assert True\n")

    reason = detect_test_suite(repo)
    assert reason == "python test file: pkg/unit/test_widget.py"


def test_scan_flags_repo_with_tests_and_missing_readme_command(tmp_path):
    repo = git_repo(tmp_path, "missing-doc")
    add_pytest_file(repo)
    write(repo / "README.md", "# missing-doc\n\nTests exist but no command is listed.\n")

    result = scan(tmp_path)
    out = format_result(result)

    assert result.repos_seen == 1
    assert result.repos_with_tests == 1
    assert result.documented == 0
    assert len(result.findings) == 1
    assert "HIT" in out
    assert "suggested_command: pytest -q" in out
    assert "draft_patch:" in out
    assert "+## Test" in out
    assert "+pytest -q" in out


def test_limit_bounds_repos_scanned(tmp_path):
    first = git_repo(tmp_path, "a-first")
    second = git_repo(tmp_path, "b-second")
    add_pytest_file(first)
    add_pytest_file(second)
    write(first / "README.md", "# first\n")
    write(second / "README.md", "# second\n")

    result = scan(tmp_path, limit=1)

    assert result.repos_seen == 1
    assert len(result.findings) == 1
    assert pathlib.Path(result.findings[0].repo).name == "a-first"


# --------------------------------------------------------------------------- #
# CLI health/liveness contract.
# --------------------------------------------------------------------------- #
def test_selfcheck_cli_exits_zero_offline():
    proc = subprocess.run(
        [sys.executable, "-m", MODULE, "--selfcheck"],
        cwd=pathlib.Path(__file__).resolve().parent.parent,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0
    assert "SELFCHECK PASS" in proc.stdout
    assert proc.stderr == ""


def test_unknown_flag_exits_nonzero_real_argparse_dispatch():
    proc = subprocess.run(
        [sys.executable, "-m", MODULE, "--garbage-unknown-flag"],
        cwd=pathlib.Path(__file__).resolve().parent.parent,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode != 0
    assert "unrecognized arguments" in proc.stderr


def test_check_target_loud_nonzero_on_empty_target(tmp_path, capsys):
    rc = main(["--check-target", "--target", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc != 0
    assert "LIVENESS FAILURE" in captured.err
    assert "no git repos found" in captured.err


def test_check_target_passes_when_repo_exists(tmp_path, capsys):
    git_repo(tmp_path, "one")

    rc = main(["--check-target", "--target", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc == 0
    assert "LIVENESS PASS" in captured.out
    assert captured.err == ""


def test_probe_mentions_standard_error_path(capsys):
    rc = main(["--probe"])
    captured = capsys.readouterr()

    assert rc == 0
    assert LAUNCHD_STDERR in captured.out


def test_invalid_limit_rejected():
    with pytest.raises(SystemExit) as exc:
        main(["--limit", "0"])
    assert exc.value.code == 2
