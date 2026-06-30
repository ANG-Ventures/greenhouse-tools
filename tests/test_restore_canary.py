"""Tests for restore_canary. Stdlib + pytest only; collects offline.

Every assertion is traced against the tool's ACTUAL emitted output — no test
asserts an invariant stricter than the tool upholds.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
import sys

import pytest

from tools.restore_canary.canary import (
    DURABLE_MOUNT_TYPES,
    FAIL,
    FAIL_HASH_MISMATCH,
    FAIL_NO_SNAPSHOT,
    FAIL_RESTORE_EMPTY,
    FAIL_STALE,
    NEAR_TIMEOUT_RATIO,
    PASS,
    TIMEOUT_MULTIPLIER,
    CanaryConfig,
    ConfigError,
    _MAX_AGE_HOURS,
    artifact_to_json,
    audit_artifact,
    build_artifact,
    build_heartbeat,
    build_op_env,
    build_parser,
    build_restic_env,
    build_restore_cmd,
    build_snapshots_cmd,
    is_durable_mount,
    is_near_timeout,
    load_config_file,
    main,
    parse_snapshots,
    run_check_target,
    run_selfcheck,
    scrub_secrets,
    select_fresh_snapshot,
    sha256_file,
    snapshot_age_hours,
    snapshot_matches_root,
    timeout_budget,
    validate_config,
    verify_restored_file,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE = "tools.restore_canary.canary"


def good_raw(**over):
    raw = {
        "repo": "rest:https://nas.local/restic-canary",
        "host": "ace-ai",
        "backup_root_path": "/srv",
        "tag": "nightly-canary-job",
        "canary_file_path": "/srv/canary/sentinel.txt",
        "expected_sha256": "a" * 64,
        "max_age_hours": 24,
        "mount_type": "rest",
    }
    raw.update(over)
    return raw


def iso(now, hours_ago):
    return dt.datetime.fromtimestamp(now - hours_ago * 3600, dt.timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #
def test_valid_config_resolves():
    cfg = validate_config(good_raw())
    assert isinstance(cfg, CanaryConfig)
    assert cfg.backup_root_path == "/srv"
    assert cfg.canary_file_path == "/srv/canary/sentinel.txt"
    assert cfg.tag == "nightly-canary-job"


def test_tag_is_mandatory():
    with pytest.raises(ConfigError):
        validate_config(good_raw(tag=""))
    with pytest.raises(ConfigError):
        validate_config(good_raw(tag="   "))


def test_root_and_file_must_differ():
    with pytest.raises(ConfigError):
        validate_config(good_raw(backup_root_path="/srv/canary/sentinel.txt"))


def test_canary_file_must_live_under_root():
    with pytest.raises(ConfigError):
        validate_config(good_raw(canary_file_path="/other/sentinel.txt"))


def test_bad_sha_rejected():
    for bad in ("", "xyz", "A" * 64 + "0", "g" * 64):
        with pytest.raises(ConfigError):
            validate_config(good_raw(expected_sha256=bad))


def test_sha_normalised_lowercase():
    cfg = validate_config(good_raw(expected_sha256="A" * 64))
    assert cfg.expected_sha256 == "a" * 64


def test_max_age_ceiling_is_hard():
    # R-3: cannot raise the ceiling via config.
    with pytest.raises(ConfigError):
        validate_config(good_raw(max_age_hours=_MAX_AGE_HOURS + 1))
    with pytest.raises(ConfigError):
        validate_config(good_raw(max_age_hours=0))
    with pytest.raises(ConfigError):
        validate_config(good_raw(max_age_hours=-5))
    assert validate_config(good_raw(max_age_hours=_MAX_AGE_HOURS)).max_age_hours == _MAX_AGE_HOURS


def test_max_age_hours_constant_value():
    assert _MAX_AGE_HOURS == 168


def test_unknown_mount_rejected():
    with pytest.raises(ConfigError):
        validate_config(good_raw(mount_type="banana"))


def test_missing_fields_rejected():
    # max_age_hours is optional (defaults to the hard ceiling); the rest required.
    base = good_raw()
    for key in [k for k in base if k != "max_age_hours"]:
        broken = dict(base)
        broken.pop(key)
        with pytest.raises(ConfigError):
            validate_config(broken)


def test_max_age_hours_defaults_to_ceiling():
    raw = good_raw()
    raw.pop("max_age_hours")
    assert validate_config(raw).max_age_hours == _MAX_AGE_HOURS


def test_non_dict_config_rejected():
    with pytest.raises(ConfigError):
        validate_config(["not", "a", "dict"])


# --------------------------------------------------------------------------- #
# R-5 durability by mount type
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mount", DURABLE_MOUNT_TYPES)
def test_durable_mounts(mount):
    assert is_durable_mount(mount) is True


@pytest.mark.parametrize("mount", ["tmpfs", "local", "overlay", "ramfs"])
def test_local_mounts_not_durable(mount):
    assert is_durable_mount(mount) is False


# --------------------------------------------------------------------------- #
# restic command builders (B-1 / B-1r)
# --------------------------------------------------------------------------- #
def test_snapshots_cmd_uses_root_path_and_tag():
    cfg = validate_config(good_raw())
    cmd = build_snapshots_cmd(cfg)
    # B-1: --path is the ROOT, not the canary file.
    pi = cmd.index("--path")
    assert cmd[pi + 1] == "/srv"
    assert cfg.canary_file_path not in cmd
    # B-1r: --tag present.
    ti = cmd.index("--tag")
    assert cmd[ti + 1] == "nightly-canary-job"
    hi = cmd.index("--host")
    assert cmd[hi + 1] == "ace-ai"


def test_restore_cmd_uses_canary_file_include():
    cfg = validate_config(good_raw())
    cmd = build_restore_cmd(cfg, "deadbeef", "/tmp/restore-out")
    ii = cmd.index("--include")
    assert cmd[ii + 1] == "/srv/canary/sentinel.txt"
    assert "deadbeef" in cmd
    ti = cmd.index("--target")
    assert cmd[ti + 1] == "/tmp/restore-out"


def test_no_secret_in_any_argv():
    cfg = validate_config(good_raw())
    for cmd in (build_snapshots_cmd(cfg), build_restore_cmd(cfg, "x", "/t")):
        for tok in cmd:
            assert "OP_SERVICE_ACCOUNT_TOKEN" not in tok
            assert "RESTIC_PASSWORD" not in tok


# --------------------------------------------------------------------------- #
# Secret scoping (B-3 / Inv-1)
# --------------------------------------------------------------------------- #
def test_op_env_scoped_to_token_only():
    base = {"OP_SERVICE_ACCOUNT_TOKEN": "ops_tok", "PATH": "/usr/bin", "HOME": "/h"}
    env = build_op_env(base)
    assert env == {"OP_SERVICE_ACCOUNT_TOKEN": "ops_tok"}


def test_op_env_missing_token_raises():
    with pytest.raises(ConfigError):
        build_op_env({"PATH": "/usr/bin"})


def test_restic_env_never_carries_op_token():
    base = {"OP_SERVICE_ACCOUNT_TOKEN": "ops_tok", "PATH": "/usr/bin"}
    env = build_restic_env(base, "repo-pw")
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in env
    assert env["RESTIC_PASSWORD"] == "repo-pw"
    assert env["PATH"] == "/usr/bin"


def test_scrub_removes_known_secret_values():
    env = {"OP_SERVICE_ACCOUNT_TOKEN": "ops_supersecrettoken12345",
           "RESTIC_PASSWORD": "hunter2hunter2"}
    text = "token=ops_supersecrettoken12345 pw=hunter2hunter2 fine"
    out = scrub_secrets(text, env)
    assert "ops_supersecrettoken12345" not in out
    assert "hunter2hunter2" not in out
    assert "[REDACTED]" in out
    assert "fine" in out


def test_scrub_ignores_short_values():
    # A short/empty secret value must not scrub innocuous substrings.
    out = scrub_secrets("the cat sat", {"RESTIC_PASSWORD": "cat"})
    assert out == "the cat sat"


# --------------------------------------------------------------------------- #
# Snapshot matching (B-1 proof) + freshness (B-1r / R-3)
# --------------------------------------------------------------------------- #
def snaps_fixture(now):
    return [
        {"id": "fresh", "hostname": "ace-ai", "paths": ["/srv"],
         "tags": ["nightly-canary-job"], "time": iso(now, 1)},
        {"id": "wrongtag", "hostname": "ace-ai", "paths": ["/srv"],
         "tags": ["manual"], "time": iso(now, 1)},
        {"id": "stale", "hostname": "ace-ai", "paths": ["/srv"],
         "tags": ["nightly-canary-job"], "time": iso(now, 1000)},
        {"id": "wronghost", "hostname": "other", "paths": ["/srv"],
         "tags": ["nightly-canary-job"], "time": iso(now, 1)},
    ]


def test_parse_snapshots_array():
    assert parse_snapshots('[{"id": "a"}]') == [{"id": "a"}]


def test_parse_snapshots_rejects_non_array():
    with pytest.raises(ValueError):
        parse_snapshots('{"id": "a"}')


def test_b1_path_matches_root_not_file():
    now = 1_900_000_000.0
    cfg = validate_config(good_raw(max_age_hours=24))
    snaps = snaps_fixture(now)
    # The ROOT (/srv) matches the host+path+tag snapshots.
    matched = [s for s in snaps if snapshot_matches_root(s, cfg)]
    assert {s["id"] for s in matched} == {"fresh", "stale"}
    # B-1 KEY PROOF: filtering by the canary FILE path matches ZERO snapshots,
    # because restic --path matches the snapshot Directory, never contained files.
    file_cfg = cfg._replace(backup_root_path=cfg.canary_file_path)
    assert not any(snapshot_matches_root(s, file_cfg) for s in snaps)


def test_select_fresh_picks_newest_in_window():
    now = 1_900_000_000.0
    cfg = validate_config(good_raw(max_age_hours=24))
    chosen, reason = select_fresh_snapshot(snaps_fixture(now), cfg, now)
    assert reason is None
    assert chosen["id"] == "fresh"


def test_select_fresh_stale_only_fails_stale():
    now = 1_900_000_000.0
    cfg = validate_config(good_raw(max_age_hours=24))
    stale = [s for s in snaps_fixture(now) if s["id"] == "stale"]
    chosen, reason = select_fresh_snapshot(stale, cfg, now)
    assert chosen is None
    assert reason == FAIL_STALE


def test_select_fresh_no_match_fails_no_snapshot():
    now = 1_900_000_000.0
    cfg = validate_config(good_raw(max_age_hours=24))
    chosen, reason = select_fresh_snapshot([], cfg, now)
    assert chosen is None
    assert reason == FAIL_NO_SNAPSHOT


def test_select_fresh_only_wrong_tag_fails_no_snapshot():
    now = 1_900_000_000.0
    cfg = validate_config(good_raw(max_age_hours=24))
    wrong = [s for s in snaps_fixture(now) if s["id"] in ("wrongtag", "wronghost")]
    chosen, reason = select_fresh_snapshot(wrong, cfg, now)
    assert chosen is None
    assert reason == FAIL_NO_SNAPSHOT


def test_snapshot_age_hours():
    now = 1_900_000_000.0
    s = {"time": iso(now, 5)}
    age = snapshot_age_hours(s, now)
    assert abs(age - 5.0) < 0.01


def test_snapshot_age_untimed_none():
    assert snapshot_age_hours({}, 1_900_000_000.0) is None


# --------------------------------------------------------------------------- #
# Hashing + verdict
# --------------------------------------------------------------------------- #
def test_sha256_file(tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"hello")
    import hashlib
    assert sha256_file(f) == hashlib.sha256(b"hello").hexdigest()


def test_verify_pass(tmp_path):
    f = tmp_path / "c"
    f.write_bytes(b"data")
    sha = sha256_file(f)
    verdict, reason, actual = verify_restored_file(f, sha)
    assert verdict == PASS
    assert reason is None
    assert actual == sha


def test_verify_mismatch(tmp_path):
    f = tmp_path / "c"
    f.write_bytes(b"data")
    verdict, reason, actual = verify_restored_file(f, "0" * 64)
    assert verdict == FAIL
    assert reason == FAIL_HASH_MISMATCH
    assert actual == sha256_file(f)


def test_verify_missing(tmp_path):
    verdict, reason, actual = verify_restored_file(tmp_path / "nope", "0" * 64)
    assert verdict == FAIL
    assert reason == FAIL_RESTORE_EMPTY
    assert actual is None


def test_verify_empty_file(tmp_path):
    f = tmp_path / "e"
    f.write_bytes(b"")
    verdict, reason, _ = verify_restored_file(f, "0" * 64)
    assert verdict == FAIL
    assert reason == FAIL_RESTORE_EMPTY


# --------------------------------------------------------------------------- #
# Timeout budgets (R-4)
# --------------------------------------------------------------------------- #
def test_restore_budget_exceeds_cheap_calls():
    assert timeout_budget("restore") > timeout_budget("snapshots")
    assert timeout_budget("restore") > timeout_budget("ls")


def test_budget_is_baseline_times_multiplier():
    # restore baseline 30 * N(2) = 60.
    assert timeout_budget("restore") == 30.0 * TIMEOUT_MULTIPLIER


def test_unknown_call_class_raises():
    with pytest.raises(ValueError):
        timeout_budget("nope")


def test_near_timeout_threshold():
    b = timeout_budget("restore")
    assert is_near_timeout("restore", b * NEAR_TIMEOUT_RATIO) is True
    assert is_near_timeout("restore", b * 0.99) is True
    assert is_near_timeout("restore", b * 0.5) is False


# --------------------------------------------------------------------------- #
# Artifact + heartbeat (B-2 / B-3)
# --------------------------------------------------------------------------- #
def test_artifact_is_secret_free():
    cfg = validate_config(good_raw())
    art = build_artifact(cfg, PASS, None, "a" * 64, 12.0, 1_900_000_000.0, "deadbeef")
    leaky = {"OP_SERVICE_ACCOUNT_TOKEN": "ops_" + "z" * 40,
             "RESTIC_PASSWORD": "topsecretpassword"}
    text = artifact_to_json(art, leaky)
    for val in leaky.values():
        assert val not in text
    parsed = json.loads(text)
    assert parsed["verdict"] == PASS
    assert parsed["repo"] == cfg.repo  # the HANDLE, not a secret
    # No secret env KEY value leaks; the artifact carries no password field.
    assert "RESTIC_PASSWORD" not in parsed
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in parsed


def test_artifact_near_timeout_flag():
    cfg = validate_config(good_raw())
    over = build_artifact(cfg, PASS, None, "a" * 64,
                          timeout_budget("restore") * 0.9, 0.0, "id")
    assert over["near_timeout"] is True
    under = build_artifact(cfg, PASS, None, "a" * 64,
                           timeout_budget("restore") * 0.4, 0.0, "id")
    assert under["near_timeout"] is False


def test_heartbeat_is_secret_free_and_pushable():
    cfg = validate_config(good_raw())
    for verdict in (PASS, FAIL):
        hb = build_heartbeat(cfg, verdict, 1_900_000_000.0)
        assert hb["verdict"] == verdict
        assert hb["host"] == cfg.host
        assert hb["tag"] == cfg.tag
        text = json.dumps(hb)
        assert "RESTIC_PASSWORD" not in text
        assert "sha" not in text.lower()  # heartbeat carries liveness, not hashes


# --------------------------------------------------------------------------- #
# Artifact-integrity audit (B-2 demoted role)
# --------------------------------------------------------------------------- #
def test_audit_artifact_ok():
    cfg = validate_config(good_raw())
    now = 1_900_000_000.0
    art = build_artifact(cfg, PASS, None, "a" * 64, 10.0, now, "id")
    status, _ = audit_artifact(json.dumps(art), now, _MAX_AGE_HOURS)
    assert status == "OK"


def test_audit_artifact_fail_verdict():
    cfg = validate_config(good_raw())
    now = 1_900_000_000.0
    art = build_artifact(cfg, FAIL, FAIL_HASH_MISMATCH, None, 10.0, now, "id")
    status, _ = audit_artifact(json.dumps(art), now, _MAX_AGE_HOURS)
    assert status == "FAIL"


def test_audit_artifact_stale():
    cfg = validate_config(good_raw())
    gen = 1_900_000_000.0
    art = build_artifact(cfg, PASS, None, "a" * 64, 10.0, gen, "id")
    later = gen + (_MAX_AGE_HOURS + 5) * 3600
    status, _ = audit_artifact(json.dumps(art), later, _MAX_AGE_HOURS)
    assert status == "STALE"


def test_audit_artifact_unparseable():
    status, _ = audit_artifact("not json{", 0.0, _MAX_AGE_HOURS)
    assert status == "UNPARSEABLE"
    status2, _ = audit_artifact('{"no": "verdict"}', 0.0, _MAX_AGE_HOURS)
    assert status2 == "UNPARSEABLE"


# --------------------------------------------------------------------------- #
# --selfcheck (deploy health)
# --------------------------------------------------------------------------- #
def test_selfcheck_passes():
    assert run_selfcheck() == 0
    assert main(["--selfcheck"]) == 0


def test_selfcheck_touches_no_real_source(monkeypatch, tmp_path):
    # Even with a stray secret in env, selfcheck must not read real config.
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_irrelevant")
    monkeypatch.chdir(tmp_path)
    assert main(["--selfcheck"]) == 0


# --------------------------------------------------------------------------- #
# --check-target (real liveness; never silent 0)
# --------------------------------------------------------------------------- #
def write_config(tmp_path, **over):
    p = tmp_path / "canary.json"
    p.write_text(json.dumps(good_raw(**over)), encoding="utf-8")
    return p


def test_check_target_missing_config_fails(tmp_path):
    assert run_check_target(tmp_path / "nope.json") != 0


def test_check_target_empty_config_fails(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("   ", encoding="utf-8")
    assert run_check_target(p) != 0


def test_check_target_invalid_config_fails(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(good_raw(tag="")), encoding="utf-8")
    assert run_check_target(p) != 0


def test_check_target_missing_secret_env_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    p = write_config(tmp_path)
    assert run_check_target(p) != 0


def test_check_target_local_mount_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_tok")
    p = write_config(tmp_path, mount_type="tmpfs")
    assert run_check_target(p) != 0


def test_check_target_good_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_tok")
    p = write_config(tmp_path)
    assert run_check_target(p) == 0


def test_load_config_file(tmp_path):
    p = write_config(tmp_path)
    cfg = load_config_file(p)
    assert cfg.host == "ace-ai"


# --------------------------------------------------------------------------- #
# CLI dispatch (real argv: garbage flag MUST be non-zero)
# --------------------------------------------------------------------------- #
def test_garbage_flag_nonzero():
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--definitely-not-a-flag"])
    assert exc.value.code != 0


def test_bare_invocation_nonzero():
    # 'read nothing' must never be a silent exit 0.
    assert main([]) != 0


def test_check_target_without_config_fails():
    assert main(["--check-target"]) != 0


def test_audit_artifact_cli_ok(tmp_path):
    cfg = validate_config(good_raw())
    import time
    art = build_artifact(cfg, PASS, None, "a" * 64, 10.0, time.time(), "id")
    p = tmp_path / "art.json"
    p.write_text(json.dumps(art), encoding="utf-8")
    assert main(["--audit-artifact", str(p)]) == 0


def test_audit_artifact_cli_fail(tmp_path):
    cfg = validate_config(good_raw())
    import time
    art = build_artifact(cfg, FAIL, FAIL_STALE, None, 10.0, time.time(), "id")
    p = tmp_path / "art.json"
    p.write_text(json.dumps(art), encoding="utf-8")
    assert main(["--audit-artifact", str(p)]) != 0


# --------------------------------------------------------------------------- #
# Subprocess-level: real argv dispatch the deploy health check relies on
# --------------------------------------------------------------------------- #
def test_selfcheck_subprocess_exit0():
    r = subprocess.run([sys.executable, "-m", MODULE, "--selfcheck"],
                       cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_garbage_flag_subprocess_nonzero():
    r = subprocess.run([sys.executable, "-m", MODULE, "--bogus"],
                       cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert r.returncode != 0


def test_selfcheck_subprocess_no_secret_in_output():
    # Defence: even with a secret in the child env, selfcheck output is clean.
    import os
    env = dict(os.environ)
    env["OP_SERVICE_ACCOUNT_TOKEN"] = "ops_subprocsecretvalue999"
    r = subprocess.run([sys.executable, "-m", MODULE, "--selfcheck"],
                       cwd=str(REPO_ROOT), capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "ops_subprocsecretvalue999" not in (r.stdout + r.stderr)
