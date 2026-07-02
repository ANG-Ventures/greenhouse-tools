#!/usr/bin/env python3
"""offsite-restore-drill: pull an offsite backup tar into scratch and reuse restore-drill.py.

The tool is intentionally small: resolve rclone, prove the real offsite target is
reachable/non-empty, copy the newest dated tarball into a throwaway scratch root,
extract it with a tar path-escape guard, assert it has the local backup-root shape,
then invoke the existing restore-drill.py unchanged with FBRD_BACKUP_ROOT pointed
at the extraction root. It is off by default and schedules nothing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_REMOTE = "fleet-offsite2:"
DEFAULT_AGENT = "apollo"
DEFAULT_DRILL_SCRIPT = "~/.hermes/projects/fleet-backup-rehome/scripts/restore-drill.py"
DEFAULT_STATE_DIR = "~/.hermes/state/offsite-restore-drill"
DEFAULT_MIN_TAR_BYTES = 1024 * 1024
DEADMAN_RELATIVE = pathlib.Path("backup/last-success-offsite-restore-drill")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TAR_RE = re.compile(r"^hermes-fleet-encrypted-\d{4}-\d{2}-\d{2}\.tar$")


class DrillError(RuntimeError):
    """Expected operational failure that should be loud and non-zero."""


@dataclass(frozen=True)
class TargetInfo:
    remote: str
    date: str
    name: str
    remote_path: str
    size: int


@dataclass(frozen=True)
class RunConfig:
    remote: str
    rclone_bin: Optional[str]
    agent: str
    drill_script: pathlib.Path
    scratch_root: pathlib.Path
    state_dir: pathlib.Path
    min_tar_bytes: int
    timeout_seconds: int


@dataclass(frozen=True)
class RunResult:
    proof_path: pathlib.Path
    heartbeat_path: pathlib.Path
    tar_path: pathlib.Path
    extract_root: pathlib.Path


def expand_path(value: str) -> pathlib.Path:
    return pathlib.Path(value).expanduser().resolve()


def remote_join(remote: str, *parts: str) -> str:
    base = remote.rstrip("/")
    clean = "/".join(p.strip("/") for p in parts if p.strip("/"))
    if not clean:
        return base
    return base + "/" + clean


def resolve_rclone(explicit: Optional[str], environ: Optional[Dict[str, str]] = None) -> Optional[str]:
    env = environ if environ is not None else os.environ
    candidates = [explicit, env.get("RCLONE_BIN"), shutil.which("rclone")]
    for candidate in candidates:
        if not candidate:
            continue
        path = pathlib.Path(candidate).expanduser()
        if path.exists() and os.access(path, os.X_OK):
            return str(path.resolve())
    return None


def run_command(argv: Sequence[str], timeout: int, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
        check=False,
    )


def require_ok(proc: subprocess.CompletedProcess[str], action: str) -> str:
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise DrillError(f"{action} failed rc={proc.returncode}: {detail}")
    return proc.stdout


def parse_lsd_dates(output: str) -> List[str]:
    dates: List[str] = []
    for raw in output.splitlines():
        if not raw.strip():
            continue
        name = raw.split()[-1]
        if DATE_RE.fullmatch(name):
            dates.append(name)
    return sorted(set(dates), reverse=True)


def parse_lsf_tars(output: str) -> List[str]:
    names = []
    for raw in output.splitlines():
        name = raw.strip().rstrip("/")
        if "/" in name:
            name = name.rsplit("/", 1)[-1]
        if TAR_RE.fullmatch(name):
            names.append(name)
    return sorted(set(names))


def parse_rclone_size(output: str) -> int:
    text = output.strip()
    if not text:
        raise DrillError("rclone size returned no output")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"Total size:\s*(\d+)\s+Bytes", text)
        if not match:
            raise DrillError(f"could not parse rclone size output: {text}")
        return int(match.group(1))
    if isinstance(data, dict) and isinstance(data.get("bytes"), int):
        return int(data["bytes"])
    raise DrillError(f"could not parse rclone size JSON: {text}")


def discover_latest_target(rclone_bin: str, remote: str, min_tar_bytes: int, timeout: int) -> TargetInfo:
    lsd = run_command([rclone_bin, "lsd", remote], timeout)
    dates = parse_lsd_dates(require_ok(lsd, f"rclone lsd {remote}"))
    if not dates:
        raise DrillError(f"LOUD: no dated offsite backup directories found on {remote}")

    date = dates[0]
    dir_remote = remote_join(remote, date)
    lsf = run_command([rclone_bin, "lsf", dir_remote, "--files-only"], timeout)
    tars = parse_lsf_tars(require_ok(lsf, f"rclone lsf {dir_remote}"))
    if not tars:
        raise DrillError(
            f"LOUD: newest dated offsite backup directory has no "
            f"hermes-fleet-encrypted-YYYY-MM-DD.tar: {dir_remote}"
        )
    name = tars[-1]
    remote_path = remote_join(remote, date, name)
    size_proc = run_command([rclone_bin, "size", remote_path, "--json"], timeout)
    size = parse_rclone_size(require_ok(size_proc, f"rclone size {remote_path}"))
    if size < min_tar_bytes:
        raise DrillError(
            f"LOUD: latest offsite tar is truncated/empty: {remote_path} has {size} bytes "
            f"(< {min_tar_bytes})"
        )
    return TargetInfo(remote=remote, date=date, name=name, remote_path=remote_path, size=size)


def check_target(cfg: RunConfig) -> TargetInfo:
    rclone = resolve_rclone(cfg.rclone_bin)
    if not rclone:
        raise DrillError("LOUD: rclone not resolvable; set --rclone-bin or RCLONE_BIN before using fleet-offsite2:")
    return discover_latest_target(rclone, cfg.remote, cfg.min_tar_bytes, cfg.timeout_seconds)


def pull_latest_offsite(cfg: RunConfig, target: TargetInfo, pull_dir: pathlib.Path) -> pathlib.Path:
    rclone = resolve_rclone(cfg.rclone_bin)
    if not rclone:
        raise DrillError("LOUD: rclone not resolvable before pull")
    pull_dir.mkdir(parents=True, exist_ok=True)
    proc = run_command([rclone, "copy", target.remote_path, str(pull_dir)], cfg.timeout_seconds)
    require_ok(proc, f"rclone copy {target.remote_path}")
    tar_path = pull_dir / target.name
    if not tar_path.exists() or not tar_path.is_file():
        raise DrillError(f"LOUD: rclone copy produced no local tar at {tar_path}")
    size = tar_path.stat().st_size
    if size != target.size:
        raise DrillError(f"LOUD: pulled tar size mismatch for {tar_path}: got {size}, expected {target.size}")
    return tar_path


def assert_inside(root: pathlib.Path, child: pathlib.Path) -> None:
    root_resolved = root.resolve()
    child_resolved = child.resolve()
    if child_resolved != root_resolved and root_resolved not in child_resolved.parents:
        raise DrillError(f"tar member escapes extraction root: {child}")


def safe_extract_tar(tar_path: pathlib.Path, extract_root: pathlib.Path) -> None:
    extract_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r") as tf:
        for member in tf.getmembers():
            name = pathlib.PurePosixPath(member.name)
            if member.name.startswith("/") or ".." in name.parts:
                raise DrillError(f"tar member escapes extraction root: {member.name}")
            if member.issym() or member.islnk():
                raise DrillError(f"tar link members are not allowed: {member.name}")
            if not member.isdir() and not member.isfile():
                raise DrillError(f"tar special members are not allowed: {member.name}")
            target = extract_root / member.name
            assert_inside(extract_root, target)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tf.extractfile(member)
            if source is None:
                raise DrillError(f"tar file member could not be read: {member.name}")
            with source, target.open("wb") as out:
                shutil.copyfileobj(source, out)


def assert_extracted_tree(extract_root: pathlib.Path, agent: str) -> pathlib.Path:
    agent_root = extract_root / agent
    if not agent_root.is_dir():
        raise DrillError(f"LOUD: malformed offsite tree: missing agent dir {agent_root}")
    candidates = []
    for date_dir in agent_root.iterdir():
        if not date_dir.is_dir() or not DATE_RE.fullmatch(date_dir.name):
            continue
        if (date_dir / "manifest.json").is_file() and list(date_dir.glob("*.zip.gpg")):
            candidates.append(date_dir)
    if not candidates:
        raise DrillError(f"LOUD: malformed offsite tree: no {agent}/YYYY-MM-DD manifest.json plus *.zip.gpg")
    return sorted(candidates, key=lambda p: p.name, reverse=True)[0]


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def invoke_restore_drill(cfg: RunConfig, extract_root: pathlib.Path) -> Dict[str, Any]:
    if not cfg.drill_script.exists():
        raise DrillError(f"LOUD: restore-drill.py not found at {cfg.drill_script}")
    inner_state = cfg.state_dir / "inner-restore-drill"
    inner_state.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "FBRD_BACKUP_ROOT": str(extract_root),
        "FBRD_AGENT": cfg.agent,
        "FBRD_STATE_DIR": str(inner_state),
        "FBRD_SCRATCH_ROOT": str(cfg.state_dir / "inner-scratch"),
        "FBRD_QUARANTINE_ROOT": str(cfg.state_dir / "inner-quarantine"),
    })
    proc = run_command([sys.executable, str(cfg.drill_script)], cfg.timeout_seconds, env=env)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise DrillError(f"LOUD: restore-drill.py failed rc={proc.returncode}: {detail}")
    ok_path = inner_state / "drill.ok"
    if not ok_path.exists():
        raise DrillError(f"LOUD: restore-drill.py exited 0 but wrote no {ok_path}")
    raw = ok_path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {"raw": raw}
    return parsed


def write_success_proof(cfg: RunConfig, target: TargetInfo, tar_path: pathlib.Path,
                        inner_drill_ok: Dict[str, Any]) -> Tuple[pathlib.Path, pathlib.Path]:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    proof = {
        "ts": now,
        "source": "offsite2",
        "remote": cfg.remote,
        "tar": target.remote_path,
        "date": target.date,
        "agent": cfg.agent,
        "sha256_verified": True,
        "tar_sha256": sha256_file(tar_path),
        "inner_drill_ok": inner_drill_ok,
    }
    proof_path = cfg.state_dir / "offsite-drill.ok"
    proof_path.write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    heartbeat_path = cfg.state_dir / DEADMAN_RELATIVE
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_path.write_text(now + "\n", encoding="utf-8")
    return proof_path, heartbeat_path


def run_with_target(cfg: RunConfig, target: TargetInfo, keep_scratch: bool = False) -> RunResult:
    scratch_root = cfg.scratch_root.resolve()
    scratch_root.mkdir(parents=True, exist_ok=True)
    run_root = pathlib.Path(tempfile.mkdtemp(prefix="offsite-restore-drill-", dir=str(scratch_root)))
    try:
        pull_dir = run_root / "pull"
        extract_root = run_root / "extract"
        tar_path = pull_latest_offsite(cfg, target, pull_dir)
        safe_extract_tar(tar_path, extract_root)
        assert_inside(scratch_root, extract_root)
        assert_extracted_tree(extract_root, cfg.agent)
        inner = invoke_restore_drill(cfg, extract_root)
        proof_path, heartbeat_path = write_success_proof(cfg, target, tar_path, inner)
        result = RunResult(proof_path=proof_path, heartbeat_path=heartbeat_path,
                           tar_path=tar_path, extract_root=extract_root)
    except Exception:
        quarantine = cfg.state_dir / "quarantine" / run_root.name
        quarantine.parent.mkdir(parents=True, exist_ok=True)
        if run_root.exists() and not quarantine.exists():
            shutil.move(str(run_root), str(quarantine))
        raise
    else:
        if not keep_scratch:
            shutil.rmtree(run_root, ignore_errors=True)
        return result


def run(cfg: RunConfig) -> RunResult:
    target = check_target(cfg)
    return run_with_target(cfg, target)


def make_fixture_tar(root: pathlib.Path, agent: str = DEFAULT_AGENT, date: str = "2026-07-01") -> pathlib.Path:
    payload = root / "payload" / agent / date
    payload.mkdir(parents=True, exist_ok=True)
    blob = payload / "config-full.zip.gpg"
    blob.write_bytes(b"encrypted-fixture-bytes\n")
    manifest = {
        "agent": agent,
        "date": date,
        "files": {blob.name: {"size": blob.stat().st_size, "sha256": sha256_file(blob)}},
    }
    (payload / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    tar_path = root / f"hermes-fleet-encrypted-{date}.tar"
    with tarfile.open(tar_path, "w") as tf:
        tf.add(payload.parent.parent / agent, arcname=agent)
    return tar_path


def make_stub_drill(path: pathlib.Path) -> pathlib.Path:
    script = path / "restore-drill-stub.py"
    script.write_text(
        "import json, os, pathlib\n"
        "state = pathlib.Path(os.environ['FBRD_STATE_DIR'])\n"
        "state.mkdir(parents=True, exist_ok=True)\n"
        "root = pathlib.Path(os.environ['FBRD_BACKUP_ROOT'])\n"
        "agent = os.environ.get('FBRD_AGENT', 'apollo')\n"
        "ok = {'stub': True, 'agent': agent, 'saw_root': root.exists()}\n"
        "(state / 'drill.ok').write_text(json.dumps(ok), encoding='utf-8')\n",
        encoding="utf-8",
    )
    return script


def selfcheck() -> None:
    with tempfile.TemporaryDirectory(prefix="offsite-restore-drill-selfcheck-") as tmp:
        base = pathlib.Path(tmp)
        tar_path = make_fixture_tar(base)
        stub = make_stub_drill(base)
        cfg = RunConfig(
            remote="selfcheck-local-fixture:",
            rclone_bin=None,
            agent=DEFAULT_AGENT,
            drill_script=stub,
            scratch_root=base / "scratch",
            state_dir=base / "state",
            min_tar_bytes=1,
            timeout_seconds=30,
        )
        extract_root = cfg.scratch_root / "extract"
        safe_extract_tar(tar_path, extract_root)
        assert_extracted_tree(extract_root, cfg.agent)
        inner = invoke_restore_drill(cfg, extract_root)
        fake_target = TargetInfo(cfg.remote, "2026-07-01", tar_path.name, "selfcheck-local-fixture:/2026-07-01/" + tar_path.name, tar_path.stat().st_size)
        proof_path, _ = write_success_proof(cfg, fake_target, tar_path, inner)
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        if proof.get("source") != "offsite2" or not proof.get("inner_drill_ok"):
            raise DrillError("selfcheck proof did not contain expected offsite2 inner drill proof")


def build_config(args: argparse.Namespace, environ: Optional[Dict[str, str]] = None) -> RunConfig:
    env = environ if environ is not None else os.environ
    remote = args.remote or env.get("FBRD_OFFSITE_REMOTE") or DEFAULT_REMOTE
    rclone_bin = args.rclone_bin or env.get("RCLONE_BIN")
    agent = args.agent or env.get("FBRD_AGENT") or DEFAULT_AGENT
    drill_script = expand_path(args.drill_script or env.get("FBRD_DRILL_SCRIPT") or DEFAULT_DRILL_SCRIPT)
    state_dir = expand_path(args.state_dir or env.get("FBRD_OFFSITE_STATE_DIR") or DEFAULT_STATE_DIR)
    scratch_root = expand_path(args.scratch_root or env.get("FBRD_OFFSITE_SCRATCH_ROOT") or str(state_dir / "scratch"))
    return RunConfig(
        remote=remote,
        rclone_bin=rclone_bin,
        agent=agent,
        drill_script=drill_script,
        scratch_root=scratch_root,
        state_dir=state_dir,
        min_tar_bytes=int(args.min_tar_bytes),
        timeout_seconds=int(args.timeout_seconds),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pull latest fleet-offsite2 tar and run restore-drill.py over it.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--selfcheck", action="store_true", help="offline health probe using a self-built fixture; no real remote")
    mode.add_argument("--check-target", action="store_true", help="real fleet-offsite2 liveness probe; loud non-zero on absent/empty target")
    mode.add_argument("--run", action="store_true", help="check real target, pull latest tar, extract, invoke restore-drill.py")
    parser.add_argument("--remote", default=None, help=f"rclone remote (default {DEFAULT_REMOTE!r} via FBRD_OFFSITE_REMOTE)")
    parser.add_argument("--rclone-bin", default=None, help="rclone binary path (or RCLONE_BIN); required for --check-target/--run")
    parser.add_argument("--agent", default=None, help=f"agent to drill (default {DEFAULT_AGENT!r} via FBRD_AGENT)")
    parser.add_argument("--drill-script", default=None, help=f"restore-drill.py path (default {DEFAULT_DRILL_SCRIPT})")
    parser.add_argument("--scratch-root", default=None, help="throwaway scratch root")
    parser.add_argument("--state-dir", default=None, help=f"state/proof dir (default {DEFAULT_STATE_DIR})")
    parser.add_argument("--min-tar-bytes", type=int, default=DEFAULT_MIN_TAR_BYTES, help="minimum acceptable remote tar size")
    parser.add_argument("--timeout-seconds", type=int, default=300, help="per subprocess timeout")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.selfcheck:
            selfcheck()
            print("OK: selfcheck passed using offline fixture; no real remote accessed")
            return 0
        cfg = build_config(args)
        if args.check_target:
            target = check_target(cfg)
            print(f"OK: target reachable and non-empty: {target.remote_path} ({target.size} bytes)")
            return 0
        result = run(cfg)
        print(f"OK: offsite restore drill proof written: {result.proof_path}")
        return 0
    except DrillError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired as exc:
        print(f"LOUD: subprocess timed out: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
