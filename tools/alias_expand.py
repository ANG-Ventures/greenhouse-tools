"""alias_expand — borrow HA/hassil alternation syntax into HACR capability aliases.

A stdlib-only, offline, PURE text->text expander. It lets HACR capability
authors write ONE hassil-style alternation phrase --

    turn (off|out) [the] lights

-- instead of hand-maintaining the 6-9 near-duplicate literal alias strings the
router's ``_match_capability`` needs today. It reads a capability YAML alias
list, expands any alias containing hassil grammar tokens ``( | )`` (alternation)
and ``[ ]`` (optional) into the FULL deterministic cross-product of plain
literal strings, and emits the expanded list.

Scope guard (see docs/SPEC-greenhouse-alias-expand.md, and REVERSIBILITY.md):

  * The expansion is PURE and REVERSIBLE: it produces exactly the alias strings
    a human would otherwise have typed. The router matcher is UNCHANGED -- it
    still sees flat literal aliases, still runs its existing normalize +
    substring-contains logic, and the safety layer (floodlight exclusion,
    confirmation gating, room remap, curated-YAML allowlist) is NOT touched.
  * This tool NEVER routes, NEVER actuates, NEVER edits the safety policy. Its
    only job is text->text alias fan-out.
  * It does NOT migrate device control to HA Assist and never implies that.

Hard invariants:
  * stdlib only; no network calls; no third-party imports (no PyYAML, no hassil).
  * Deterministic: the same input always yields the same ordered output.
  * A no-token literal alias passes through byte-identical.
  * ``--selfcheck`` is the OFFLINE logic probe (self-built fixture; touches no
    real repo). ``--check-target`` is the REAL-source liveness gate. A green
    ``--selfcheck`` is NOT a liveness signal -- that is why the two are separate.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Mirrors the router's _NON_CAPABILITY_FILES exclusion: files that live in the
# capabilities dir but are NOT capability definitions and carry no aliases.
_NON_CAPABILITY_FILES = frozenset({"policy.yaml", "groups.yaml", "llm_providers.yaml"})

# Loud, alert-worthy stderr prefix for real-target liveness failures.
_LIVENESS_FAIL_PREFIX = "ALIAS_EXPAND_LIVENESS_FAIL:"


# --- grammar expansion (the pure text->text core) ---------------------------
#
# Grammar (hassil subset):
#   * alternation:  (a|b|c)   -> one of a, b, c
#   * optional:     [x]       -> x or the empty string
#   * groups may nest and may contain alternation / optionals.
#   * everything outside a group is a literal run of characters.
#
# Expansion is the deterministic cross-product of every choice, read
# left-to-right, alternatives in source order, optional-present before
# optional-absent. Whitespace is collapsed at the very end so an optional that
# drops out (e.g. ``[the] ``) does not leave a double space.


class AliasSyntaxError(ValueError):
    """Raised when an alias contains malformed hassil grammar."""


def _tokenize(text: str) -> list:
    """Turn a raw alias into a flat token stream of literals and group markers.

    Tokens: ("lit", s) | ("(",) | ("|",) | (")",) | ("[",) | ("]",).
    """
    tokens: list = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            tokens.append(("lit", "".join(buf)))
            buf.clear()

    specials = {"(": ("(",), ")": (")",), "[": ("[",), "]": ("]",), "|": ("|",)}
    for ch in text:
        if ch in specials:
            flush()
            tokens.append(specials[ch])
        else:
            buf.append(ch)
    flush()
    return tokens


def _parse(tokens: list, pos: int, terminators: frozenset) -> tuple[list[str], int]:
    """Parse a sequence into its list of literal expansions.

    Returns (expansions, next_pos). ``terminators`` are the closing markers that
    end the current sequence (``")"``/``"|"`` inside ``()``; ``"]"`` inside
    ``[]``; empty at top level).
    """
    # A sequence is a concatenation of parts; each part contributes a list of
    # alternatives, and the sequence's expansions are the cross-product.
    seq: list[list[str]] = [[""]]  # start with the single empty prefix

    def append_part(alts: list[str]) -> None:
        seq.append(alts)

    while pos < len(tokens):
        tok = tokens[pos]
        kind = tok[0]
        if kind in terminators:
            break
        if kind == "lit":
            append_part([tok[1]])
            pos += 1
        elif kind == "(":
            alts, pos = _parse_alternation(tokens, pos + 1)
            append_part(alts)
        elif kind == "[":
            inner, pos = _parse(tokens, pos + 1, frozenset({"]"}))
            if pos >= len(tokens) or tokens[pos][0] != "]":
                raise AliasSyntaxError("unclosed '[' optional group")
            pos += 1  # consume ']'
            # optional: each inner expansion, OR the empty string. Present first.
            append_part(list(inner) + [""])
        elif kind == ")":
            raise AliasSyntaxError("unbalanced ')'")
        elif kind == "]":
            raise AliasSyntaxError("unbalanced ']'")
        elif kind == "|":
            raise AliasSyntaxError("'|' outside of an alternation group")
        else:  # pragma: no cover - defensive
            raise AliasSyntaxError(f"unexpected token {tok!r}")

    # Cross-product of every part, left-to-right.
    result = [""]
    for alts in seq:
        result = [prefix + alt for prefix in result for alt in alts]
    return result, pos


def _parse_alternation(tokens: list, pos: int) -> tuple[list[str], int]:
    """Parse the body of a ``(...)`` group starting just after the ``(``.

    Returns (alternatives, next_pos_after_closing_paren).
    """
    alternatives: list[str] = []
    while True:
        branch, pos = _parse(tokens, pos, frozenset({"|", ")"}))
        alternatives.extend(branch)
        if pos >= len(tokens):
            raise AliasSyntaxError("unclosed '(' alternation group")
        marker = tokens[pos][0]
        if marker == "|":
            pos += 1  # consume '|', parse the next branch
            continue
        if marker == ")":
            pos += 1  # consume ')'
            return alternatives, pos
        raise AliasSyntaxError(f"unexpected marker {marker!r} in alternation")


_WS_RE = re.compile(r"\s+")


def _clean(s: str) -> str:
    """Collapse runs of whitespace and strip ends (so dropped optionals do not
    leave double spaces or leading/trailing gaps)."""
    return _WS_RE.sub(" ", s).strip()


def has_grammar(alias: str) -> bool:
    """True iff the alias contains any hassil grammar token to expand."""
    return any(c in alias for c in "()[]|")


def expand_alias(alias: str) -> list[str]:
    """Expand ONE alias into its deterministic list of literal strings.

    A no-token literal returns ``[alias]`` byte-identical. An alias with grammar
    returns the full ordered cross-product, whitespace-cleaned and deduplicated
    (first occurrence wins, order preserved).
    """
    if not has_grammar(alias):
        return [alias]
    tokens = _tokenize(alias)
    expansions, pos = _parse(tokens, 0, frozenset())
    if pos != len(tokens):  # pragma: no cover - defensive; balance is enforced above
        raise AliasSyntaxError("trailing unparsed tokens")
    out: list[str] = []
    seen: set[str] = set()
    for e in expansions:
        cleaned = _clean(e)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def expand_aliases(aliases: list[str]) -> list[str]:
    """Expand a list of aliases, flattening and de-duplicating across the whole
    list (first occurrence wins; order preserved)."""
    out: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        for e in expand_alias(alias):
            if e not in seen:
                seen.add(e)
                out.append(e)
    return out


# --- minimal capability-YAML reader (stdlib only) ---------------------------
#
# The curated capabilities/*.yaml files are simple: a list of capability blocks,
# each with a scalar ``name:`` (or ``id:``) and an ``aliases:`` block list of
# scalar strings. We do NOT need a general YAML engine and MUST NOT import one
# (stdlib-only invariant). This purpose-built reader handles exactly that shape:
#
#   - name: living_room_lights
#     aliases:
#       - turn on the lights
#       - "turn (off|out) [the] lights"
#
# It also tolerates a top-level ``aliases:`` list (single-capability file) and
# quoted scalars. Anything it cannot understand is skipped, not guessed.

_LIST_ITEM_RE = re.compile(r"^(\s*)-\s+(.*)$")
_KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+)\s*:\s*(.*)$")


def _strip_comment(line: str) -> str:
    """Remove a trailing ``# comment`` that is not inside quotes."""
    out: list[str] = []
    quote: str | None = None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def parse_capability_yaml(text: str) -> list[dict]:
    """Parse the simple capability-file shape into a list of capability dicts.

    Each returned dict has keys ``name`` (str, may be "") and ``aliases``
    (list[str]). Only the ``aliases`` block lists are collected; other keys are
    recorded shallowly as ``name`` when the key is ``name``/``id``. This is a
    deliberately narrow reader, not a general YAML parser.
    """
    caps: list[dict] = []
    cur: dict | None = None
    in_aliases = False
    aliases_indent = -1

    def close() -> None:
        nonlocal cur, in_aliases, aliases_indent
        if cur is not None:
            caps.append(cur)
        cur = None
        in_aliases = False
        aliases_indent = -1

    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip(" "))

        # The aliases block ends as soon as we dedent to at/above the 'aliases:'
        # key. That includes a list item at that indent -- it is an OUTER
        # capability item ("- name: ..."), not an alias string. Alias items are
        # always indented strictly deeper than the 'aliases:' key.
        if in_aliases and indent <= aliases_indent:
            in_aliases = False

        m_item = _LIST_ITEM_RE.match(line)
        if m_item:
            content = m_item.group(2).strip()
            if in_aliases:
                # A bare aliases list item -> one alias string.
                if cur is None:
                    cur = {"name": "", "aliases": []}
                cur["aliases"].append(_unquote(content))
                continue
            # A new capability block item: "- name: x" or "- key: v".
            close()
            cur = {"name": "", "aliases": []}
            m_key = _KEY_RE.match(content)
            if m_key:
                key = m_key.group(2)
                val = _unquote(m_key.group(3))
                if key in ("name", "id"):
                    cur["name"] = val
                elif key == "aliases" and m_key.group(3).strip() == "":
                    in_aliases = True
                    aliases_indent = indent
            continue

        m_key = _KEY_RE.match(line)
        if m_key:
            key = m_key.group(2)
            val = m_key.group(3)
            if cur is None:
                cur = {"name": "", "aliases": []}
            if key in ("name", "id"):
                cur["name"] = _unquote(val)
            elif key == "aliases" and val.strip() == "":
                in_aliases = True
                aliases_indent = indent
            elif key == "aliases":
                # inline list "aliases: [a, b]" (rare) -- best-effort split.
                inline = val.strip()
                if inline.startswith("[") and inline.endswith("]"):
                    parts = [p for p in inline[1:-1].split(",")]
                    cur["aliases"].extend(_unquote(p) for p in parts if p.strip())
                    in_aliases = False
            continue

    close()
    # Drop empty shells that carried neither a name nor any alias.
    return [c for c in caps if c["name"] or c["aliases"]]


def _capability_files(cap_dir: Path) -> list[Path]:
    """The *.yaml files in cap_dir that are capability files (excluding the
    non-capability policy/config files), sorted for determinism."""
    return sorted(
        p
        for p in cap_dir.glob("*.yaml")
        if p.is_file() and p.name not in _NON_CAPABILITY_FILES
    )


def load_capabilities(cap_dir: str | Path) -> list[dict]:
    """Load every capability from a real capabilities dir. Returns a flat list
    of capability dicts (each ``{"name", "aliases", "file"}``)."""
    cap_dir = Path(cap_dir)
    caps: list[dict] = []
    for path in _capability_files(cap_dir):
        for cap in parse_capability_yaml(path.read_text(encoding="utf-8")):
            cap = dict(cap)
            cap["file"] = path.name
            caps.append(cap)
    return caps


# --- --selfcheck: OFFLINE logic probe (self-built fixture) ------------------
#
# Sandbox-safe: builds its OWN (pattern -> expected) fixtures in memory. It MUST
# NOT read, require, or assume any real HACR file, path, or the ha-command-router
# repo. It proves the expander logic only -- NOT real-world health.

def _selfcheck_fixtures() -> list[tuple[str, list[str]]]:
    """Hand-authored (pattern -> expected expansion) cases."""
    return [
        # No-token literal passes through byte-identical.
        ("turn on the lights", ["turn on the lights"]),
        # Plain alternation.
        (
            "turn (off|out) the lights",
            ["turn off the lights", "turn out the lights"],
        ),
        # Optional token: present-first, absent second (whitespace cleaned).
        (
            "turn off [the] lights",
            ["turn off the lights", "turn off lights"],
        ),
        # Alternation + optional cross-product, deterministic order.
        (
            "turn (off|out) [the] lights",
            [
                "turn off the lights",
                "turn off lights",
                "turn out the lights",
                "turn out lights",
            ],
        ),
        # The asymmetry Ace hand-patches: "turn off all the lights" from grammar.
        (
            "turn off [all] [the] lights",
            [
                "turn off all the lights",
                "turn off all lights",
                "turn off the lights",
                "turn off lights",
            ],
        ),
        # Multiple alternations multiply out in source order.
        (
            "(turn|switch) (on|off)",
            ["turn on", "turn off", "switch on", "switch off"],
        ),
        # Nested group inside an optional.
        (
            "dim [the (kitchen|hall)] lights",
            ["dim the kitchen lights", "dim the hall lights", "dim lights"],
        ),
    ]


def selfcheck() -> bool:
    """Offline logic probe. Builds its OWN in-memory fixtures and asserts the
    expander produces the exact expected cross-product, that determinism holds
    within the run, and that a no-token literal passes through byte-identical.

    Touches NO real repo/path. Returns True iff the logic is correct. This is
    the DEPLOY health check; it is NOT a liveness signal (that is --check-target).
    """
    for pattern, expected in _selfcheck_fixtures():
        got = expand_alias(pattern)
        if got != expected:
            return False
        # Determinism within the run: expanding again yields the identical list.
        if expand_alias(pattern) != got:
            return False

    # No-token literal is byte-identical (explicit, per spec).
    if expand_alias("living room scene one") != ["living room scene one"]:
        return False

    # expand_aliases dedupes across the list, order-preserving.
    merged = expand_aliases(["turn (on|off) lights", "turn on lights"])
    if merged != ["turn on lights", "turn off lights"]:
        return False

    return True


def _run_selfcheck() -> int:
    try:
        ok = selfcheck()
    except Exception as exc:  # pragma: no cover - defensive
        print(f"selfcheck FAIL: {exc}", file=sys.stderr)
        return 1
    if not ok:
        print("selfcheck FAIL: expander violated an invariant", file=sys.stderr)
        return 1
    print("selfcheck OK")
    return 0


# --- --check-target: REAL-target liveness gate (anti-fake-green) ------------
#
# Assert the ACTUAL capabilities dir this tool consumes exists, is the right
# kind, and is non-empty. "Read nothing" must be a LOUD non-zero exit, never a
# silent 0. Each failure emits a single alert-worthy stderr line prefixed
# ALIAS_EXPAND_LIVENESS_FAIL:.

def check_target(cap_dir: str | Path) -> tuple[bool, str]:
    """Real-source liveness probe. Returns (ok, message).

    Fails (loudly) unless ALL hold:
      1. the path exists and is a directory;
      2. it contains >=1 capability *.yaml (excluding the non-capability files);
      3. at least one capability has a non-empty aliases list -- the tool's
         actual input signal is present. Zero total aliases is a FAIL, not a
         quiet success (the "read nothing -> exit 0" trap being closed).
    """
    p = Path(cap_dir)
    if not p.exists():
        return False, f"capabilities dir does not exist: {p}"
    if not p.is_dir():
        return False, f"capabilities path is not a directory: {p}"

    cap_files = _capability_files(p)
    if not cap_files:
        return False, f"no capability *.yaml files under: {p}"

    total_aliases = 0
    total_caps = 0
    for path in cap_files:
        for cap in parse_capability_yaml(path.read_text(encoding="utf-8")):
            total_caps += 1
            total_aliases += len(cap["aliases"])

    if total_aliases == 0:
        return (
            False,
            f"capabilities dir has {len(cap_files)} file(s) but ZERO aliases "
            f"to expand: {p}",
        )

    return (
        True,
        f"capabilities OK: {total_aliases} alias(es) across {total_caps} "
        f"capability(ies) in {len(cap_files)} file(s) under {p}",
    )


def _run_check_target(cap_dir: str) -> int:
    ok, msg = check_target(cap_dir)
    if not ok:
        print(f"{_LIVENESS_FAIL_PREFIX} {msg}", file=sys.stderr)
        return 2
    print(f"check-target OK: {msg}")
    return 0


# --- expand mode (the actual text->text emit) -------------------------------
def _run_expand(cap_dir: str) -> int:
    ok, msg = check_target(cap_dir)
    if not ok:
        print(f"{_LIVENESS_FAIL_PREFIX} {msg}", file=sys.stderr)
        return 2
    caps = load_capabilities(cap_dir)
    for cap in caps:
        expanded = expand_aliases(cap["aliases"])
        for alias in expanded:
            print(alias)
    return 0


# --- CLI --------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alias_expand",
        description=(
            "Expand hassil-style ( | ) / [ ] grammar in HACR capability aliases "
            "into flat literal strings. Pure text->text; touches no safety policy."
        ),
    )
    parser.add_argument(
        "--capabilities-dir",
        help="path to the real HACR capabilities/ directory (the tool's source)",
    )
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="offline logic probe (self-built fixture); NOT a liveness check",
    )
    parser.add_argument(
        "--check-target",
        action="store_true",
        help=(
            "real-source liveness gate: assert --capabilities-dir exists, is a "
            "dir, and has >=1 alias to expand"
        ),
    )
    args = parser.parse_args(argv)

    if args.selfcheck:
        return _run_selfcheck()

    if args.check_target:
        if not args.capabilities_dir:
            parser.error("--check-target requires --capabilities-dir")
        return _run_check_target(args.capabilities_dir)

    if not args.capabilities_dir:
        parser.error(
            "--capabilities-dir is required unless --selfcheck is given"
        )
    return _run_expand(args.capabilities_dir)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
