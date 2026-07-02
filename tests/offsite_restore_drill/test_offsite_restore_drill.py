"""Offline tests for tools.offsite_restore_drill.offsite_restore_drill."""
from __future__ import annotations

import ast
import json
import os
import pathlib
import stat
import tarfile

import pytest

from tools.offsite_restore_drill.offsite_restore_drill import (
    DEFAULT_DRILL_SCRIPT,
    DEFAULT_REMOTE,
    DrillError,
    RunConfig,
    TargetInfo,
    assert_extracted_tree,
    build_config,
    build_parser,
    check_target,
    discover_latest_target,
    main,
    make_fixture_tar,
    make_stub_drill,
    parse_lsd_dates,
    parse_lsf_tars,
    remote_join,
    run,
    safe_extract_tar,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "tools" / "offsite_restore_drill" / "offsite_restore_drill.py"


def write_fake_rclone(tmp_path: pathlib.Path) -> pathlib.Path:
    script = tmp_path / "rclone-fake.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, shutil, sys\n"
        "args = sys.argv[1:]\n"
        "if not args:\n"
        "    sys.exit(2)\n"
        "cmd = args[0]\n"
        "date = os.environ.get('FAKE_RCLONE_DATE', '2026-07-01')\n"
        "tar_name = os.environ.get('FAKE_RCLONE_TAR_NAME', f'hermes-fleet-encrypted-{date}.tar')\n"
        "if cmd == 'lsd':\n"
        "    mode = os.environ.get('FAKE_RCLONE_MODE', 'ok')\n"
        "    if mode == 'unreachable':\n"
        "        print('remote not found', file=sys.stderr)\n"
        "        sys.exit(7)\n"
        "    if mode == 'empty':\n"
        "        sys.exit(0)\n"
        "    print(f'          -1 2026-06-01 00:00:00        -1 2026-06-01')\n"
        "    print(f'          -1 {date} 00:00:00        -1 {date}')\n"
        "    sys.exit(0)\n"
        "if cmd == 'lsf':\n"
        "    mode = os.environ.get('FAKE_RCLONE_MODE', 'ok')\n"
        "    if mode == 'notar':\n"
        "        print('notes.txt')\n"
        "    else:\n"
        "        print(tar_name)\n"
        "    sys.exit(0)\n"
        "if cmd == 'size':\n"
        "    print(json.dumps({'bytes': int(os.environ.get('FAKE_RCLONE_BYTES', '2048'))}))\n"
        "    sys.exit(0)\n"
        "if cmd == 'copy':\n"
        "    source = pathlib.Path(os.environ['FAKE_RCLONE_SOURCE_TAR'])\n"
        "    dest = pathlib.Path(args[-1])\n"
        "    dest.mkdir(parents=True, exist_ok=True)\n"
        "    shutil.copyfile(source, dest / pathlib.PurePosixPath(args[1]).name)\n"
        "    sys.exit(0)\n"
        "print('unsupported fake rclone command: ' + cmd, file=sys.stderr)\n"
        "sys.exit(9)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def cfg(tmp_path: pathlib.Path, rclone: pathlib.Path | None = None, drill: pathlib.Path | None = None, min_bytes: int = 1) -> RunConfig:
    return RunConfig(
        remote=DEFAULT_REMOTE,
        rclone_bin=str(rclone) if rclone else None,
        agent="apollo",
        drill_script=drill or (tmp_path / "restore-drill.py"),
        scratch_root=tmp_path / "scratch",
        state_dir=tmp_path / "state",
        min_tar_bytes=min_bytes,
        timeout_seconds=30,
    )


def test_defaults_match_deployment(monkeypatch):
    parser = build_parser()
    args = parser.parse_args(["--check-target"])
    monkeypatch.delenv("FBRD_OFFSITE_REMOTE", raising=False)
    monkeypatch.delenv("FBRD_DRILL_SCRIPT", raising=False)
    resolved = build_config(args, environ={})
    assert resolved.remote == "fleet-offsite2:"
    assert str(resolved.drill_script).endswith("/.hermes/projects/fleet-backup-rehome/scripts/restore-drill.py")
    assert DEFAULT_REMOTE == "fleet-offsite2:"
    assert DEFAULT_DRILL_SCRIPT == "~/.hermes/projects/fleet-backup-rehome/scripts/restore-drill.py"


def test_remote_join_keeps_rclone_colon_shape():
    assert remote_join("fleet-offsite2:", "2026-07-01", "x.tar") == "fleet-offsite2:/2026-07-01/x.tar"


def test_latest_tar_discovery_parsers_are_deterministic():
    lsd = "          -1 2026-06-30 00:00:00        -1 2026-06-30\nnot-a-date\n          -1 2026-07-01 00:00:00        -1 2026-07-01\n"
    lsf = "notes.txt\nhermes-fleet-encrypted-2026-07-01.tar\nsubdir/\n"
    assert parse_lsd_dates(lsd) == ["2026-07-01", "2026-06-30"]
    assert parse_lsf_tars(lsf) == ["hermes-fleet-encrypted-2026-07-01.tar"]


def test_check_target_fails_with_no_remote(tmp_path, capsys):
    missing = tmp_path / "missing-rclone"
    rc = main(["--check-target", "--rclone-bin", str(missing)])
    captured = capsys.readouterr()
    assert rc != 0
    assert "LOUD" in captured.err
    assert "rclone not resolvable" in captured.err


def test_selfcheck_passes_with_no_remote(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    rc = main(["--selfcheck"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "offline fixture" in captured.out
    assert "real remote" in captured.out


def test_unknown_flag_exits_nonzero():
    with pytest.raises(SystemExit) as exc:
        main(["--definitely-unknown"])
    assert exc.value.code != 0


def test_check_target_fails_on_empty_listing(tmp_path, monkeypatch):
    rclone = write_fake_rclone(tmp_path)
    monkeypatch.setenv("FAKE_RCLONE_MODE", "empty")
    with pytest.raises(DrillError, match="no dated offsite backup"):
        check_target(cfg(tmp_path, rclone=rclone))


def test_check_target_fails_on_truncated_tar(tmp_path, monkeypatch):
    rclone = write_fake_rclone(tmp_path)
    monkeypatch.setenv("FAKE_RCLONE_BYTES", "10")
    with pytest.raises(DrillError, match="truncated/empty"):
        check_target(cfg(tmp_path, rclone=rclone, min_bytes=1024))


def test_check_target_passes_against_fake_nonempty_remote(tmp_path, monkeypatch):
    rclone = write_fake_rclone(tmp_path)
    monkeypatch.setenv("FAKE_RCLONE_BYTES", "2048")
    target = check_target(cfg(tmp_path, rclone=rclone, min_bytes=1024))
    assert target.remote_path == "fleet-offsite2:/2026-07-01/hermes-fleet-encrypted-2026-07-01.tar"
    assert target.size == 2048


def test_extract_rejects_path_escape(tmp_path):
    tar_path = tmp_path / "bad.tar"
    evil = tmp_path / "evil.txt"
    evil.write_text("bad", encoding="utf-8")
    with tarfile.open(tar_path, "w") as tf:
        tf.add(evil, arcname="../evil.txt")
    with pytest.raises(DrillError, match="escapes extraction root"):
        safe_extract_tar(tar_path, tmp_path / "extract")


def test_extract_root_is_under_scratch(tmp_path):
    tar_path = make_fixture_tar(tmp_path)
    scratch = tmp_path / "scratch"
    extract = scratch / "extract"
    safe_extract_tar(tar_path, extract)
    chosen = assert_extracted_tree(extract, "apollo")
    assert scratch.resolve() in extract.resolve().parents
    assert chosen.name == "2026-07-01"


def test_run_fails_when_extract_tree_malformed(tmp_path):
    bad_root = tmp_path / "badroot"
    bad_root.mkdir()
    tar_path = tmp_path / "hermes-fleet-encrypted-2026-07-01.tar"
    (bad_root / "README.txt").write_text("not backup layout", encoding="utf-8")
    with tarfile.open(tar_path, "w") as tf:
        tf.add(bad_root / "README.txt", arcname="README.txt")
    extract = tmp_path / "extract"
    safe_extract_tar(tar_path, extract)
    with pytest.raises(DrillError, match="malformed offsite tree"):
        assert_extracted_tree(extract, "apollo")


def test_run_writes_offsite_proof_with_stubbed_drill_and_fake_rclone(tmp_path, monkeypatch):
    source_tar = make_fixture_tar(tmp_path)
    rclone = write_fake_rclone(tmp_path)
    stub = make_stub_drill(tmp_path)
    monkeypatch.setenv("FAKE_RCLONE_SOURCE_TAR", str(source_tar))
    monkeypatch.setenv("FAKE_RCLONE_BYTES", str(source_tar.stat().st_size))
    result = run(cfg(tmp_path, rclone=rclone, drill=stub, min_bytes=1))
    proof = json.loads(result.proof_path.read_text(encoding="utf-8"))
    assert proof["source"] == "offsite2"
    assert proof["sha256_verified"] is True
    assert proof["agent"] == "apollo"
    assert proof["inner_drill_ok"]["stub"] is True
    assert result.heartbeat_path.exists()


def test_pull_copy_size_mismatch_is_loud(tmp_path, monkeypatch):
    source_tar = make_fixture_tar(tmp_path)
    rclone = write_fake_rclone(tmp_path)
    stub = make_stub_drill(tmp_path)
    monkeypatch.setenv("FAKE_RCLONE_SOURCE_TAR", str(source_tar))
    monkeypatch.setenv("FAKE_RCLONE_BYTES", str(source_tar.stat().st_size + 1))
    with pytest.raises(DrillError, match="size mismatch"):
        run(cfg(tmp_path, rclone=rclone, drill=stub, min_bytes=1))


def test_no_third_party_imports_in_tool():
    allowed_roots = {
        "argparse", "datetime", "hashlib", "json", "os", "pathlib", "re", "shutil",
        "subprocess", "sys", "tarfile", "tempfile", "dataclasses", "typing", "__future__",
    }
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            seen.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            seen.add(node.module.split(".")[0])
    assert seen <= allowed_roots


def test_no_live_schedule_artifact_installed_by_tests():
    launchd = REPO_ROOT / "launchd" / "ai.hermes.offsite-restore-drill.plist.proposed"
    if launchd.exists():
        text = launchd.read_text(encoding="utf-8")
        assert "RunAtLoad" in text
        assert "<false/>" in text


def test_proposed_plist_checks_target_before_run_and_has_disabled_sentinel():
    launchd = REPO_ROOT / "launchd" / "ai.hermes.offsite-restore-drill.plist.proposed"
    text = launchd.read_text(encoding="utf-8")
    assert "DISABLED" in text
    assert "then exit 0" in text
    assert text.index("--check-target") < text.index("--run")
