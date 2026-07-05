#!/usr/bin/env python3
"""pin-ai-models: one-time Mac local-AI model path policy.

Stdlib-only. Dry-run by default. Mutating paths are limited to the configured
runner default directories, the configured models root, and the JSON ledger.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_MODELS_ROOT = "/Volumes/Models SSD 4TB/models"
DEFAULT_LEDGER = "~/.pin_ai_models/state.json"


@dataclass(frozen=True)
class RunnerSpec:
    name: str
    source_default: str
    target_subdir: str


DEFAULT_RUNNERS: Tuple[RunnerSpec, ...] = (
    RunnerSpec("ollama", "~/.ollama/models", "ollama"),
    RunnerSpec("hf/mlx", "~/.cache/huggingface", "huggingface"),
    RunnerSpec("lmstudio", "~/.cache/lm-studio/models", "lmstudio"),
)


class PinError(RuntimeError):
    pass


def stable_runners(runners: Iterable[RunnerSpec]) -> List[RunnerSpec]:
    return sorted(runners, key=lambda r: r.name.lower())


def parse_runner_override(raw: str) -> RunnerSpec:
    parts = raw.split("=")
    if len(parts) != 3 or not all(parts):
        raise argparse.ArgumentTypeError("runner must be name=default_dir=target_subdir")
    return RunnerSpec(parts[0], parts[1], parts[2])


def resolve_path(raw: str) -> pathlib.Path:
    return pathlib.Path(os.path.expandvars(os.path.expanduser(raw))).resolve(strict=False)


def load_ledger(path: pathlib.Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "runs": []}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        raise PinError(f"ledger malformed: {path}")
    data.setdefault("version", 1)
    return data


def save_ledger(path: pathlib.Path, ledger: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


def _run_cmd(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def _run_tmutil(argv: Sequence[str], *, use_sudo: bool = False) -> subprocess.CompletedProcess[str]:
    cmd = list(argv)
    if use_sudo:
        cmd = ["sudo"] + cmd
    return _run_cmd(cmd)


def _preflight_privilege() -> Tuple[bool, str]:
    proc = _run_cmd(["sudo", "-n", "tmutil", "version"])
    if proc.returncode == 0:
        return True, ""
    msg = (
        "sudo preflight failed before mutation; run `sudo -v` or verify "
        "`sudo -n tmutil version` succeeds, then re-run --apply"
    )
    return False, msg


def _mounted_ancestor(path: pathlib.Path) -> Optional[pathlib.Path]:
    cur = path
    while True:
        if cur.exists() and os.path.ismount(cur):
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent


def _probe_writable(path: pathlib.Path) -> Tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".pin-ai-models-write-probe-{os.getpid()}"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
        return True, ""
    except OSError as exc:
        return False, str(exc)


def validate_models_root(models_root: pathlib.Path, *, require_mount: bool = True) -> Tuple[bool, str]:
    mounted = _mounted_ancestor(models_root)
    if require_mount and mounted is None:
        return False, f"external models volume not mounted for {models_root}"
    ok, err = _probe_writable(models_root)
    if not ok:
        return False, f"models root not writable: {models_root}: {err}"
    if not models_root.is_dir() or models_root.is_symlink():
        return False, f"models root is not a real directory: {models_root}"
    return True, ""


def _dir_nonempty(path: pathlib.Path) -> bool:
    return path.is_dir() and any(path.iterdir())


def _target_for(models_root: pathlib.Path, runner: RunnerSpec) -> pathlib.Path:
    return models_root / runner.target_subdir


def classify_source(source: pathlib.Path, target: pathlib.Path) -> str:
    if not source.exists() and not source.is_symlink():
        return "missing"
    if source.is_symlink():
        link = os.readlink(source)
        return "our_symlink" if pathlib.Path(link) == target else "foreign_symlink"
    if source.is_dir():
        return "real_dir"
    return "other"


def plan_lines(runners: Sequence[RunnerSpec], models_root: pathlib.Path) -> List[str]:
    lines = ["DRY-RUN plan for pin-ai-models", f"models-root: {models_root}"]
    for runner in stable_runners(runners):
        src = resolve_path(runner.source_default)
        tgt = _target_for(models_root, runner)
        state = classify_source(src, tgt)
        detail = ""
        if src.is_symlink():
            detail = f" -> {os.readlink(src)}"
        lines.append(f"{runner.name}: {src}{detail} => {tgt} [{state}]")
    lines.append("No changes made. Re-run with --apply to mutate or --undo to roll back recorded changes.")
    return lines


def _record(ledger: Dict[str, Any], runner: RunnerSpec, source: pathlib.Path, target: pathlib.Path,
            action: str, *, symlink_created: bool, backup_moved: bool,
            prior_link_target: Optional[str], tm_excluded: bool) -> None:
    ledger.setdefault("runs", []).append({
        "runner": runner.name,
        "action": action,
        "source_default": str(source),
        "target": str(target),
        "symlink_created": symlink_created,
        "backup_moved": backup_moved,
        "prior_link_target": prior_link_target,
        "tm_excluded": tm_excluded,
        "ts": time.time(),
    })


def _apply_one(runner: RunnerSpec, models_root: pathlib.Path, ledger: Dict[str, Any],
               tmutil: Callable[..., subprocess.CompletedProcess[str]] = _run_tmutil) -> Tuple[bool, str]:
    source = resolve_path(runner.source_default)
    target = _target_for(models_root, runner)
    backup = source.with_name(source.name + ".pin-bak")
    state = classify_source(source, target)

    if state == "our_symlink":
        return False, f"{runner.name}: already pinned, no change"

    if state == "real_dir" and _dir_nonempty(source) and _dir_nonempty(target):
        return False, f"{runner.name}: ABORT collision: source and target are both non-empty"

    if state in {"foreign_symlink", "real_dir"} and backup.exists():
        return False, f"{runner.name}: ABORT pre-existing backup blocks safe mutation: {backup}"

    if state == "other":
        return False, f"{runner.name}: ABORT unsupported source state at {source}"

    source.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)

    if state == "missing":
        source.symlink_to(target, target_is_directory=True)
        proc = tmutil(["tmutil", "addexclusion", "-p", str(target)], use_sudo=True)
        if proc.returncode != 0:
            raise PinError(f"tmutil addexclusion failed for {target}: {proc.stderr.strip()}")
        _record(ledger, runner, source, target, "created_symlink", symlink_created=True,
                backup_moved=False, prior_link_target=None, tm_excluded=True)
        return True, f"{runner.name}: created symlink and Time Machine fixed-path exclusion"

    if state == "foreign_symlink":
        prior = os.readlink(source)
        source.unlink()
        source.symlink_to(target, target_is_directory=True)
        proc = tmutil(["tmutil", "addexclusion", "-p", str(target)], use_sudo=True)
        if proc.returncode != 0:
            raise PinError(f"tmutil addexclusion failed for {target}: {proc.stderr.strip()}")
        _record(ledger, runner, source, target, "replaced_foreign_symlink", symlink_created=True,
                backup_moved=False, prior_link_target=prior, tm_excluded=True)
        return True, f"{runner.name}: replaced foreign symlink and recorded prior target"

    if state == "real_dir":
        shutil.move(str(source), str(backup))
        source.symlink_to(target, target_is_directory=True)
        proc = tmutil(["tmutil", "addexclusion", "-p", str(target)], use_sudo=True)
        if proc.returncode != 0:
            raise PinError(f"tmutil addexclusion failed for {target}: {proc.stderr.strip()}")
        _record(ledger, runner, source, target, "moved_and_linked", symlink_created=True,
                backup_moved=True, prior_link_target=None, tm_excluded=True)
        return True, f"{runner.name}: moved source to .pin-bak, linked target, excluded target"

    raise PinError(f"unhandled source state: {state}")


def run_apply(runners: Sequence[RunnerSpec], models_root: pathlib.Path, ledger_path: pathlib.Path,
              *, require_mount: bool = True,
              preflight: Callable[[], Tuple[bool, str]] = _preflight_privilege,
              tmutil: Callable[..., subprocess.CompletedProcess[str]] = _run_tmutil) -> Tuple[int, str]:
    priv_ok, priv_msg = preflight()
    if not priv_ok:
        return 2, "APPLY ABORT: " + priv_msg
    ok, msg = validate_models_root(models_root, require_mount=require_mount)
    if not ok:
        return 2, "APPLY ABORT: " + msg
    ledger = load_ledger(ledger_path)
    messages: List[str] = []
    changed = False
    failed = False
    before = json.dumps(ledger, sort_keys=True)
    for runner in stable_runners(runners):
        try:
            did_change, message = _apply_one(runner, models_root, ledger, tmutil=tmutil)
            changed = changed or did_change
            if "ABORT" in message:
                failed = True
            messages.append(message)
        except PinError as exc:
            failed = True
            messages.append(f"{runner.name}: ABORT {exc}")
    after = json.dumps(ledger, sort_keys=True)
    if after != before:
        save_ledger(ledger_path, ledger)
    if not changed and not failed:
        messages.append("already pinned, no change")
    return (2 if failed else 0), "\n".join(messages)


def run_undo(ledger_path: pathlib.Path,
             tmutil: Callable[..., subprocess.CompletedProcess[str]] = _run_tmutil) -> Tuple[int, str]:
    ledger = load_ledger(ledger_path)
    runs = list(ledger.get("runs", []))
    if not runs:
        return 0, "UNDO: no recorded actions"
    messages: List[str] = []
    failed = False
    for rec in reversed(runs):
        source = pathlib.Path(rec["source_default"])
        target = pathlib.Path(rec["target"])
        try:
            if rec.get("tm_excluded"):
                proc = tmutil(["tmutil", "removeexclusion", "-p", str(target)], use_sudo=True)
                if proc.returncode != 0:
                    raise PinError(proc.stderr.strip() or "tmutil removeexclusion failed")
            if rec.get("symlink_created"):
                if source.is_symlink() and pathlib.Path(os.readlink(source)) == target:
                    source.unlink()
                elif source.exists() or source.is_symlink():
                    raise PinError(f"refusing to undo {source}: not the recorded symlink")
            if rec.get("action") == "replaced_foreign_symlink":
                prior = rec.get("prior_link_target")
                if not prior:
                    raise PinError("missing prior_link_target")
                source.parent.mkdir(parents=True, exist_ok=True)
                source.symlink_to(prior)
            if rec.get("backup_moved"):
                backup = source.with_name(source.name + ".pin-bak")
                if source.exists() or source.is_symlink():
                    raise PinError(f"refusing to restore backup over existing {source}")
                shutil.move(str(backup), str(source))
            messages.append(f"{rec.get('runner')}: undone {rec.get('action')}")
        except (OSError, PinError) as exc:
            failed = True
            messages.append(f"{rec.get('runner')}: UNDO ABORT {exc}")
    if not failed:
        ledger["runs"] = []
        save_ledger(ledger_path, ledger)
    return (2 if failed else 0), "\n".join(messages)


def _tm_is_excluded(path: pathlib.Path,
                    tmutil: Callable[..., subprocess.CompletedProcess[str]] = _run_tmutil) -> bool:
    proc = tmutil(["tmutil", "isexcluded", str(path)], use_sudo=False)
    text = (proc.stdout + proc.stderr).lower()
    return proc.returncode == 0 and ("[excluded]" in text or ("excluded" in text and "not excluded" not in text))


def run_check_target(runners: Sequence[RunnerSpec], models_root: pathlib.Path, ledger_path: pathlib.Path,
                     *, require_mount: bool = True,
                     tmutil: Callable[..., subprocess.CompletedProcess[str]] = _run_tmutil) -> Tuple[int, str]:
    messages: List[str] = []
    ok, msg = validate_models_root(models_root, require_mount=require_mount)
    if not ok:
        return 2, "PIN_AI_MODELS_LIVENESS_FAIL: external models volume not mounted or unusable: " + msg
    ledger = load_ledger(ledger_path)
    runs = ledger.get("runs", [])
    if not runs:
        return 0, "PIN_AI_MODELS_INFO: no runners pinned yet"
    failed = False
    by_runner = {r.name: r for r in runners}
    pinned_names = {str(rec.get("runner")) for rec in runs}
    for rec in runs:
        runner = str(rec.get("runner"))
        source = pathlib.Path(str(rec.get("source_default")))
        target = pathlib.Path(str(rec.get("target")))
        if not source.is_symlink() or pathlib.Path(os.readlink(source)) != target:
            failed = True
            messages.append(f"PIN_AI_MODELS_PAGE: {runner} was pinned, now UNPINNED or drifted: {source}")
        if not target.is_dir():
            failed = True
            messages.append(f"PIN_AI_MODELS_PAGE: {runner} target missing: {target}")
        if not _tm_is_excluded(target, tmutil=tmutil):
            failed = True
            messages.append(f"PIN_AI_MODELS_PAGE: {runner} target is being backed up: {target}")
    for runner in stable_runners(runners):
        if runner.name not in pinned_names:
            messages.append(f"PIN_AI_MODELS_INFO: {runner.name} not in ledger; never pinned or not installed")
    return (2 if failed else 0), "\n".join(messages) if messages else "PIN_AI_MODELS_OK"


def run_selfcheck() -> Tuple[int, str]:
    class Proc:
        def __init__(self, code=0, stdout=""):
            self.returncode = code
            self.stdout = stdout
            self.stderr = ""
    tm_calls: List[List[str]] = []
    def tm(argv, *, use_sudo=False):
        tm_calls.append(list(argv))
        if "isexcluded" in argv:
            return Proc(0, "[Excluded] ")
        return Proc(0, "ok")
    def preflight():
        return True, ""
    try:
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            home = base / "home"
            root = base / "volume" / "models"
            ledger = base / "ledger" / "state.json"
            missing = RunnerSpec("missing", str(home / "missing"), "missing")
            real = RunnerSpec("real", str(home / "real"), "real")
            foreign = RunnerSpec("foreign", str(home / "foreign"), "foreign")
            collision = RunnerSpec("collision", str(home / "collision"), "collision")
            pinbak = RunnerSpec("pinbak", str(home / "pinbak"), "pinbak")
            other = RunnerSpec("other", str(home / "other"), "other")
            (home / "real").mkdir(parents=True)
            (home / "real" / "x").write_text("x", encoding="utf-8")
            old = base / "old-target"
            old.mkdir()
            (home / "foreign").parent.mkdir(parents=True, exist_ok=True)
            (home / "foreign").symlink_to(old, target_is_directory=True)
            (home / "collision").mkdir(parents=True)
            (home / "collision" / "x").write_text("x", encoding="utf-8")
            (root / "collision").mkdir(parents=True)
            (root / "collision" / "y").write_text("y", encoding="utf-8")
            (home / "pinbak").mkdir(parents=True)
            (home / "pinbak.pin-bak").mkdir(parents=True)
            (home / "other").parent.mkdir(parents=True, exist_ok=True)
            (home / "other").write_text("file", encoding="utf-8")
            code, out = run_apply([missing, real, foreign], root, ledger, require_mount=False, preflight=preflight, tmutil=tm)
            if code != 0:
                return 1, "selfcheck apply failed: " + out
            code2, out2 = run_apply([missing, real, foreign], root, ledger, require_mount=False, preflight=preflight, tmutil=tm)
            if code2 != 0 or "already pinned" not in out2:
                return 1, "selfcheck idempotency failed: " + out2
            code3, out3 = run_apply([collision, pinbak, other], root, base / "badledger.json", require_mount=False, preflight=preflight, tmutil=tm)
            if code3 == 0 or "ABORT" not in out3:
                return 1, "selfcheck abort paths failed: " + out3
            code4, out4 = run_undo(ledger, tmutil=tm)
            if code4 != 0:
                return 1, "selfcheck undo failed: " + out4
            if not (home / "foreign").is_symlink() or os.readlink(home / "foreign") != str(old):
                return 1, "selfcheck foreign symlink restore failed"
            return 0, "SELF CHECK PASS: offline pin/undo/idempotency/collision paths OK"
    except Exception as exc:
        return 1, f"SELF CHECK FAIL: {exc}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pin_ai_models")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--undo", action="store_true")
    mode.add_argument("--selfcheck", action="store_true")
    mode.add_argument("--check-target", action="store_true")
    p.add_argument("--models-root", default=DEFAULT_MODELS_ROOT)
    p.add_argument("--ledger", default=DEFAULT_LEDGER)
    p.add_argument("--runner", action="append", type=parse_runner_override, default=[])
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    runners = tuple(args.runner) if args.runner else DEFAULT_RUNNERS
    models_root = resolve_path(args.models_root)
    ledger = resolve_path(args.ledger)
    if args.selfcheck:
        code, out = run_selfcheck()
    elif args.check_target:
        code, out = run_check_target(runners, models_root, ledger)
    elif args.undo:
        code, out = run_undo(ledger)
    elif args.apply:
        code, out = run_apply(runners, models_root, ledger)
    else:
        code, out = 0, "\n".join(plan_lines(runners, models_root))
    stream = sys.stderr if code else sys.stdout
    print(out, file=stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
