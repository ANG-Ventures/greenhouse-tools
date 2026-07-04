from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools.vault_ribbon import (
    DEFAULT_VAULT_ROOT,
    MalformedRAGResponse,
    VaultNote,
    annotate,
    build_index,
    check_vault,
    iter_chunks,
    parse_asof,
)
from tools.vault_ribbon.cli import main

ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ROOT / "tools" / "vault_ribbon"


def _write_note(path: Path, text: str = "note") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_index_excludes_dotdirs_and_counts_collisions(tmp_path: Path) -> None:
    _write_note(tmp_path / "Alpha.md")
    _write_note(tmp_path / "Nested" / "alpha.md")
    _write_note(tmp_path / ".obsidian" / "Alpha.md")
    _write_note(tmp_path / "Plain.txt")

    index = build_index(tmp_path)

    assert sorted(index) == ["alpha"]
    assert [note.path for note in index["alpha"]] == ["Alpha.md", "Nested/alpha.md"]


def test_no_match_emits_null() -> None:
    response = {"chunks": [{"title": "Computer Buying and Building", "text": "x"}]}

    out = annotate(response, {})

    vault = out["chunks"][0]["vault"]
    assert vault["vault_match"] is None
    assert vault["match_count"] == 0


def test_newer_null_without_asof() -> None:
    index = {"hardware": [VaultNote("Hardware.md", datetime(2026, 7, 3, tzinfo=timezone.utc))]}

    out = annotate({"chunks": [{"title": "Hardware"}]}, index)

    assert out["chunks"][0]["vault"]["vault_is_newer"] is None


def test_newer_true_false_with_asof() -> None:
    index = {
        "new": [VaultNote("New.md", datetime(2026, 7, 3, 5, tzinfo=timezone.utc))],
        "old": [VaultNote("Old.md", datetime(2026, 7, 3, 3, tzinfo=timezone.utc))],
    }
    response = {"chunks": [{"title": "New"}, {"title": "Old"}]}

    out = annotate(response, index, "2026-07-03T04:00:00+00:00")

    assert out["chunks"][0]["vault"]["vault_is_newer"] is True
    assert out["chunks"][1]["vault"]["vault_is_newer"] is False


def test_naive_asof_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        parse_asof("2026-07-03T04:00:00")


def test_annotate_preserves_fields_and_order() -> None:
    response = {
        "chunks": [
            {"title": "First", "text": "a", "source_type": "drive", "score": 0.1},
            {"title": "Second", "text": "b", "source_type": "obsidian", "score": 0.2},
        ],
        "sources": ["kept"],
    }

    out = annotate(response, {})

    assert out["sources"] == ["kept"]
    assert [chunk["title"] for chunk in out["chunks"]] == ["First", "Second"]
    assert out["chunks"][0]["text"] == "a"
    assert out["chunks"][1]["score"] == 0.2
    assert "vault" in out["chunks"][0]


def test_adapter_real_and_bare_and_malformed() -> None:
    chunk = {"title": "Hardware"}

    assert list(iter_chunks({"chunks": [chunk], "sources": []})) == [chunk]
    assert list(iter_chunks([chunk])) == [chunk]
    with pytest.raises(MalformedRAGResponse):
        list(iter_chunks({"not_chunks": []}))
    with pytest.raises(MalformedRAGResponse):
        list(iter_chunks({"chunks": ["not a chunk"]}))


def test_selfcheck_ignores_real_vault() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = main(["--selfcheck", "--vault-root", "/does/not/exist"], stdout=stdout, stderr=stderr)

    assert code == 0
    assert stdout.getvalue().strip() == "SELFCHECK OK"
    assert stderr.getvalue() == ""


def test_check_vault_missing_exits_nonzero() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = main(["--check-vault", "--vault-root", "/does/not/exist"], stdout=stdout, stderr=stderr)

    assert code == 2
    assert stdout.getvalue() == ""
    assert "VAULT LIVENESS FAILED" in stderr.getvalue()


def test_check_vault_empty_exits_nonzero(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = main(["--check-vault", "--vault-root", str(tmp_path)], stdout=stdout, stderr=stderr)

    assert code == 2
    assert "VAULT LIVENESS FAILED" in stderr.getvalue()


def test_check_vault_success_counts_notes(tmp_path: Path) -> None:
    _write_note(tmp_path / "One.md")
    _write_note(tmp_path / "Two.md")

    assert check_vault(tmp_path) == 2


def test_unknown_flag_exits_nonzero() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "tools.vault_ribbon", "--garbage"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode != 0
    assert "usage:" in proc.stderr


def test_annotate_cli_outputs_json_and_stats(tmp_path: Path) -> None:
    _write_note(tmp_path / "Hardware.md")
    stdin = io.StringIO(json.dumps({"chunks": [{"title": "Hardware"}, {"title": "Absent"}]}))
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = main(["--annotate", "--vault-root", str(tmp_path), "--stats"], stdin=stdin, stdout=stdout, stderr=stderr)

    assert code == 0
    payload = json.loads(stdout.getvalue())
    stats = json.loads(stderr.getvalue())
    assert payload["chunks"][0]["vault"]["vault_match"] == "Hardware.md"
    assert payload["chunks"][1]["vault"]["vault_match"] is None
    assert stats["chunks_annotated"] == 2
    assert stats["matched"] == 1
    assert stats["notes_indexed"] == 1


def test_no_writes_outside_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_note(vault / "Hardware.md")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    before = set(cwd.iterdir())
    monkeypatch.chdir(cwd)
    stdin = io.StringIO(json.dumps({"chunks": [{"title": "Hardware"}]}))

    code = main(["--annotate", "--vault-root", str(vault)], stdin=stdin, stdout=io.StringIO(), stderr=io.StringIO())

    assert code == 0
    assert set(cwd.iterdir()) == before


def test_collision_reports_count() -> None:
    index = {
        "skill": [
            VaultNote("A/SKILL.md", datetime(2026, 1, 1, tzinfo=timezone.utc)),
            VaultNote("B/SKILL.md", datetime(2026, 1, 1, tzinfo=timezone.utc)),
            VaultNote("C/SKILL.md", datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ]
    }

    out = annotate({"chunks": [{"title": "SKILL"}]}, index)

    vault = out["chunks"][0]["vault"]
    assert vault["match_count"] == 3
    assert vault["vault_match"] == "A/SKILL.md"


def test_annotate_real_sample(tmp_path: Path) -> None:
    fixture = ROOT / "tests" / "fixtures" / "rag_response_real.json"
    response = json.loads(fixture.read_text(encoding="utf-8"))
    _write_note(tmp_path / "Hardware.md")
    index = build_index(tmp_path)

    out = annotate(response, index)
    by_title = {chunk["title"]: chunk["vault"] for chunk in out["chunks"]}

    assert by_title["Computer Buying and Building"]["vault_match"] is None
    assert by_title["Computer Buying and Building"]["match_count"] == 0
    assert by_title["Hardware"]["vault_match"] == "Hardware.md"
    assert by_title["Hardware"]["match_count"] == 1


def test_default_vault_root_is_real_path_not_fixture() -> None:
    assert DEFAULT_VAULT_ROOT == Path("~/Obsidian/Ace Place").expanduser()
    assert "fixture" not in str(DEFAULT_VAULT_ROOT).casefold()
    if DEFAULT_VAULT_ROOT.is_dir():
        assert sum(len(notes) for notes in build_index(DEFAULT_VAULT_ROOT).values()) >= 1000
    else:
        pytest.skip(f"real deployment vault is not mounted in this isolated test environment: {DEFAULT_VAULT_ROOT}")


def test_no_third_party_imports() -> None:
    allowed = set(sys.stdlib_module_names) | {"tools"}
    for path in MODULE_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                roots = {node.module.split(".")[0]} if node.module else set()
            else:
                continue
            assert roots <= allowed, f"{path.name} imports non-stdlib modules: {sorted(roots - allowed)}"
