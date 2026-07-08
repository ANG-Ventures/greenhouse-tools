"""Offline tests for tools.fleet_doc_audit."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import tools.fleet_doc_audit as fda

PINNED = {"apollo", "aegis", "argus", "athena", "daedalus", "daedalus-opus", "momus"}


def live(keys: set[str] | None = None) -> dict:
    return fda.fixture_live(keys or PINNED)


def audit(text: str, live_doc: dict | None = None, exists=lambda _p: True) -> fda.AuditResult:
    return fda.audit_bytes(text.encode("utf-8"), live_doc or live(), exists=exists)


def statuses(result: fda.AuditResult) -> dict[str, fda.RuleResult]:
    return {row.rule: row for row in result.results}


def test_identity_key_join():
    assert fda.identity_key("default") == "apollo"
    assert fda.identity_key("") == "apollo"
    assert fda.identity_key("gateway-default", "gateway") == "apollo"
    assert fda.identity_key("gateway-aegis", "gateway") == "aegis"
    assert fda.identity_key("Daedalus") == "daedalus"
    assert fda.identity_key("Daedalus-Opus") == "daedalus-opus"
    assert fda.identity_key("daedalus") != fda.identity_key("daedalus-opus")


def test_running_identities_join_pins_fixture_set_without_ellipsis():
    assert fda.running_identities_join(live()) == PINNED
    assert "…" not in PINNED
    assert len(PINNED) == 7


def test_rlcm_stale_on_underlist():
    result = fda.probe_rlcm(fda.stale_doc(), PINNED)
    assert result.status == "STALE"
    assert result.missing == frozenset({"argus", "athena", "daedalus", "daedalus-opus", "momus"})
    assert result.extra == frozenset()


def test_rlcm_ok_on_exact_set():
    result = fda.probe_rlcm(fda.corrected_doc(), PINNED)
    assert result.status == "OK"
    assert result.missing == frozenset()
    assert result.extra == frozenset()


def test_rlcm_stale_on_overlist():
    doc = fda.corrected_doc().replace(", momus", ", ghost")
    result = fda.probe_rlcm(doc, {"apollo", "aegis", "argus", "athena", "daedalus", "daedalus-opus"})
    assert result.status == "STALE"
    assert result.missing == frozenset()
    assert result.extra == frozenset({"ghost"})


def test_rlcm_stale_on_empty_fence():
    doc = fda.stale_doc().replace("apollo, aegis\n```", "   \n```")
    result = fda.probe_rlcm(doc, {"apollo"})
    assert result.status == "STALE"
    assert result.missing == frozenset({"apollo"})


def test_rlcm_locator_missing_on_absent_fence():
    doc = fda.stale_doc().replace("```lcm-agents\napollo, aegis\n```\n\n", "")
    result = fda.probe_rlcm(doc, PINNED)
    assert result.status == "LOCATOR_MISSING"


def test_prose_has_no_scope_enumeration():
    doc = fda.corrected_doc().replace("on the agents listed below", "on Apollo and Aegis, listed below")
    result = fda.probe_rlcm(doc, PINNED)
    assert result.status == "STALE"
    assert result.extra == frozenset({"apollo", "aegis"})
    assert audit(doc).exit_code == fda.EXIT_DRIFT


def test_neutering_scope_set_fails(monkeypatch):
    assert fda.probe_rlcm(fda.stale_doc(), PINNED).status == "STALE"
    monkeypatch.setattr(fda, "parse_lcm_agents", lambda _body: set(PINNED))
    assert fda.probe_rlcm(fda.stale_doc(), PINNED).status == "OK"


def test_neutering_prose_guard_fails(monkeypatch):
    doc = fda.corrected_doc().replace("on the agents listed below", "on Apollo and Aegis, listed below")
    assert fda.probe_rlcm(doc, PINNED).status == "STALE"
    monkeypatch.setattr(fda, "prose_scope_tokens", lambda _doc, _known=None: set())
    assert fda.probe_rlcm(doc, PINNED).status == "OK"


def test_rlink_dead_link():
    result = fda.probe_rlink(fda.stale_doc(), exists=lambda path: False)
    assert result.status == "STALE"
    assert "~/.hermes/plans/2026-06-05_apollo-orchestrator-aegis-breakglass-spec.md" in result.message


def test_neutering_rlink_fails():
    assert fda.probe_rlink(fda.stale_doc(), exists=lambda _path: False).status == "STALE"
    assert fda.probe_rlink(fda.stale_doc(), exists=lambda _path: True).status == "OK"


def test_rstamp_stale_when_body_changed():
    doc = fda.corrected_doc(with_digest=True) + "New certified-breaking body line.\n"
    result = fda.probe_stamp(doc.encode("utf-8"))
    assert result.status == "STALE"


def test_rstamp_ok_when_body_matches(tmp_path):
    doc = fda.corrected_doc(with_digest=True)
    path = tmp_path / "Agents.md"
    path.write_text(doc, encoding="utf-8")
    path.touch()
    assert fda.probe_stamp(path.read_bytes()).status == "OK"


def test_rstamp_warn_when_digest_absent():
    result = fda.probe_stamp(fda.stale_doc().encode("utf-8"))
    assert result.status == "WARN"
    assert "run --emit-stamp" in result.message


def test_neutering_rstamp_fails(monkeypatch):
    doc = fda.corrected_doc(with_digest=True) + "changed\n"
    assert fda.probe_stamp(doc.encode("utf-8")).status == "STALE"
    expected = fda.frontmatter_value(doc, "verified_body_sha256")
    monkeypatch.setattr(fda, "body_digest", lambda _doc_bytes: expected)
    assert fda.probe_stamp(doc.encode("utf-8")).status == "OK"


def test_normalize_body_absent_frontmatter():
    raw = b"No frontmatter\r\nbody line   \r\n\r\n"
    assert fda.split_frontmatter(raw) == (b"", raw)
    assert fda.normalize_body(raw) == b"No frontmatter\nbody line\n"
    assert fda.body_digest(raw) == fda.body_digest(b"No frontmatter\nbody line\n")


def test_normalize_body_is_per_line_rstrip():
    a = b"---\nlast_verified: 2026-07-08\n---\nA   \r\nB\t\r\n\r\n"
    b = b"---\nlast_verified: 2026-07-08\n---\nA\nB\n"
    assert fda.normalize_body(a) == fda.normalize_body(b)
    assert fda.body_digest(a) == fda.body_digest(b)


def test_emit_stamp_roundtrip():
    doc = fda.corrected_doc(with_digest=False)
    line = fda.emit_stamp(doc.encode("utf-8"))
    assert re.fullmatch(r"verified_body_sha256: [0-9a-f]{64}\n", line)
    stamped = doc.replace("last_verified: 2026-07-08\n", "last_verified: 2026-07-08\n" + line)
    assert fda.probe_stamp(stamped.encode("utf-8")).status == "OK"


def test_emit_stamp_prints_only_digest():
    line = fda.emit_stamp(fda.corrected_doc().encode("utf-8"))
    assert line.startswith("verified_body_sha256: ")
    assert line.count("\n") == 1
    assert "Context engine" not in line
    assert "Aegis" not in line


def test_check_target_ignores_staleness():
    result = fda.check_target_bytes(fda.stale_doc().encode("utf-8"))
    assert result.exit_code == fda.EXIT_OK
    assert {row.status for row in result.results} == {"OK"}


def test_real_slice_detects_three_known_drifts():
    result = audit(fda.stale_doc(), exists=lambda _path: False)
    rows = statuses(result)
    assert result.exit_code == fda.EXIT_DRIFT
    assert rows["R-LCM"].status == "STALE"
    assert rows["R-LINK"].status == "STALE"
    assert rows["R-STAMP"].status == "WARN"
    assert rows["R-AEGIS"].status == "OK"


def test_corrected_doc_all_ok_and_check_target_green():
    doc = fda.corrected_doc(with_digest=True)
    result = audit(doc, exists=lambda _path: True)
    assert result.exit_code == fda.EXIT_OK
    assert {row.status for row in result.results} == {"OK"}
    assert fda.check_target_bytes(doc.encode("utf-8")).exit_code == fda.EXIT_OK


def test_check_target_reports_missing_fence_pre_seed():
    doc = fda.stale_doc().replace("```lcm-agents\napollo, aegis\n```\n\n", "")
    result = fda.check_target_bytes(doc.encode("utf-8"))
    assert result.exit_code == fda.EXIT_DRIFT
    assert statuses(result)["R-LCM"].status == "LOCATOR_MISSING"


def test_cli_selfcheck_and_unknown_flag_dispatch():
    assert fda.main(["--selfcheck"]) == fda.EXIT_OK
    with pytest.raises(SystemExit) as exc:
        fda.main(["--definitely-unknown"])
    assert exc.value.code != 0


def test_cli_check_target_and_emit_stamp(tmp_path, capsys):
    path = tmp_path / "Agents.md"
    path.write_text(fda.stale_doc(), encoding="utf-8")
    assert fda.main(["--check-target", str(path)]) == fda.EXIT_OK
    assert fda.main(["--emit-stamp", str(path)]) == fda.EXIT_OK
    out = capsys.readouterr().out
    assert "verified_body_sha256:" in out


def test_no_write_api_in_tool_hot_path():
    tree = ast.parse(Path(fda.__file__).read_text(encoding="utf-8"))
    forbidden_attrs = {"write_text", "write_bytes"}
    found = [node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr in forbidden_attrs]
    assert found == []
