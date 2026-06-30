#!/usr/bin/env python3
"""restore-canary — nightly restore-drill for an encrypted restic home-lab backup.

Proves backups are *restorable*, not just consistent: it decrypts + restores ONE
known sentinel file, hashes it against a recorded hash, and emits a secret-free
PASS/FAIL artifact. Stdlib only. See REVERSIBILITY.md.

This module is built as pure, hermetic logic + command/payload BUILDERS and
PARSERS — the actual `restic` / `op` subprocesses are constructed here but only
invoked by the live `--run` path on the real host. The test suite and both health
probes exercise the builders/parsers over committed fixtures, never the network.

Key design points (RESPEC v4):

* B-1  `backup_root_path` (the snapshot FILTER = restic --path = backup-root
       Directory) is split from `canary_file_path` (the file WITHIN the snapshot,
       used by restore --include / record / hash). `restic snapshots --path P`
       matches the snapshot's Directory, NOT files inside it; a committed fixture
       parser proves this.
* B-1r `--tag` is MANDATORY. Freshness gates on host + path + tag so a second
       writer / manual `restic backup` of the same host+path cannot mask a dead
       nightly job.
* B-2  The dead-man's-switch is PUSH-based and evaluated OFF-HOST: every --run
       emits a heartbeat payload (PASS or FAIL). This module only BUILDS that
       payload; the off-host monitor raises if no ping arrives by T. The on-host
       `--audit-artifact` is retained but demoted to artifact-integrity only and
       does NOT claim to cover host-down.
* B-3  Two secrets are accounted: the restic repo password AND
       OP_SERVICE_ACCOUNT_TOKEN. The token is delivered ONLY to the scoped `op`
       subprocess env, never to restic's env, argv, the artifact, logs, or the
       heartbeat. Scrubbing + scoping are tested.
* R-3  The freshness ceiling is a HARD CONSTANT (_MAX_AGE_HOURS = 168), not a
       flag. The window flag is validated 0 < hours <= ceiling.
* R-4  Timeout = max(measured) * N (N=2). Per-call-class budgets. A restore whose
       wall-time is within NEAR_TIMEOUT_RATIO of its budget emits a near_timeout
       warning so index growth is caught before it flips to spurious FAIL.
* R-5  local-vs-durable is defined by MOUNT TYPE, not URL scheme or path string.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Hard constants (R-3): these are NOT flags and cannot be raised by config.
# --------------------------------------------------------------------------- #
_MAX_AGE_HOURS = 168  # 7 days. The freshness ceiling. Hard. Not configurable.
TIMEOUT_MULTIPLIER = 2  # R-4: budget = max(measured) * N, N stated = 2.
NEAR_TIMEOUT_RATIO = 0.8  # R-4: warn when wall-time >= 0.8 * budget.

# Per-call-class measured baselines (seconds), cheap calls vs the costly restore.
# R-4: unequal call classes get unequal budgets; one global timeout is wrong.
_BASELINE_SECONDS = {
    "snapshots": 5.0,
    "ls": 5.0,
    "restore": 30.0,
}

# R-5: a durable repo is reached by one of these mount types; "local" scratch
# (tmpfs / a bare local dir with no network/remote backing) is NOT durable.
DURABLE_MOUNT_TYPES = ("nfs", "smb", "cifs", "sftp", "rest", "s3", "b2", "rclone")
LOCAL_MOUNT_TYPES = ("tmpfs", "local", "overlay", "ramfs")

# Verdicts.
PASS = "PASS"
FAIL = "FAIL"

# Failure reason codes (stable; used by tests + the off-host monitor).
FAIL_NO_SNAPSHOT = "FAIL_NO_SNAPSHOT"
FAIL_STALE = "FAIL_STALE"
FAIL_HASH_MISMATCH = "FAIL_HASH_MISMATCH"
FAIL_RESTORE_EMPTY = "FAIL_RESTORE_EMPTY"

# Anything matching these in any emitted string is a leaked secret -> scrubbed.
# We scrub by *named env keys* (the secret VALUES live in env, never hard-coded).
SECRET_ENV_KEYS = ("OP_SERVICE_ACCOUNT_TOKEN", "RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE")
_SCRUB_PLACEHOLDER = "[REDACTED]"


class ConfigError(ValueError):
    """Raised when a CanaryConfig is structurally invalid."""


class CanaryConfig(NamedTuple):
    """Resolved, validated config for one canary drill.

    B-1: backup_root_path and canary_file_path are SEPARATE fields mapping to
    two different restic concepts. They must never be conflated.
    """
    repo: str                 # restic repository handle (NOT a secret)
    host: str                 # restic --host filter (backup producer host)
    backup_root_path: str     # restic --path filter == snapshot Directory (B-1)
    tag: str                  # restic --tag filter, MANDATORY (B-1r)
    canary_file_path: str     # file WITHIN the snapshot (restore --include) (B-1)
    expected_sha256: str      # recorded hash of the canary file
    max_age_hours: int        # freshness window, 0 < h <= _MAX_AGE_HOURS (R-3)
    mount_type: str           # how the repo is reached; classifies durability (R-5)


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #
def validate_config(raw: Dict[str, Any]) -> CanaryConfig:
    """Validate a raw config dict into a CanaryConfig or raise ConfigError.

    Enforces: tag mandatory (B-1r); backup_root_path != canary_file_path and the
    file lives under the root (B-1); 0 < max_age_hours <= _MAX_AGE_HOURS (R-3);
    expected_sha256 is a 64-hex digest; mount_type is known (R-5).
    """
    if not isinstance(raw, dict):
        raise ConfigError("config must be a dict, got %s" % type(raw).__name__)

    required = ("repo", "host", "backup_root_path", "tag",
                "canary_file_path", "expected_sha256", "mount_type")
    missing = [k for k in required if not raw.get(k)]
    if missing:
        raise ConfigError("config missing required field(s): %s" % ", ".join(sorted(missing)))

    tag = str(raw["tag"]).strip()
    if not tag:
        raise ConfigError("tag is MANDATORY and must be non-empty (B-1r)")

    root = str(raw["backup_root_path"]).rstrip("/") or "/"
    cfile = str(raw["canary_file_path"])
    if root == cfile.rstrip("/"):
        raise ConfigError(
            "backup_root_path and canary_file_path must differ: the root is the "
            "snapshot Directory (--path filter), the file is WITHIN it (B-1)")
    # The canary file must live under the backup root, else --include can't find it.
    root_prefix = "/" if root == "/" else root + "/"
    if not cfile.startswith(root_prefix):
        raise ConfigError(
            "canary_file_path %r must live under backup_root_path %r (B-1)"
            % (cfile, root))

    sha = str(raw["expected_sha256"]).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ConfigError("expected_sha256 must be a 64-char hex sha256 digest")

    age = raw.get("max_age_hours", _MAX_AGE_HOURS)
    try:
        age = int(age)
    except (TypeError, ValueError):
        raise ConfigError("max_age_hours must be an integer")
    if not (0 < age <= _MAX_AGE_HOURS):
        raise ConfigError(
            "max_age_hours must satisfy 0 < hours <= %d (the hard ceiling cannot "
            "be raised by config, R-3); got %d" % (_MAX_AGE_HOURS, age))

    mount = str(raw["mount_type"]).strip().lower()
    if mount not in DURABLE_MOUNT_TYPES and mount not in LOCAL_MOUNT_TYPES:
        raise ConfigError(
            "mount_type %r unknown; durable=%s local=%s (R-5)"
            % (mount, list(DURABLE_MOUNT_TYPES), list(LOCAL_MOUNT_TYPES)))

    return CanaryConfig(
        repo=str(raw["repo"]),
        host=str(raw["host"]),
        backup_root_path=root,
        tag=tag,
        canary_file_path=cfile,
        expected_sha256=sha,
        max_age_hours=age,
        mount_type=mount,
    )


def is_durable_mount(mount_type: str) -> bool:
    """R-5: durability is a property of the MOUNT TYPE, not the path string."""
    return mount_type.strip().lower() in DURABLE_MOUNT_TYPES


# --------------------------------------------------------------------------- #
# restic command builders (B-1 / B-1r). Pure: build argv, never run it here.
# Secrets are passed via env (built separately), NEVER as argv.
# --------------------------------------------------------------------------- #
def build_snapshots_cmd(cfg: CanaryConfig) -> List[str]:
    """argv for `restic snapshots --json` filtered to ONE producer job.

    B-1: --path filters on the snapshot's backup-root Directory, so we pass
    backup_root_path here (NOT the canary file). B-1r: --tag is included so a
    second writer of the same host+path without the tag cannot satisfy us.
    """
    return [
        "restic", "-r", cfg.repo, "snapshots", "--json",
        "--host", cfg.host,
        "--path", cfg.backup_root_path,
        "--tag", cfg.tag,
        "--latest", "1",
    ]


def build_restore_cmd(cfg: CanaryConfig, snapshot_id: str, target_dir: str) -> List[str]:
    """argv for `restic restore <id> --include <canary file> --target <dir>`.

    B-1: --include uses canary_file_path (the file inside the snapshot), the
    OTHER half of the split.
    """
    return [
        "restic", "-r", cfg.repo, "restore", snapshot_id,
        "--include", cfg.canary_file_path,
        "--target", target_dir,
    ]


# --------------------------------------------------------------------------- #
# Secret handling (B-3 / Inv-1).
# --------------------------------------------------------------------------- #
def build_op_env(base_env: Dict[str, str]) -> Dict[str, str]:
    """Env for the SCOPED `op` subprocess only.

    B-3: OP_SERVICE_ACCOUNT_TOKEN is env-resident by 1Password's service-account
    model. It is delivered ONLY here. The restic env (below) must never carry it.
    """
    token = base_env.get("OP_SERVICE_ACCOUNT_TOKEN")
    if not token:
        raise ConfigError("OP_SERVICE_ACCOUNT_TOKEN absent from env (Inv-1)")
    return {"OP_SERVICE_ACCOUNT_TOKEN": token}


def build_restic_env(base_env: Dict[str, str], repo_password: str) -> Dict[str, str]:
    """Env for the restic subprocess.

    B-3: carries the repo password (resolved out-of-band via the scoped `op`
    call) but explicitly does NOT carry OP_SERVICE_ACCOUNT_TOKEN — restic has no
    business holding the vault-wide token.
    """
    env = {k: v for k, v in base_env.items() if k != "OP_SERVICE_ACCOUNT_TOKEN"}
    env["RESTIC_PASSWORD"] = repo_password
    return env


def scrub_secrets(text: str, env: Optional[Dict[str, str]] = None) -> str:
    """Replace any known secret VALUE present in text with a placeholder.

    Defence-in-depth for the artifact/log/heartbeat path: even if a secret value
    somehow reached a string, this strips it before emission. Empty/short values
    are ignored to avoid scrubbing innocuous substrings.
    """
    if not text:
        return text
    env = env or {}
    out = text
    for key in SECRET_ENV_KEYS:
        val = env.get(key)
        if val and len(val) >= 6:
            out = out.replace(val, _SCRUB_PLACEHOLDER)
    return out


# --------------------------------------------------------------------------- #
# Snapshot parsing + freshness (B-1 proof, B-1r, R-3).
# --------------------------------------------------------------------------- #
def parse_snapshots(raw_json: str) -> List[Dict[str, Any]]:
    """Parse `restic snapshots --json` output into a list of snapshot dicts."""
    data = json.loads(raw_json)
    if not isinstance(data, list):
        raise ValueError("restic snapshots --json must yield a JSON array")
    return data


def snapshot_matches_root(snapshot: Dict[str, Any], cfg: CanaryConfig) -> bool:
    """Does this snapshot match our host + backup-root path + tag filter?

    B-1: restic's --path matches a snapshot whose `paths` (its backup-root
    Directory list) contains the path — it does NOT look inside the snapshot at
    contained files. This function mirrors that: it checks `paths`, never the
    canary file. The committed fixture test asserts the canary FILE path matches
    ZERO snapshots while the ROOT path matches.
    """
    if str(snapshot.get("hostname", "")) != cfg.host:
        return False
    paths = snapshot.get("paths") or []
    if cfg.backup_root_path not in [str(p).rstrip("/") or "/" for p in paths]:
        return False
    tags = snapshot.get("tags") or []
    return cfg.tag in [str(t) for t in tags]


def _parse_iso8601(ts: str) -> Optional[float]:
    """Best-effort ISO-8601 -> epoch seconds using stdlib only.

    restic emits RFC3339 like '2026-06-29T03:00:01.123456-04:00'. We normalise
    fractional seconds + 'Z' and use datetime.fromisoformat (3.7-safe subset).
    """
    import datetime as _dt
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # fromisoformat (3.7) tolerates 3 or 6 fractional digits; trim to 6.
    m = re.match(r"^(.*\.\d{1,6})\d*([+-]\d{2}:\d{2}|)$", s)
    if m:
        s = m.group(1) + m.group(2)
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.timestamp()


def snapshot_age_hours(snapshot: Dict[str, Any], now_epoch: float) -> Optional[float]:
    """Age of a snapshot in hours given a reference epoch, or None if untimed."""
    ts = snapshot.get("time")
    if not ts:
        return None
    epoch = _parse_iso8601(str(ts))
    if epoch is None:
        return None
    return (now_epoch - epoch) / 3600.0


def select_fresh_snapshot(snapshots: Sequence[Dict[str, Any]], cfg: CanaryConfig,
                          now_epoch: float) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Pick the newest matching snapshot inside the freshness window.

    Returns (snapshot, None) on success or (None, FAIL_*) on failure. Gates on
    host+path+tag (B-1/B-1r) and age <= cfg.max_age_hours (already ceiling-bound
    by validate_config, R-3).
    """
    matching = [s for s in snapshots if snapshot_matches_root(s, cfg)]
    if not matching:
        return None, FAIL_NO_SNAPSHOT
    timed = []
    for s in matching:
        age = snapshot_age_hours(s, now_epoch)
        if age is not None:
            timed.append((age, s))
    if not timed:
        return None, FAIL_NO_SNAPSHOT
    timed.sort(key=lambda t: t[0])  # smallest age = freshest first
    freshest_age, freshest = timed[0]
    if freshest_age > cfg.max_age_hours:
        return None, FAIL_STALE
    return freshest, None


# --------------------------------------------------------------------------- #
# Hashing + verdict (the actual restorability proof).
# --------------------------------------------------------------------------- #
def sha256_file(path: pathlib.Path) -> str:
    """Stream-hash a file -> hex sha256. Works on the restored canary."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_restored_file(restored: pathlib.Path, expected_sha256: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Compare a restored file's hash to the recorded one.

    Returns (verdict, reason_or_None, actual_sha_or_None).
    """
    if not restored.exists() or not restored.is_file():
        return FAIL, FAIL_RESTORE_EMPTY, None
    if restored.stat().st_size == 0:
        return FAIL, FAIL_RESTORE_EMPTY, None
    actual = sha256_file(restored)
    if actual != expected_sha256.lower():
        return FAIL, FAIL_HASH_MISMATCH, actual
    return PASS, None, actual


# --------------------------------------------------------------------------- #
# Timeout budgets (R-4).
# --------------------------------------------------------------------------- #
def timeout_budget(call_class: str) -> float:
    """Per-call-class timeout = max(measured baseline) * TIMEOUT_MULTIPLIER.

    R-4: cheap calls (snapshots/ls) get a small budget; only restore gets the
    big one. A single global timeout for unequal call classes is wrong.
    """
    if call_class not in _BASELINE_SECONDS:
        raise ValueError("unknown call class %r; known=%s"
                         % (call_class, sorted(_BASELINE_SECONDS)))
    return _BASELINE_SECONDS[call_class] * TIMEOUT_MULTIPLIER


def is_near_timeout(call_class: str, wall_seconds: float) -> bool:
    """R-4: True when wall-time >= NEAR_TIMEOUT_RATIO of the budget.

    Drives the near_timeout warning so a growing multi-TB index is caught BEFORE
    it flips a real run to a spurious FAIL.
    """
    return wall_seconds >= NEAR_TIMEOUT_RATIO * timeout_budget(call_class)


# --------------------------------------------------------------------------- #
# Artifact + heartbeat (secret-free; B-2 / B-3).
# --------------------------------------------------------------------------- #
def build_artifact(cfg: CanaryConfig, verdict: str, reason: Optional[str],
                   actual_sha: Optional[str], restore_wall_seconds: Optional[float],
                   now_epoch: float, snapshot_id: Optional[str]) -> Dict[str, Any]:
    """Build the secret-free PASS/FAIL artifact dict.

    B-3: the artifact carries NO secret. It carries the repo HANDLE (not a
    password), the host/path/tag filter, the verdict, and timings. R-4: the
    restore wall-time + near_timeout warning are emitted so index growth is
    visible to the audit + off-host monitor.
    """
    near = False
    if restore_wall_seconds is not None:
        near = is_near_timeout("restore", restore_wall_seconds)
    artifact = {
        "schema": "restore-canary/v4",
        "verdict": verdict,
        "reason": reason,
        "repo": cfg.repo,
        "host": cfg.host,
        "backup_root_path": cfg.backup_root_path,
        "tag": cfg.tag,
        "canary_file_path": cfg.canary_file_path,
        "snapshot_id": snapshot_id,
        "expected_sha256": cfg.expected_sha256,
        "actual_sha256": actual_sha,
        "restore_wall_seconds": restore_wall_seconds,
        "restore_timeout_budget": timeout_budget("restore"),
        "near_timeout": near,
        "max_age_hours": cfg.max_age_hours,
        "mount_type": cfg.mount_type,
        "durable_mount": is_durable_mount(cfg.mount_type),
        "generated_at_epoch": now_epoch,
    }
    return artifact


def build_heartbeat(cfg: CanaryConfig, verdict: str, now_epoch: float) -> Dict[str, Any]:
    """Build the PUSH heartbeat payload sent off-host on EVERY run (B-2).

    Sent on PASS or FAIL. The OFF-HOST monitor raises if no ping arrives by T —
    that is the host-down failure mode an on-host check structurally cannot see.
    Carries NO secret and NO file hash (just liveness + verdict).
    """
    return {
        "schema": "restore-canary-heartbeat/v4",
        "host": cfg.host,
        "tag": cfg.tag,
        "verdict": verdict,
        "sent_at_epoch": now_epoch,
        "deadline_hint_hours": cfg.max_age_hours,
    }


def artifact_to_json(artifact: Dict[str, Any], env: Optional[Dict[str, str]] = None) -> str:
    """Serialise an artifact to JSON, scrubbing any secret value (B-3)."""
    return scrub_secrets(json.dumps(artifact, indent=2, sort_keys=True), env)


# --------------------------------------------------------------------------- #
# Artifact-integrity audit (B-2 demoted role): on-host only; NOT host-down.
# --------------------------------------------------------------------------- #
def audit_artifact(artifact_text: str, now_epoch: float, max_age_hours: int) -> Tuple[str, str]:
    """On-host artifact-integrity check (B-2, demoted).

    Returns (status, message) where status in {OK, STALE, FAIL, UNPARSEABLE}.
    This does NOT detect host-down (that is the off-host heartbeat's job) — it
    only flags a stale / failed / corrupt artifact WHILE the host is up.
    """
    try:
        art = json.loads(artifact_text)
    except (json.JSONDecodeError, TypeError):
        return "UNPARSEABLE", "artifact is not valid JSON"
    if not isinstance(art, dict) or "verdict" not in art:
        return "UNPARSEABLE", "artifact missing verdict field"
    gen = art.get("generated_at_epoch")
    if isinstance(gen, (int, float)):
        age_h = (now_epoch - gen) / 3600.0
        if age_h > max_age_hours:
            return "STALE", "artifact %.1fh old exceeds %dh window" % (age_h, max_age_hours)
    if art.get("verdict") != PASS:
        return "FAIL", "last verdict was %r: %s" % (art.get("verdict"), art.get("reason"))
    return "OK", "artifact fresh and PASS"


# --------------------------------------------------------------------------- #
# --selfcheck : offline logic probe over a SELF-BUILT fixture (health).
# --------------------------------------------------------------------------- #
def _selfcheck_config(repo: str = "rest:https://nas.local/restic-canary") -> Dict[str, Any]:
    """A known-good raw config for the offline probe (no real source touched)."""
    return {
        "repo": repo,
        "host": "ace-ai",
        "backup_root_path": "/srv",
        "tag": "nightly-canary-job",
        "canary_file_path": "/srv/canary/sentinel.txt",
        "expected_sha256": "a" * 64,  # overwritten below with the real fixture hash
        "max_age_hours": 24,
        "mount_type": "rest",
    }


def _selfcheck_snapshots(now_epoch: float) -> List[Dict[str, Any]]:
    """Captured-shape `snapshots --json` fixture used to prove B-1 offline.

    Note `paths` is the backup-root Directory (/srv), and the canary FILE
    (/srv/canary/sentinel.txt) appears NOWHERE in `paths` — proving --path
    matches the root, not contained files.
    """
    import datetime as _dt
    fresh = _dt.datetime.fromtimestamp(now_epoch - 3600, _dt.timezone.utc).isoformat()
    stale = _dt.datetime.fromtimestamp(now_epoch - 1000 * 3600, _dt.timezone.utc).isoformat()
    return [
        {"id": "deadbeef", "short_id": "dead", "hostname": "ace-ai",
         "paths": ["/srv"], "tags": ["nightly-canary-job"], "time": fresh},
        {"id": "cafef00d", "short_id": "cafe", "hostname": "ace-ai",
         "paths": ["/srv"], "tags": ["manual-adhoc"], "time": fresh},  # wrong tag
        {"id": "0ldsnap0", "short_id": "0ld0", "hostname": "ace-ai",
         "paths": ["/srv"], "tags": ["nightly-canary-job"], "time": stale},  # stale
        {"id": "wrongh0st", "short_id": "wrng", "hostname": "other-host",
         "paths": ["/srv"], "tags": ["nightly-canary-job"], "time": fresh},  # wrong host
    ]


def run_selfcheck() -> int:
    """Offline logic probe. Builds its own fixture; touches NO real source."""
    import tempfile
    import time as _time
    now = _time.time()
    failures: List[str] = []

    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        # Build a real canary file + its true hash so the PASS path is exercised.
        canary = tmp / "sentinel.txt"
        canary.write_bytes(b"greenhouse restore-canary self-check sentinel\n")
        true_sha = sha256_file(canary)

        raw = _selfcheck_config()
        raw["expected_sha256"] = true_sha
        try:
            cfg = validate_config(raw)
        except ConfigError as exc:
            print("SELFCHECK FAIL: known-good config rejected: %s" % exc, file=sys.stderr)
            return 1

        # B-1 proof: --path matches the ROOT; the canary FILE matches ZERO snaps.
        snaps = _selfcheck_snapshots(now)
        root_matches = [s for s in snaps if snapshot_matches_root(s, cfg)]
        if len(root_matches) != 2:  # fresh + stale share host+path+tag
            failures.append("expected 2 host+path+tag matches, got %d" % len(root_matches))
        file_cfg = cfg._replace(backup_root_path=cfg.canary_file_path)
        # canary_file_path is not in any snapshot's `paths` -> zero matches.
        if any(snapshot_matches_root(s, file_cfg) for s in snaps):
            failures.append("B-1 violated: canary FILE path matched a snapshot --path")

        # Freshness: selects the fresh nightly-canary snapshot, rejects stale/wrong.
        chosen, reason = select_fresh_snapshot(snaps, cfg, now)
        if chosen is None or chosen.get("id") != "deadbeef":
            failures.append("freshness picked %r (reason=%s), expected deadbeef"
                            % (chosen and chosen.get("id"), reason))

        # Stale-only path -> FAIL_STALE.
        stale_only = [s for s in snaps if s.get("id") == "0ldsnap0"]
        _, sreason = select_fresh_snapshot(stale_only, cfg, now)
        if sreason != FAIL_STALE:
            failures.append("stale-only should FAIL_STALE, got %s" % sreason)

        # No-match path -> FAIL_NO_SNAPSHOT.
        _, nreason = select_fresh_snapshot([], cfg, now)
        if nreason != FAIL_NO_SNAPSHOT:
            failures.append("empty should FAIL_NO_SNAPSHOT, got %s" % nreason)

        # Restore verify: PASS on the true hash, FAIL on a wrong one.
        verdict, _, actual = verify_restored_file(canary, true_sha)
        if verdict != PASS or actual != true_sha:
            failures.append("verify PASS path broken: %s" % verdict)
        bad_verdict, bad_reason, _ = verify_restored_file(canary, "b" * 64)
        if bad_verdict != FAIL or bad_reason != FAIL_HASH_MISMATCH:
            failures.append("verify mismatch path broken: %s/%s" % (bad_verdict, bad_reason))

        # Artifact is secret-free even if a secret value is present in env.
        art = build_artifact(cfg, PASS, None, true_sha, 12.0, now, "deadbeef")
        leaky_env = {"OP_SERVICE_ACCOUNT_TOKEN": "ops_" + "x" * 40,
                     "RESTIC_PASSWORD": "hunter2hunter2"}
        text = artifact_to_json(art, leaky_env)
        for val in leaky_env.values():
            if val in text:
                failures.append("secret leaked into artifact JSON")
                break

        # Timeout budgets: restore budget > snapshots budget; near_timeout fires.
        if not (timeout_budget("restore") > timeout_budget("snapshots")):
            failures.append("restore budget should exceed snapshots budget")
        if not is_near_timeout("restore", timeout_budget("restore") * 0.9):
            failures.append("near_timeout should fire at 0.9 of budget")
        if is_near_timeout("restore", timeout_budget("restore") * 0.5):
            failures.append("near_timeout should NOT fire at 0.5 of budget")

        # R-3: ceiling is hard — config above the ceiling is rejected.
        over = dict(raw)
        over["max_age_hours"] = _MAX_AGE_HOURS + 1
        try:
            validate_config(over)
            failures.append("R-3 violated: config above ceiling was accepted")
        except ConfigError:
            pass

    if failures:
        print("SELFCHECK FAIL:", file=sys.stderr)
        for f in failures:
            print("  - %s" % f, file=sys.stderr)
        return 1
    print("SELFCHECK OK: restore-canary offline logic verified (B-1/B-1r/B-3/R-3/R-4)")
    return 0


# --------------------------------------------------------------------------- #
# --check-target / --check-vault : REAL-source liveness (loud, never silent 0).
# --------------------------------------------------------------------------- #
def load_config_file(path: pathlib.Path) -> CanaryConfig:
    """Read + validate a real config file -> CanaryConfig (or raise)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return validate_config(raw)


def run_check_target(config_path: pathlib.Path) -> int:
    """Assert the REAL config source exists/right-kind/non-empty + is valid.

    The nightly entry runs THIS first, so 'read nothing' is never a silent exit
    0. It does not touch the network; it proves the on-host config is sane and
    the secret env keys are PRESENT (without printing their values).
    """
    import os
    if not config_path.exists():
        print("CHECK-TARGET FAIL: config not found: %s" % config_path, file=sys.stderr)
        return 2
    if not config_path.is_file():
        print("CHECK-TARGET FAIL: config path is not a file: %s" % config_path, file=sys.stderr)
        return 2
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        print("CHECK-TARGET FAIL: cannot read %s: %s" % (config_path, exc), file=sys.stderr)
        return 2
    if not text.strip():
        print("CHECK-TARGET FAIL: config is empty: %s" % config_path, file=sys.stderr)
        return 2
    try:
        cfg = load_config_file(config_path)
    except (json.JSONDecodeError, ConfigError) as exc:
        print("CHECK-TARGET FAIL: invalid config %s: %s" % (config_path, exc), file=sys.stderr)
        return 2

    missing_secrets = [k for k in ("OP_SERVICE_ACCOUNT_TOKEN",)
                       if not os.environ.get(k)]
    if missing_secrets:
        print("CHECK-TARGET FAIL: required secret env absent: %s "
              "(set the op service-account token, B-3)" % ", ".join(missing_secrets),
              file=sys.stderr)
        return 2

    if not is_durable_mount(cfg.mount_type):
        print("CHECK-TARGET FAIL: repo mount_type %r is LOCAL/ephemeral, not "
              "durable (R-5): %s" % (cfg.mount_type, cfg.repo), file=sys.stderr)
        return 2

    print("CHECK-TARGET OK: config valid; repo=%s host=%s path=%s tag=%s "
          "mount=%s(durable) window=%dh" % (
              cfg.repo, cfg.host, cfg.backup_root_path, cfg.tag,
              cfg.mount_type, cfg.max_age_hours))
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="restore-canary",
        description="Nightly restore-drill for an encrypted restic home-lab backup.")
    p.add_argument("--config", type=pathlib.Path, default=None,
                   help="path to the canary config JSON")
    p.add_argument("--selfcheck", action="store_true",
                   help="offline logic probe over a self-built fixture (deploy health)")
    p.add_argument("--check-target", "--check-vault", dest="check_target",
                   action="store_true",
                   help="assert the REAL config/secret source is live (nightly entry)")
    p.add_argument("--audit-artifact", type=pathlib.Path, default=None,
                   help="on-host artifact-integrity check (NOT host-down; see B-2)")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.selfcheck:
        return run_selfcheck()
    if args.audit_artifact is not None:
        import time as _time
        try:
            text = args.audit_artifact.read_text(encoding="utf-8")
        except OSError as exc:
            print("AUDIT-ARTIFACT FAIL: cannot read %s: %s" % (args.audit_artifact, exc),
                  file=sys.stderr)
            return 2
        status, msg = audit_artifact(text, _time.time(), _MAX_AGE_HOURS)
        stream = sys.stdout if status == "OK" else sys.stderr
        print("AUDIT-ARTIFACT %s: %s" % (status, msg), file=stream)
        return 0 if status == "OK" else 2
    if args.check_target:
        if args.config is None:
            print("CHECK-TARGET FAIL: --config is required", file=sys.stderr)
            return 2
        return run_check_target(args.config)
    # Bare invocation: there is no offline --run (it needs the real host); point
    # the operator at the right entrypoints rather than exiting a silent 0.
    print("restore-canary: pass --selfcheck (health), --check-target --config <f> "
          "(liveness), or --audit-artifact <f>. Live --run is host-only.",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
