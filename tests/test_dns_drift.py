from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import sys

import pytest

from tools.dns_drift import agh, frontdoors
from tools.dns_drift.diff import DriftItem, exit_code_for, reconcile
from tools.dns_drift.drift import main, render

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "dns_drift"
MODULE = "tools.dns_drift.drift"


def state() -> dict[str, dict[str, object]]:
    return {
        "192.168.1.216": {"id": "fd_216", "readable": True, "deferred": False},
        "192.168.1.4": {"id": "fd_4", "readable": False, "deferred": True},
        "192.168.1.5": {"id": "fd_5", "readable": False, "deferred": True},
    }


def test_diff_classifies_all_core_classes_and_sub_reasons():
    expected = {
        "ambiguous.ace": ["fd_216", "fd_4"],
        "disabled.ace": ["fd_216"],
        "missing.ace": ["fd_216"],
        "mispoint.ace": ["fd_216"],
        "ok.ace": ["fd_216"],
        "unconfirmed-mispoint.ace": ["fd_5"],
        "site.docs.ace": ["fd_216"],
    }
    live = {
        "disabled.ace": ("192.168.1.216", False),
        "mispoint.ace": ("192.168.1.4", True),
        "ok.ace": ("192.168.1.216", True),
        "unconfirmed-mispoint.ace": ("192.168.1.216", True),
        "orphan.ace": ("192.168.1.216", True),
        "deferred-live.ace": ("192.168.1.4", True),
        "outside.ace": ("192.168.1.99", True),
        "*.docs.ace": ("192.168.1.216", True),
        "*.empty.ace": ("192.168.1.216", True),
    }

    items = reconcile(expected, live, state(), mispoint_confirmed={"fd_216"})
    got = {(item.klass, item.name, item.sub_reason) for item in items}

    assert ("ambiguous", "ambiguous.ace", None) in got
    assert ("MISSING", "disabled.ace", "disabled") in got
    assert ("MISSING", "missing.ace", "absent") in got
    assert ("MISPOINTED", "mispoint.ace", None) in got
    assert ("ORPHAN", "orphan.ace", None) in got
    assert ("unknown_source", "deferred-live.ace", "deferred_source") in got
    assert ("unknown_source", "outside.ace", "out_of_model") in got
    assert ("unknown_shape", "*.empty.ace", "wildcard_unmatched") in got
    assert not any(item.name == "unconfirmed-mispoint.ace" and item.klass == "MISPOINTED" for item in items)
    assert not any(item.name == "*.docs.ace" for item in items)


def test_deferred_source_does_not_escalate_exit_code_bnew1():
    items = [
        DriftItem("unknown_source", "bulk-at-deferred.ace", "deferred_source"),
        DriftItem("MISSING", "real-missing-on-readable.ace", "absent"),
    ]

    assert exit_code_for(items) == 2
    assert exit_code_for([items[0]]) == 0


def test_out_of_model_unknown_source_is_low_severity_exit_one():
    assert exit_code_for([DriftItem("unknown_source", "outside.ace", "out_of_model")]) == 1


def test_agh_parser_reads_rewrite_fixture_with_wildcard():
    live = agh.parse_rewrite_list((FIXTURES / "agh_rewrite_list.json").read_text(encoding="utf-8"))

    assert live["alpha.ace"] == ("192.168.1.216", True)
    assert live["disabled.ace"] == ("192.168.1.216", False)
    assert live["*.docs.ace"] == ("192.168.1.216", True)


def test_frontdoor_parsers_and_build_shape_use_real_defaults():
    names, unknown = frontdoors.parse_services_yaml((FIXTURES / "services.yaml").read_text(encoding="utf-8"))
    hosts, host_unknown = frontdoors.parse_caddyfile_hosts((FIXTURES / "Caddyfile").read_text(encoding="utf-8"))

    assert names == ["alpha.ace", "missing.ace", "disabled.ace", "site.docs.ace"]
    assert unknown == []
    assert {"ha.ace", "portal.ace", "skills.ace"}.issubset(set(hosts))
    assert host_unknown == []
    assert frontdoors.DEFAULT_FRONTDOOR_SOURCES["fd_216"].ip == "192.168.1.216"
    assert str(frontdoors.DEFAULT_FRONTDOOR_SOURCES["fd_216"].path).endswith("stacks/lan-proxy/services.yaml")


def test_e2e_fixture_report_counts_are_hand_verified(capsys):
    rc = main([
        "--agh-json", str(FIXTURES / "agh_rewrite_list.json"),
        "--source", f"fd_216={FIXTURES / 'services.yaml'}",
        "--floor-ratio", "0",
        "--lkg-name-count", "1",
    ])
    out = capsys.readouterr().out

    assert rc == 2
    assert "MISSING:disabled disabled.ace" in out
    assert "MISSING:absent missing.ace" in out
    assert "ORPHAN stray.ace" in out
    assert "unknown_source:deferred_source deferred-live.ace" in out
    assert "unknown_source:out_of_model outside.ace" in out
    assert "*.docs.ace" not in out
    assert "site.docs.ace" not in out


def test_check_target_partial_deferred_passes_not_exit_three(capsys):
    rc = main([
        "--check-target",
        "--agh-json", str(FIXTURES / "agh_rewrite_list.json"),
        "--source", f"fd_216={FIXTURES / 'services.yaml'}",
        "--floor-ratio", "0",
        "--lkg-name-count", "1",
    ])
    captured = capsys.readouterr()

    assert rc == 0
    assert "DNS_DRIFT_COVERAGE_NOTE:" in captured.out
    assert "DNS_DRIFT_LIVENESS_PASS:" in captured.out
    assert captured.err == ""


def test_check_target_empty_agh_fails_loud(capsys):
    rc = main([
        "--check-target",
        "--agh-json", str(FIXTURES / "agh_empty.json"),
        "--source", f"fd_216={FIXTURES / 'services.yaml'}",
        "--floor-ratio", "0.85",
        "--lkg-name-count", "89",
    ])
    captured = capsys.readouterr()

    assert rc == 3
    assert "DNS_DRIFT_LIVENESS_FAIL:" in captured.err


def test_all_sources_unreadable_is_total_cant_measure(capsys):
    rc = main([
        "--agh-json", str(FIXTURES / "agh_rewrite_list.json"),
        "--defer", "fd_216",
        "--defer", "fd_4",
        "--defer", "fd_5",
        "--defer", "fd_18",
        "--floor-ratio", "0",
        "--lkg-name-count", "1",
    ])
    captured = capsys.readouterr()

    assert rc == 3
    assert "DNS_DRIFT_CANT_MEASURE: zero readable" in captured.err


def test_selfcheck_cli_exits_zero_offline():
    proc = subprocess.run(
        [sys.executable, "-m", MODULE, "--selfcheck"],
        cwd=ROOT,
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
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode != 0
    assert "unrecognized arguments" in proc.stderr


def test_json_output_is_parseable_private_surface(capsys):
    rc = main([
        "--json",
        "--agh-json", str(FIXTURES / "agh_rewrite_list.json"),
        "--source", f"fd_216={FIXTURES / 'services.yaml'}",
        "--floor-ratio", "0",
        "--lkg-name-count", "1",
    ])
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert rc == 2
    assert payload["exit_code"] == 2
    assert payload["frontdoor_state"]["192.168.1.4"]["deferred"] is True


def test_render_is_deterministic_same_input():
    items = reconcile({"b.ace": ["fd_216"], "a.ace": ["fd_216"]}, {}, state(), mispoint_confirmed={"fd_216"})

    assert render(items, state(), live_count=0, floor=0) == render(items, state(), live_count=0, floor=0)


def test_read_only_allowlist_spy_for_remote_modes(monkeypatch, capsys):
    captured: list[tuple[str, ...]] = []

    def fake_run(remote_argv: list[str]) -> str:
        captured.append(tuple(remote_argv))
        return '[{"domain":"alpha.ace","answer":"192.168.1.216","enabled":true}]'

    def fake_build(**kwargs):
        return {"alpha.ace": ["fd_216"]}, {"192.168.1.216": {"id": "fd_216", "readable": True, "deferred": False}}, []

    monkeypatch.setattr(agh, "_run_remote", fake_run)
    monkeypatch.setattr(frontdoors, "build", fake_build)

    assert main(["--floor-ratio", "0", "--lkg-name-count", "1"]) == 0
    assert main(["--json", "--floor-ratio", "0", "--lkg-name-count", "1"]) == 0
    assert main(["--check-target", "--floor-ratio", "0", "--lkg-name-count", "1"]) == 0
    capsys.readouterr()
    assert set(captured) == {tuple(agh.REWRITE_LIST_CMD)}


def test_structural_subprocess_call_site_is_agh_run_remote_only():
    call_sites: list[tuple[str, str]] = []
    for path in (ROOT / "tools" / "dns_drift").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess":
                    parent = parents.get(node)
                    while parent is not None and not isinstance(parent, ast.FunctionDef):
                        parent = parents.get(parent)
                    call_sites.append((path.name, parent.name if isinstance(parent, ast.FunctionDef) else "<module>"))

    assert call_sites == [("agh.py", "_run_remote")]


def test_tool_imports_are_stdlib_or_repo_only():
    allowed_external = {"__future__", "argparse", "dataclasses", "json", "os", "pathlib", "re", "subprocess", "sys", "typing"}
    for path in (ROOT / "tools" / "dns_drift").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".", 1)[0] in allowed_external
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.level == 0:
                    assert node.module.split(".", 1)[0] in allowed_external
