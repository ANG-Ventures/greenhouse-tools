"""Offline tests for tools.offsite_restore_drill.offsite_restore_drill."""
from __future__ import annotations

import ast
import json
import pathlib
import stat
import subprocess

import pytest

import tools.offsite_restore_drill.offsite_restore_drill as od
from tools.offsite_restore_drill.offsite_restore_drill import (
    DEFAULT_REMOTE,
    DrillError,
    RunConfig,
    TargetInfo,
    build_config,
    build_parser,
    check_target,
    discover_latest_target,
    main,
    parse_lsd_dates,
    parse_lsf_tars,
    parse_rclone_size,
    remote_join,
    resolve_rclone,
    run,
    run_with_target,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "tools" / "offsite_restore_drill" / "offsite_restore_drill.py"


class FakeRclone:
    def __init__(self, *, dates: tuple[str, ...] = ("2026-06-01", "2026-07-01"),
                 tar_dates: tuple[str, ...] = ("2026-07-01",), size: int = 2048,
                 fail_lsd: bool = False) -> None:
        self.dates = dates
        self.tar_dates = set(tar_dates)
        self.size = size
        self.fail_lsd = fail_lsd
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout, env=None):
        self.calls.append(list(argv))
        cmd = argv[1]
        if cmd == "lsd":
            if self.fail_lsd:
                return subprocess.CompletedProcess(argv, 7, "", "remote not found")
            out = "".join(f"          -1 {d} 00:00:00        -1 {d}\n" for d in self.dates)
            return subprocess.CompletedProcess(argv, 0, out, "")
        if cmd == "lsf":
            remote = argv[2]
            date = next((d for d in self.dates if remote.endswith(d) or f"/{d}" in remote), self.dates[-1])
            if date in self.tar_dates:
                out = f"hermes-fleet-encrypted-{date}.tar\n"
            else:
                out = "notes.txt\n"
            return subprocess.CompletedProcess(argv, 0, out, "")
        if cmd == "size":
            return subprocess.CompletedProcess(argv, 0, json.dumps({"bytes": self.size}), "")
        return subprocess.CompletedProcess(argv, 9, "", "unsupported fake rclone command")


def cfg(tmp_path: pathlib.Path, rclone: pathlib.Path | None = None, min_bytes: int = 1) -> RunConfig:
    return RunConfig(
        remote=DEFAULT_REMOTE,
        rclone_bin=str(rclone) if rclone else None,
        agent="apollo",
        state_dir=tmp_path / "state",
        min_tar_bytes=min_bytes,
        timeout_seconds=30,
    )


def executable(path: pathlib.Path) -> pathlib.Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_defaults_match_deployment(monkeypatch):
    parser = build_parser()
    args = parser.parse_args(["--check-target"])
    monkeypatch.delenv("FBRD_OFFSITE_REMOTE", raising=False)
    resolved = build_config(args, environ={})
    assert resolved.remote == "fleet-offsite2:"
    assert str(resolved.state_dir).endswith("/.hermes/state/offsite-restore-drill")
    assert DEFAULT_REMOTE == "fleet-offsite2:"
    assert resolved.agent == "apollo"


def test_remote_join_keeps_rclone_colon_shape():
    assert remote_join("fleet-offsite2:", "2026-07-01", "x.tar") == "fleet-offsite2:/2026-07-01/x.tar"


def test_resolve_rclone_accepts_binary_name_from_path(tmp_path):
    rclone = executable(tmp_path / "rclone")
    assert resolve_rclone("rclone", environ={"PATH": str(tmp_path)}) == str(rclone.resolve())


def test_latest_tar_discovery_parsers_are_deterministic():
    lsd = "          -1 2026-06-30 00:00:00        -1 2026-06-30\nnot-a-date\n          -1 2026-07-01 00:00:00        -1 2026-07-01\n"
    lsf = "notes.txt\nhermes-fleet-encrypted-2026-07-01.tar\nsubdir/\n"
    assert parse_lsd_dates(lsd) == ["2026-07-01", "2026-06-30"]
    assert parse_lsf_tars(lsf) == ["hermes-fleet-encrypted-2026-07-01.tar"]


def test_size_parser_accepts_json_and_text():
    assert parse_rclone_size('{"bytes": 2048}') == 2048
    assert parse_rclone_size("Total size: 4096 Bytes") == 4096


def test_size_parser_rejects_unparseable_output():
    with pytest.raises(DrillError, match="could not parse"):
        parse_rclone_size("not size output")


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
    assert "offline target fixture" in captured.out
    assert "real remote" in captured.out


def test_unknown_flag_exits_nonzero():
    with pytest.raises(SystemExit) as exc:
        main(["--definitely-unknown"])
    assert exc.value.code != 0


def test_discovery_propagates_rclone_lsd_failure(monkeypatch):
    fake = FakeRclone(fail_lsd=True)
    monkeypatch.setattr(od, "run_command", fake)
    with pytest.raises(DrillError, match="rclone lsd"):
        discover_latest_target("/bin/rclone", DEFAULT_REMOTE, 1, 30)
    assert fake.calls[0][1] == "lsd"


def test_check_target_fails_on_empty_listing(tmp_path, monkeypatch):
    rclone = executable(tmp_path / "rclone")
    fake = FakeRclone(dates=())
    monkeypatch.setattr(od, "run_command", fake)
    with pytest.raises(DrillError, match="no dated offsite backup"):
        check_target(cfg(tmp_path, rclone=rclone))
    assert fake.calls[0][2] == DEFAULT_REMOTE


def test_check_target_fails_on_truncated_tar(tmp_path, monkeypatch):
    rclone = executable(tmp_path / "rclone")
    monkeypatch.setattr(od, "run_command", FakeRclone(size=10))
    with pytest.raises(DrillError, match="truncated/empty"):
        check_target(cfg(tmp_path, rclone=rclone, min_bytes=1024))


def test_latest_tar_discovery_skips_newest_date_without_tar(tmp_path, monkeypatch):
    rclone = executable(tmp_path / "rclone")
    fake = FakeRclone(dates=("2026-06-30", "2026-07-01", "2026-07-02"), tar_dates=("2026-07-01",))
    monkeypatch.setattr(od, "run_command", fake)
    target = check_target(cfg(tmp_path, rclone=rclone, min_bytes=1))
    assert target.date == "2026-07-01"
    assert target.remote_path == "fleet-offsite2:/2026-07-01/hermes-fleet-encrypted-2026-07-01.tar"
    assert [call[1] for call in fake.calls].count("lsf") == 2


def test_check_target_fails_when_no_dated_directory_has_a_tar(tmp_path, monkeypatch):
    rclone = executable(tmp_path / "rclone")
    monkeypatch.setattr(od, "run_command", FakeRclone(dates=("2026-07-01", "2026-07-02"), tar_dates=()))
    with pytest.raises(DrillError, match="no offsite tar found"):
        check_target(cfg(tmp_path, rclone=rclone, min_bytes=1))


def test_check_target_passes_against_fake_nonempty_remote(tmp_path, monkeypatch):
    rclone = executable(tmp_path / "rclone")
    monkeypatch.setattr(od, "run_command", FakeRclone(size=2048))
    target = check_target(cfg(tmp_path, rclone=rclone, min_bytes=1024))
    assert target.remote_path == "fleet-offsite2:/2026-07-01/hermes-fleet-encrypted-2026-07-01.tar"
    assert target.size == 2048


def test_run_writes_offsite_proof_with_stubbed_target(tmp_path, monkeypatch):
    rclone = executable(tmp_path / "rclone")
    monkeypatch.setattr(od, "run_command", FakeRclone(size=2048))
    result = run(cfg(tmp_path, rclone=rclone, min_bytes=1))
    proof = json.loads(result.proof_path.read_text(encoding="utf-8"))
    assert proof["source"] == "offsite2"
    assert proof["target_verified"] is True
    assert proof["agent"] == "apollo"
    assert proof["size"] == 2048
    assert result.heartbeat_path.exists()


def test_run_with_target_path_writes_proof_without_rclone(tmp_path):
    local_cfg = cfg(tmp_path, rclone=None, min_bytes=1)
    target = TargetInfo(
        remote="selfcheck-local-fixture:",
        date="2026-07-01",
        name="hermes-fleet-encrypted-2026-07-01.tar",
        remote_path="selfcheck-local-fixture:/2026-07-01/hermes-fleet-encrypted-2026-07-01.tar",
        size=2048,
    )
    result = run_with_target(local_cfg, target)
    proof = json.loads(result.proof_path.read_text(encoding="utf-8"))
    assert proof["source"] == "offsite2"
    assert proof["target_verified"] is True
    assert result.heartbeat_path.exists()


def test_run_with_target_small_target_is_loud(tmp_path):
    local_cfg = cfg(tmp_path, rclone=None, min_bytes=1024)
    target = TargetInfo("remote:", "2026-07-01", "hermes-fleet-encrypted-2026-07-01.tar", "remote:/2026-07-01/hermes-fleet-encrypted-2026-07-01.tar", 10)
    with pytest.raises(DrillError, match="truncated/empty"):
        run_with_target(local_cfg, target)


def test_main_run_writes_proof_path(tmp_path, monkeypatch, capsys):
    rclone = executable(tmp_path / "rclone")
    monkeypatch.setattr(od, "run_command", FakeRclone(size=2048))
    rc = main(["--run", "--rclone-bin", str(rclone), "--state-dir", str(tmp_path / "state"), "--min-tar-bytes", "1"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "offsite target freshness proof written" in captured.out
    assert (tmp_path / "state" / "offsite-drill.ok").exists()


def test_no_third_party_imports_in_tool():
    allowed_roots = {
        "argparse", "datetime", "json", "os", "pathlib", "re", "shutil",
        "subprocess", "sys", "tempfile", "dataclasses", "typing", "__future__",
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
