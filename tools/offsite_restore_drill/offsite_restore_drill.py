#!/usr/bin/env python3
"""offsite-restore-drill: minimal offsite target freshness probe.

This tool intentionally does one small thing for the deterministic floor: prove a
configured rclone offsite target has a dated fleet encrypted tar whose reported
size is above a floor, then write a local proof + heartbeat.  It does not restore,
decrypt, extract, or mutate the remote.  ``--selfcheck`` stays fully offline.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

DEFAULT_REMOTE = "fleet-offsite2:"
DEFAULT_AGENT = "apollo"
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
    state_dir: pathlib.Path
    min_tar_bytes: int
    timeout_seconds: int


@dataclass(frozen=True)
class RunResult:
    proof_path: pathlib.Path
    heartbeat_path: pathlib.Path
    target: TargetInfo


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
    candidates = [explicit, env.get("RCLONE_BIN"), shutil.which("rclone", path=env.get("PATH"))]
    for candidate in candidates:
        if not candidate:
            continue
        if os.sep not in candidate:
            found = shutil.which(candidate, path=env.get("PATH"))
            if found:
                return str(pathlib.Path(found).resolve())
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
    names: List[str] = []
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

    for date in dates:
        dir_remote = remote_join(remote, date)
        lsf = run_command([rclone_bin, "lsf", dir_remote, "--files-only"], timeout)
        tars = parse_lsf_tars(require_ok(lsf, f"rclone lsf {dir_remote}"))
        if not tars:
            continue
        name = tars[0]
        remote_path = remote_join(remote, date, name)
        size_proc = run_command([rclone_bin, "size", remote_path, "--json"], timeout)
        size = parse_rclone_size(require_ok(size_proc, f"rclone size {remote_path}"))
        if size < min_tar_bytes:
            raise DrillError(
                f"LOUD: latest offsite tar is truncated/empty: {remote_path} has {size} bytes "
                f"(< {min_tar_bytes})"
            )
        return TargetInfo(remote=remote, date=date, name=name, remote_path=remote_path, size=size)
    raise DrillError(f"LOUD: no offsite tar found under dated directories on {remote}")


def check_target(cfg: RunConfig) -> TargetInfo:
    rclone = resolve_rclone(cfg.rclone_bin)
    if not rclone:
        raise DrillError("LOUD: rclone not resolvable; set --rclone-bin or RCLONE_BIN before using fleet-offsite2:")
    return discover_latest_target(rclone, cfg.remote, cfg.min_tar_bytes, cfg.timeout_seconds)


def write_success_proof(cfg: RunConfig, target: TargetInfo) -> Tuple[pathlib.Path, pathlib.Path]:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    proof = {
        "ts": now,
        "source": "offsite2",
        "remote": cfg.remote,
        "tar": target.remote_path,
        "date": target.date,
        "agent": cfg.agent,
        "target_verified": True,
        "size": target.size,
        "min_tar_bytes": cfg.min_tar_bytes,
    }
    proof_path = cfg.state_dir / "offsite-drill.ok"
    proof_path.write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    heartbeat_path = cfg.state_dir / DEADMAN_RELATIVE
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_path.write_text(now + "\n", encoding="utf-8")
    return proof_path, heartbeat_path


def run_with_target(cfg: RunConfig, target: TargetInfo) -> RunResult:
    if target.size < cfg.min_tar_bytes:
        raise DrillError(
            f"LOUD: latest offsite tar is truncated/empty: {target.remote_path} has {target.size} bytes "
            f"(< {cfg.min_tar_bytes})"
        )
    proof_path, heartbeat_path = write_success_proof(cfg, target)
    return RunResult(proof_path=proof_path, heartbeat_path=heartbeat_path, target=target)


def run(cfg: RunConfig) -> RunResult:
    return run_with_target(cfg, check_target(cfg))


def selfcheck() -> None:
    with tempfile.TemporaryDirectory(prefix="offsite-restore-drill-selfcheck-") as tmp:
        base = pathlib.Path(tmp)
        cfg = RunConfig(
            remote="selfcheck-local-fixture:",
            rclone_bin=None,
            agent=DEFAULT_AGENT,
            state_dir=base / "state",
            min_tar_bytes=1,
            timeout_seconds=30,
        )
        target = TargetInfo(
            remote=cfg.remote,
            date="2026-07-01",
            name="hermes-fleet-encrypted-2026-07-01.tar",
            remote_path="selfcheck-local-fixture:/2026-07-01/hermes-fleet-encrypted-2026-07-01.tar",
            size=2048,
        )
        result = run_with_target(cfg, target)
        proof = json.loads(result.proof_path.read_text(encoding="utf-8"))
        if proof.get("source") != "offsite2" or proof.get("target_verified") is not True:
            raise DrillError("selfcheck proof did not contain expected offsite target proof")
        if not result.heartbeat_path.exists():
            raise DrillError("selfcheck did not write heartbeat")


def build_config(args: argparse.Namespace, environ: Optional[Dict[str, str]] = None) -> RunConfig:
    env = environ if environ is not None else os.environ
    remote = args.remote or env.get("FBRD_OFFSITE_REMOTE") or DEFAULT_REMOTE
    rclone_bin = args.rclone_bin or env.get("RCLONE_BIN")
    agent = args.agent or env.get("FBRD_AGENT") or DEFAULT_AGENT
    state_dir = expand_path(args.state_dir or env.get("FBRD_OFFSITE_STATE_DIR") or DEFAULT_STATE_DIR)
    return RunConfig(
        remote=remote,
        rclone_bin=rclone_bin,
        agent=agent,
        state_dir=state_dir,
        min_tar_bytes=int(args.min_tar_bytes),
        timeout_seconds=int(args.timeout_seconds),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check latest fleet-offsite2 tar freshness and write a proof heartbeat.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--selfcheck", action="store_true", help="offline health probe using a self-built target; no real remote")
    mode.add_argument("--check-target", action="store_true", help="real fleet-offsite2 liveness probe; loud non-zero on absent/empty target")
    mode.add_argument("--run", action="store_true", help="check real target and write offsite target freshness proof")
    parser.add_argument("--remote", default=None, help=f"rclone remote (default {DEFAULT_REMOTE!r} via FBRD_OFFSITE_REMOTE)")
    parser.add_argument("--rclone-bin", default=None, help="rclone binary path (or RCLONE_BIN); required for --check-target/--run")
    parser.add_argument("--agent", default=None, help=f"agent label to record in proof (default {DEFAULT_AGENT!r} via FBRD_AGENT)")
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
            print("OK: selfcheck passed using offline target fixture; no real remote accessed")
            return 0
        cfg = build_config(args)
        if args.check_target:
            target = check_target(cfg)
            print(f"OK: target reachable and non-empty: {target.remote_path} ({target.size} bytes)")
            return 0
        result = run(cfg)
        print(f"OK: offsite target freshness proof written: {result.proof_path}")
        return 0
    except DrillError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired as exc:
        print(f"LOUD: subprocess timed out: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
