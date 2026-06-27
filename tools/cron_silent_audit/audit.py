#!/usr/bin/env python3
"""cron-silent-audit — read-only auditor for silently-failing fleet crons.

Scans $HERMES_HOME/cron/jobs.json; flags enabled jobs delivered local/nowhere
with no alert token. Classifies, never mutates. Stdlib only. See REVERSIBILITY.md.
--selfcheck = offline logic probe (health). --check-target = real-registry liveness.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Any, NamedTuple

DEFAULT_ALERT_TOKENS = ("notify.py", "#alerts", "#logs")
LOCAL_DELIVER = ("local", "none", "", None)

SILENT_SCRIPT_MISSING = "SILENT_SCRIPT_MISSING"
SILENT_NO_ALERT_SCRIPT = "SILENT_NO_ALERT_SCRIPT"
SILENT_NO_ALERT_PROMPT = "SILENT_NO_ALERT_PROMPT"
SILENT_EMPTY_BODY = "SILENT_EMPTY_BODY"
OK_HAS_ALERT = "OK_HAS_ALERT"
OK_DELIVERS_REMOTE = "OK_DELIVERS_REMOTE"

# Lowest rank = most urgent; drives stable output ordering.
VERDICT_RANK = {
    SILENT_SCRIPT_MISSING: 0,
    SILENT_NO_ALERT_SCRIPT: 1,
    SILENT_NO_ALERT_PROMPT: 2,
    SILENT_EMPTY_BODY: 3,
    OK_HAS_ALERT: 4,
    OK_DELIVERS_REMOTE: 5,
}
FLAGGED = {SILENT_SCRIPT_MISSING, SILENT_NO_ALERT_SCRIPT,
           SILENT_NO_ALERT_PROMPT, SILENT_EMPTY_BODY}


class Verdict(NamedTuple):
    name: str
    verdict: str
    reason: str

    @property
    def rank(self) -> int:
        return VERDICT_RANK[self.verdict]

    @property
    def flagged(self) -> bool:
        return self.verdict in FLAGGED


def hermes_home() -> pathlib.Path:
    env = os.environ.get("HERMES_HOME")
    return pathlib.Path(env) if env else pathlib.Path.home() / ".hermes"


def default_registry() -> pathlib.Path:
    return hermes_home() / "cron" / "jobs.json"


def default_scripts_dir(registry: pathlib.Path) -> pathlib.Path:
    return registry.parent / "scripts"


def _has_alert(text: str, tokens) -> bool:
    return any(tok in text for tok in tokens)


def classify_job(job, scripts_dir, tokens=DEFAULT_ALERT_TOKENS):
    """Verdict for an ENABLED job, or None if disabled. May read a script body."""
    if not job.get("enabled"):
        return None
    name = str(job.get("name", "<unnamed>"))
    deliver = job.get("deliver")
    if deliver not in LOCAL_DELIVER:
        return Verdict(name, OK_DELIVERS_REMOTE, "delivers to %r" % (deliver,))

    script = job.get("script")
    prompt = job.get("prompt") or ""
    if script:
        sp = scripts_dir / pathlib.Path(str(script)).name
        if not sp.exists():
            return Verdict(name, SILENT_SCRIPT_MISSING,
                           "script %r referenced but absent from %s" % (script, scripts_dir))
        try:
            body = sp.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover
            return Verdict(name, SILENT_SCRIPT_MISSING, "script %r unreadable: %s" % (script, exc))
        if _has_alert(body, tokens):
            return Verdict(name, OK_HAS_ALERT, "alert token in %s" % script)
        return Verdict(name, SILENT_NO_ALERT_SCRIPT,
                       "script %s has no alert token %s" % (script, list(tokens)))

    if prompt.strip():
        if _has_alert(prompt, tokens):
            return Verdict(name, OK_HAS_ALERT, "alert token in prompt")
        return Verdict(name, SILENT_NO_ALERT_PROMPT,
                       "prompt has no alert token %s" % (list(tokens),))
    return Verdict(name, SILENT_EMPTY_BODY, "no script and no non-empty prompt")


def parse_registry(raw):
    """Validate shape and return the jobs list. A bare list is a failure, not coercion."""
    if not isinstance(raw, dict):
        raise ValueError("registry must be a dict with a 'jobs' list, got %s" % type(raw).__name__)
    jobs = raw.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("registry 'jobs' must be a list")
    return jobs


def audit(jobs, scripts_dir, tokens=DEFAULT_ALERT_TOKENS, include_all=False):
    """Classify every enabled job; return verdicts sorted (rank, name)."""
    out = []
    for job in jobs:
        v = classify_job(job, scripts_dir, tokens)
        if v is None:
            continue
        if include_all or v.flagged:
            out.append(v)
    out.sort(key=lambda v: (v.rank, v.name))
    return out


def _selfcheck_fixture():
    return [
        {"name": "z-missing", "enabled": True, "deliver": "local",
         "script": "does-not-exist.sh", "prompt": None, "no_agent": True},
        {"name": "y-script-noalert", "enabled": True, "deliver": "local",
         "script": "present_noalert.sh", "prompt": None, "no_agent": True},
        {"name": "x-script-alert", "enabled": True, "deliver": "local",
         "script": "present_alert.sh", "prompt": None, "no_agent": True},
        {"name": "w-prompt-noalert", "enabled": True, "deliver": "none",
         "script": None, "prompt": "just do the thing", "no_agent": False},
        {"name": "v-prompt-alert", "enabled": True, "deliver": "",
         "script": None, "prompt": "on failure post to #alerts", "no_agent": False},
        {"name": "u-empty", "enabled": True, "deliver": None,
         "script": None, "prompt": "   ", "no_agent": False},
        {"name": "t-remote", "enabled": True, "deliver": "discord:123",
         "script": None, "prompt": "no alert here", "no_agent": False},
        {"name": "s-disabled", "enabled": False, "deliver": "local",
         "script": None, "prompt": "", "no_agent": True},
    ]


def run_selfcheck():
    """Offline logic probe. Builds its own fixture; touches NO real source."""
    import tempfile
    expected = {
        "z-missing": SILENT_SCRIPT_MISSING,
        "y-script-noalert": SILENT_NO_ALERT_SCRIPT,
        "x-script-alert": OK_HAS_ALERT,
        "w-prompt-noalert": SILENT_NO_ALERT_PROMPT,
        "v-prompt-alert": OK_HAS_ALERT,
        "u-empty": SILENT_EMPTY_BODY,
        "t-remote": OK_DELIVERS_REMOTE,
    }
    with tempfile.TemporaryDirectory() as td:
        sd = pathlib.Path(td)
        (sd / "present_noalert.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        (sd / "present_alert.sh").write_text("#!/bin/sh\npython notify.py 'boom'\n", encoding="utf-8")
        verdicts = audit(_selfcheck_fixture(), sd, include_all=True)
    got = {v.name: v.verdict for v in verdicts}
    if "s-disabled" in got:
        print("SELFCHECK FAIL: disabled job leaked into verdicts", file=sys.stderr)
        return 1
    if got != expected:
        print("SELFCHECK FAIL: verdict mismatch", file=sys.stderr)
        for n in sorted(set(expected) | set(got)):
            e, g = expected.get(n), got.get(n)
            print("  %s: expected=%s got=%s%s" % (n, e, g, "" if e == g else "  <-- MISMATCH"), file=sys.stderr)
        return 1
    flagged = [v for v in verdicts if v.flagged]
    if flagged != sorted(flagged, key=lambda v: (v.rank, v.name)):
        print("SELFCHECK FAIL: output not stably ordered", file=sys.stderr)
        return 1
    print("SELFCHECK OK: %d synthetic verdicts matched expectations" % len(verdicts))
    return 0


def run_check_target(registry):
    """Real-target liveness. Loud non-zero if the source is unusable."""
    if not registry.exists():
        print("CHECK-TARGET FAIL: registry not found: %s" % registry, file=sys.stderr)
        return 2
    try:
        raw = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print("CHECK-TARGET FAIL: cannot parse %s: %s" % (registry, exc), file=sys.stderr)
        return 2
    try:
        jobs = parse_registry(raw)
    except ValueError as exc:
        print("CHECK-TARGET FAIL: bad registry shape: %s" % exc, file=sys.stderr)
        return 2
    if not jobs:
        print("CHECK-TARGET FAIL: registry has zero jobs: %s" % registry, file=sys.stderr)
        return 2
    enabled = sum(1 for j in jobs if j.get("enabled"))
    print("CHECK-TARGET OK: %s parseable, %d jobs, %d enabled" % (registry, len(jobs), enabled))
    return 0


def run_audit(registry, scripts_dir, include_all, tokens):
    if not registry.exists():
        print("AUDIT FAIL: registry not found: %s (run --check-target for liveness)" % registry, file=sys.stderr)
        return 2
    try:
        jobs = parse_registry(json.loads(registry.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print("AUDIT FAIL: %s" % exc, file=sys.stderr)
        return 2
    verdicts = audit(jobs, scripts_dir, tokens, include_all=include_all)
    flagged = [v for v in verdicts if v.flagged]
    header = "ALL ENABLED JOBS" if include_all else "SILENT-FAILURE CANDIDATES"
    print("== %s (%d flagged) ==" % (header, len(flagged)))
    for v in verdicts:
        print("[%s] %s: %s" % (v.verdict, v.name, v.reason))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="cron-silent-audit",
                                description="Read-only auditor for silently-failing fleet crons.")
    p.add_argument("--registry", type=pathlib.Path, default=None,
                   help="path to jobs.json (default: $HERMES_HOME/cron/jobs.json)")
    p.add_argument("--scripts-dir", type=pathlib.Path, default=None,
                   help="cron scripts dir (default: sibling scripts/)")
    p.add_argument("--alert-tokens", default=",".join(DEFAULT_ALERT_TOKENS),
                   help="comma-separated alert tokens")
    p.add_argument("--all", action="store_true",
                   help="list every enabled job with its verdict, not just flagged")
    p.add_argument("--selfcheck", action="store_true",
                   help="offline logic probe over a synthetic fixture (health check)")
    p.add_argument("--check-target", action="store_true",
                   help="assert the real registry exists/right-kind/non-empty")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.selfcheck:
        return run_selfcheck()
    registry = args.registry or default_registry()
    scripts_dir = args.scripts_dir or default_scripts_dir(registry)
    tokens = tuple(t for t in (s.strip() for s in args.alert_tokens.split(",")) if t)
    if args.check_target:
        return run_check_target(registry)
    return run_audit(registry, scripts_dir, args.all, tokens)


if __name__ == "__main__":
    raise SystemExit(main())
