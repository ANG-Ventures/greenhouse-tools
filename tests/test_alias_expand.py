"""Tests for tools.alias_expand — collect & pass offline, stdlib only.

Covers the pure grammar expander (alternation, optional, nesting, dedup,
determinism, byte-identical passthrough), the minimal capability-YAML reader,
and the two-probe contract (--selfcheck offline logic probe vs --check-target
real-source liveness gate, including the anti-fake-green "read nothing ->
loud non-zero" trap).

No network, no third-party imports, no real HACR repo access.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tools import alias_expand as ax


# --- pure expander: alternation / optional / nesting ------------------------
def test_no_token_literal_passes_through_byte_identical():
    assert ax.expand_alias("turn on the lights") == ["turn on the lights"]
    # has_grammar agrees it is a plain literal.
    assert ax.has_grammar("turn on the lights") is False


def test_simple_alternation():
    assert ax.expand_alias("turn (off|out) the lights") == [
        "turn off the lights",
        "turn out the lights",
    ]


def test_optional_present_then_absent():
    assert ax.expand_alias("turn off [the] lights") == [
        "turn off the lights",
        "turn off lights",
    ]


def test_alternation_and_optional_crossproduct_order():
    assert ax.expand_alias("turn (off|out) [the] lights") == [
        "turn off the lights",
        "turn off lights",
        "turn out the lights",
        "turn out lights",
    ]


def test_double_optional_the_asymmetry_case():
    # The exact phrasing gap Ace hand-patches: "turn off all the lights".
    assert ax.expand_alias("turn off [all] [the] lights") == [
        "turn off all the lights",
        "turn off all lights",
        "turn off the lights",
        "turn off lights",
    ]


def test_multiple_alternations_multiply_in_source_order():
    assert ax.expand_alias("(turn|switch) (on|off)") == [
        "turn on",
        "turn off",
        "switch on",
        "switch off",
    ]


def test_nested_group_inside_optional():
    assert ax.expand_alias("dim [the (kitchen|hall)] lights") == [
        "dim the kitchen lights",
        "dim the hall lights",
        "dim lights",
    ]


def test_whitespace_is_collapsed_and_stripped():
    # A dropped optional must not leave a double space or leading/trailing gap.
    out = ax.expand_alias("turn [really] on")
    assert out == ["turn really on", "turn on"]
    assert all("  " not in s for s in out)
    assert all(s == s.strip() for s in out)


def test_expand_alias_dedupes_within_one_alias():
    # (a|a) collapses to a single literal.
    assert ax.expand_alias("turn (on|on) lights") == ["turn on lights"]


def test_expand_is_deterministic():
    pat = "turn (off|out) [the] lights"
    first = ax.expand_alias(pat)
    for _ in range(5):
        assert ax.expand_alias(pat) == first


# --- expand_aliases: cross-list dedup, order-preserving ---------------------
def test_expand_aliases_dedupes_across_the_list():
    merged = ax.expand_aliases(["turn (on|off) lights", "turn on lights"])
    assert merged == ["turn on lights", "turn off lights"]


def test_expand_aliases_flattens_and_preserves_order():
    merged = ax.expand_aliases(["good night", "turn (on|off) lamp"])
    assert merged == ["good night", "turn on lamp", "turn off lamp"]


# --- malformed grammar raises ----------------------------------------------
def test_unbalanced_open_paren_raises():
    try:
        ax.expand_alias("turn (on lights")
        assert False, "expected AliasSyntaxError"
    except ax.AliasSyntaxError:
        pass


def test_unbalanced_close_paren_raises():
    try:
        ax.expand_alias("turn on) lights")
        assert False, "expected AliasSyntaxError"
    except ax.AliasSyntaxError:
        pass


def test_unbalanced_open_bracket_raises():
    try:
        ax.expand_alias("turn [on lights")
        assert False, "expected AliasSyntaxError"
    except ax.AliasSyntaxError:
        pass


def test_pipe_outside_group_raises():
    try:
        ax.expand_alias("turn on|off")
        assert False, "expected AliasSyntaxError"
    except ax.AliasSyntaxError:
        pass


# --- minimal capability-YAML reader ----------------------------------------
_SAMPLE_YAML = """\
# living room capability file
- name: living_room_lights
  aliases:
    - turn on the lights
    - "turn (off|out) [the] lights"
- name: bedroom_lights
  aliases:
    - good night
"""


def test_parse_capability_yaml_reads_names_and_aliases():
    caps = ax.parse_capability_yaml(_SAMPLE_YAML)
    assert [c["name"] for c in caps] == ["living_room_lights", "bedroom_lights"]
    assert caps[0]["aliases"] == [
        "turn on the lights",
        "turn (off|out) [the] lights",
    ]
    assert caps[1]["aliases"] == ["good night"]


def test_parse_capability_yaml_strips_inline_comments():
    text = "- name: x  # a comment\n  aliases:\n    - hello  # trailing\n"
    caps = ax.parse_capability_yaml(text)
    assert caps == [{"name": "x", "aliases": ["hello"]}]


def test_parse_capability_yaml_ignores_blank_and_empty():
    assert ax.parse_capability_yaml("\n\n   \n") == []


def test_load_and_expand_end_to_end(tmp_path):
    (tmp_path / "living.yaml").write_text(_SAMPLE_YAML, encoding="utf-8")
    caps = ax.load_capabilities(tmp_path)
    names = {c["name"] for c in caps}
    assert names == {"living_room_lights", "bedroom_lights"}
    living = next(c for c in caps if c["name"] == "living_room_lights")
    assert living["file"] == "living.yaml"
    expanded = ax.expand_aliases(living["aliases"])
    assert expanded == [
        "turn on the lights",
        "turn off the lights",
        "turn off lights",
        "turn out the lights",
        "turn out lights",
    ]


def test_non_capability_files_are_excluded(tmp_path):
    (tmp_path / "living.yaml").write_text(_SAMPLE_YAML, encoding="utf-8")
    # These must be ignored even though they end in .yaml.
    (tmp_path / "policy.yaml").write_text("- name: nope\n  aliases:\n    - x\n", encoding="utf-8")
    (tmp_path / "groups.yaml").write_text("- name: grp\n  aliases:\n    - y\n", encoding="utf-8")
    (tmp_path / "llm_providers.yaml").write_text("- name: llm\n  aliases:\n    - z\n", encoding="utf-8")
    caps = ax.load_capabilities(tmp_path)
    assert {c["name"] for c in caps} == {"living_room_lights", "bedroom_lights"}


# --- --selfcheck: offline logic probe --------------------------------------
def test_selfcheck_returns_true():
    assert ax.selfcheck() is True


def test_selfcheck_cli_exits_zero():
    assert ax.main(["--selfcheck"]) == 0


def test_selfcheck_does_not_read_real_repo(tmp_path, monkeypatch):
    # Selfcheck must run correctly with NO real capabilities dir present.
    monkeypatch.chdir(tmp_path)
    assert ax.main(["--selfcheck"]) == 0


# --- unknown/garbage flag must exit non-zero (real argv dispatch) ----------
def test_unknown_flag_exits_nonzero():
    try:
        ax.main(["--definitely-not-a-flag"])
        assert False, "expected SystemExit"
    except SystemExit as exc:
        assert exc.code != 0


def test_no_args_exits_nonzero():
    try:
        ax.main([])
        assert False, "expected SystemExit"
    except SystemExit as exc:
        assert exc.code != 0


# --- --check-target: real-source liveness gate -----------------------------
def test_check_target_ok_on_real_dir_with_aliases(tmp_path):
    (tmp_path / "living.yaml").write_text(_SAMPLE_YAML, encoding="utf-8")
    ok, msg = ax.check_target(tmp_path)
    assert ok is True
    assert "capabilities OK" in msg


def test_check_target_cli_ok(tmp_path):
    (tmp_path / "living.yaml").write_text(_SAMPLE_YAML, encoding="utf-8")
    assert ax.main(["--check-target", "--capabilities-dir", str(tmp_path)]) == 0


def test_check_target_missing_dir_fails_loud():
    ok, msg = ax.check_target("/no/such/capabilities/dir/xyz")
    assert ok is False
    assert "does not exist" in msg


def test_check_target_cli_missing_dir_nonzero(tmp_path):
    missing = tmp_path / "gone"
    rc = ax.main(["--check-target", "--capabilities-dir", str(missing)])
    assert rc != 0


def test_check_target_path_is_a_file_fails(tmp_path):
    f = tmp_path / "not_a_dir.yaml"
    f.write_text(_SAMPLE_YAML, encoding="utf-8")
    ok, msg = ax.check_target(f)
    assert ok is False
    assert "not a directory" in msg


def test_check_target_empty_dir_fails_loud(tmp_path):
    # Directory exists but has no capability yaml at all -> must NOT be silent OK.
    ok, msg = ax.check_target(tmp_path)
    assert ok is False
    assert "no capability" in msg


def test_check_target_zero_aliases_is_a_liveness_fail(tmp_path):
    # THE anti-fake-green trap: files present but zero aliases to expand.
    (tmp_path / "empty_cap.yaml").write_text(
        "- name: has_no_aliases\n  aliases:\n", encoding="utf-8"
    )
    ok, msg = ax.check_target(tmp_path)
    assert ok is False
    assert "ZERO aliases" in msg


def test_check_target_only_non_capability_files_fails(tmp_path):
    # Only excluded files present -> counts as "no capability files".
    (tmp_path / "policy.yaml").write_text("- name: p\n  aliases:\n    - x\n", encoding="utf-8")
    ok, msg = ax.check_target(tmp_path)
    assert ok is False
    assert "no capability" in msg


def test_check_target_requires_capabilities_dir():
    try:
        ax.main(["--check-target"])
        assert False, "expected SystemExit"
    except SystemExit as exc:
        assert exc.code != 0


# --- expand mode via CLI gates on liveness first ---------------------------
def test_expand_mode_gates_on_liveness(tmp_path):
    # Empty dir -> expand mode refuses (loud non-zero), never silent 0.
    rc = ax.main(["--capabilities-dir", str(tmp_path)])
    assert rc != 0


def test_expand_mode_ok_prints_aliases(tmp_path, capsys):
    (tmp_path / "living.yaml").write_text(_SAMPLE_YAML, encoding="utf-8")
    rc = ax.main(["--capabilities-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "turn off the lights" in out
    assert "turn out lights" in out
    assert "good night" in out


# --- safety invariants: pure text tool, no network / actuation -------------
def _module_source() -> str:
    return Path(ax.__file__).read_text(encoding="utf-8")


def test_no_forbidden_imports():
    """AST-assert the module imports stdlib only: no third-party (PyYAML,
    hassil, requests) and no networking/subprocess/actuation modules."""
    tree = ast.parse(_module_source())
    forbidden = {
        "yaml", "hassil", "requests", "httpx", "urllib3", "aiohttp",
        "socket", "subprocess", "http", "urllib", "ftplib", "telnetlib",
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
    assert not (imported & forbidden), f"forbidden imports: {imported & forbidden}"


def test_liveness_fail_prefix_is_loud():
    # The prefix the nightly alerting greps for must be exactly this constant.
    assert ax._LIVENESS_FAIL_PREFIX == "ALIAS_EXPAND_LIVENESS_FAIL:"
