#!/usr/bin/env python3
"""fleet_doc_audit -- bounded read-only drift audit for the fleet roster doc.

Stdlib-only. The audit reports exactly the spec's bounded drift classes:
R-LCM, R-LINK, R-STAMP, and the R-AEGIS locator guard. ``--selfcheck`` is
an offline health probe over synthetic bytes. ``--check-target`` is only a
liveness/locator gate; it deliberately does not run staleness oracles.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any, Callable

DEFAULT_TARGET = pathlib.Path.home() / "Obsidian" / "Ace Place" / "AI" / "Agents.md"
FLEET_LINK_BASENAME = "2026-06-05_apollo-orchestrator-aegis-breakglass-spec.md"
IDENTITY_ALIASES = {"default": "apollo", "": "apollo"}
KNOWN_IDENTITIES = {
    "apollo",
    "aegis",
    "argus",
    "athena",
    "daedalus",
    "daedalus-opus",
    "momus",
}
EXIT_OK = 0
EXIT_DRIFT = 2
EXIT_WARN = 3


@dataclass(frozen=True)
class RuleResult:
    rule: str
    status: str
    message: str = ""
    missing: frozenset[str] = frozenset()
    extra: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AuditResult:
    results: tuple[RuleResult, ...]

    @property
    def exit_code(self) -> int:
        if any(row.status in {"STALE", "LOCATOR_MISSING"} for row in self.results):
            return EXIT_DRIFT
        if any(row.status == "WARN" for row in self.results):
            return EXIT_WARN
        return EXIT_OK

    def render(self) -> str:
        lines = [f"fleet_doc_audit: exit={self.exit_code}"]
        for row in self.results:
            detail = f" - {row.message}" if row.message else ""
            lines.append(f"{row.rule}: {row.status}{detail}")
            if row.missing:
                lines.append(f"  missing: {', '.join(sorted(row.missing))}")
            if row.extra:
                lines.append(f"  extra: {', '.join(sorted(row.extra))}")
        return "\n".join(lines) + "\n"


class AuditError(Exception):
    """Raised when a real target is missing or unreadable."""


def identity_key(raw: str, namespace: str = "profile") -> str:
    key = str(raw).strip().casefold()
    key = IDENTITY_ALIASES.get(key, key)
    if namespace == "gateway":
        if key.startswith("gateway-"):
            key = key[len("gateway-") :]
        key = IDENTITY_ALIASES.get(key, key)
    return key


def split_frontmatter(doc_bytes: bytes) -> tuple[bytes, bytes]:
    """Return (frontmatter_bytes, body_bytes), with absent/malformed frontmatter total."""
    if not doc_bytes.startswith(b"---\n"):
        return b"", doc_bytes
    closing = doc_bytes.find(b"\n---\n", 4)
    if closing == -1:
        return b"", doc_bytes
    end = closing + len(b"\n---\n")
    return doc_bytes[:end], doc_bytes[end:]


def normalize_body(doc_bytes: bytes) -> bytes:
    """Canonicalize the body bytes used by R-STAMP and --emit-stamp."""
    _frontmatter, body = split_frontmatter(doc_bytes)
    text = body.decode("utf-8", "strict").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip(" \t") for line in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return b""
    return ("\n".join(lines) + "\n").encode("utf-8")


def body_digest(doc_bytes: bytes) -> str:
    return hashlib.sha256(normalize_body(doc_bytes)).hexdigest()


def frontmatter_value(doc_text: str, key: str) -> str | None:
    if not doc_text.startswith("---\n"):
        return None
    closing = doc_text.find("\n---\n", 4)
    if closing == -1:
        return None
    frontmatter = doc_text[4:closing]
    match = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", frontmatter, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip("\"'")


def find_lcm_fence(doc_text: str) -> str | None:
    match = re.search(r"```lcm-agents\s*\n(.*?)\n```", doc_text, re.DOTALL)
    return match.group(1) if match else None


def parse_lcm_agents(fence_body: str) -> set[str]:
    return {identity_key(token) for token in re.split(r"[,\n]", fence_body) if token.strip()}


def lcm_intro_text(doc_text: str) -> str | None:
    fence_open = re.search(r"```lcm-agents\s*\n", doc_text)
    if fence_open is None:
        return None
    before_fence = doc_text[: fence_open.start()]
    marker = before_fence.rfind("context.engine: lcm")
    if marker == -1:
        return None
    return before_fence[marker + len("context.engine: lcm") :]


def prose_scope_tokens(doc_text: str, known_identities: set[str] | None = None) -> set[str]:
    """Find forbidden agent-name tokens in the prose that introduces the lcm fence."""
    intro = lcm_intro_text(doc_text)
    if intro is None:
        return set()
    known = known_identities or KNOWN_IDENTITIES
    found: set[str] = set()
    for token in re.findall(r"[A-Za-z][A-Za-z-]*", intro):
        key = identity_key(token)
        if key in known:
            found.add(key)
    return found


def find_fleet_link(doc_text: str) -> str | None:
    pathish = re.compile(r"(~?/[^\s`)]+" + re.escape(FLEET_LINK_BASENAME) + r")")
    match = pathish.search(doc_text)
    if match:
        return match.group(1)
    if FLEET_LINK_BASENAME in doc_text:
        return FLEET_LINK_BASENAME
    return None


def _engine_from(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip().casefold()
    if isinstance(value, dict):
        context = value.get("context")
        if isinstance(context, dict) and isinstance(context.get("engine"), str):
            return context["engine"].strip().casefold()
        if isinstance(value.get("engine"), str):
            return value["engine"].strip().casefold()
    return None


def running_identities_join(live: dict[str, Any]) -> set[str]:
    """Return identities that effectively run lcm and have a running gateway."""
    global_engine = (
        _engine_from(live.get("global_config"))
        or _engine_from(live.get("global"))
        or _engine_from(live.get("config"))
        or _engine_from(live.get("engine"))
    )

    profiles_raw = live.get("profiles", {})
    profile_engines: dict[str, str | None] = {}
    if isinstance(profiles_raw, dict):
        for name, value in profiles_raw.items():
            profile_engines[identity_key(str(name), "profile")] = _engine_from(value)
    elif isinstance(profiles_raw, list):
        for item in profiles_raw:
            if isinstance(item, str):
                profile_engines[identity_key(item, "profile")] = None
            elif isinstance(item, dict) and isinstance(item.get("name"), str):
                profile_engines[identity_key(item["name"], "profile")] = _engine_from(item)

    gateways_raw = live.get("running_gateways", live.get("gateways", []))
    if isinstance(gateways_raw, dict):
        running = {identity_key(str(name), "gateway") for name, enabled in gateways_raw.items() if enabled}
    else:
        running = {identity_key(str(name), "gateway") for name in gateways_raw}

    effective_lcm: set[str] = set()
    for profile, engine in profile_engines.items():
        effective = global_engine if engine in {None, "", "auto"} else engine
        if effective == "lcm":
            effective_lcm.add(profile)
    return effective_lcm & running


def load_live_json(path: str | pathlib.Path) -> dict[str, Any]:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def discover_live(home: pathlib.Path | None = None) -> dict[str, Any]:
    """Best-effort local discovery used only when no explicit fixture is supplied."""
    root = home or pathlib.Path.home()
    profiles_dir = root / ".hermes" / "profiles"
    profiles: dict[str, dict[str, Any]] = {}
    if profiles_dir.is_dir():
        for child in profiles_dir.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                profiles[child.name] = {"context": {"engine": "auto"}}
    global_engine = "lcm"
    config_path = root / ".hermes" / "config.yaml"
    try:
        config_text = config_path.read_text(encoding="utf-8")
    except OSError:
        config_text = ""
    match = re.search(r"(?m)^\s*engine:\s*([A-Za-z0-9_-]+)\s*$", config_text)
    if match:
        global_engine = match.group(1).casefold()
    return {
        "global_config": {"context": {"engine": global_engine}},
        "profiles": profiles,
        "running_gateways": sorted(profiles),
    }


def probe_rlcm(doc_text: str, live_set: set[str]) -> RuleResult:
    fence = find_lcm_fence(doc_text)
    if fence is None:
        return RuleResult("R-LCM", "LOCATOR_MISSING", "lcm-agents fence missing")
    known = set(live_set) | KNOWN_IDENTITIES | parse_lcm_agents(fence)
    prose_tokens = prose_scope_tokens(doc_text, known)
    if prose_tokens:
        return RuleResult(
            "R-LCM",
            "STALE",
            "lcm prose re-introduces scope enumeration",
            extra=frozenset(prose_tokens),
        )
    asserted = parse_lcm_agents(fence)
    missing = live_set - asserted
    extra = asserted - live_set
    if missing or extra:
        return RuleResult(
            "R-LCM",
            "STALE",
            "lcm scope set differs from live",
            missing=frozenset(missing),
            extra=frozenset(extra),
        )
    return RuleResult("R-LCM", "OK", "lcm scope matches live")


def probe_rlink(doc_text: str, exists: Callable[[str], bool] = os.path.exists) -> RuleResult:
    target = find_fleet_link(doc_text)
    if target is None:
        return RuleResult("R-LINK", "LOCATOR_MISSING", "fleet ownership spec link missing")
    expanded = os.path.expanduser(target)
    if not exists(expanded):
        return RuleResult("R-LINK", "STALE", f"dead link: {target}")
    return RuleResult("R-LINK", "OK", "fleet ownership spec link exists")


def probe_stamp(doc_bytes: bytes) -> RuleResult:
    doc_text = doc_bytes.decode("utf-8", "strict")
    if frontmatter_value(doc_text, "last_verified") is None:
        return RuleResult("R-STAMP", "LOCATOR_MISSING", "last_verified missing")
    expected = frontmatter_value(doc_text, "verified_body_sha256")
    if expected is None:
        return RuleResult("R-STAMP", "WARN", "stamp digest not yet initialized — run --emit-stamp")
    if body_digest(doc_bytes) != expected:
        return RuleResult("R-STAMP", "STALE", "body digest differs from verified_body_sha256")
    return RuleResult("R-STAMP", "OK", "body digest matches stamp")


def probe_aegis(doc_text: str) -> RuleResult:
    if re.search(r"\bAegis\b", doc_text):
        return RuleResult("R-AEGIS", "OK", "Aegis locator present")
    return RuleResult("R-AEGIS", "LOCATOR_MISSING", "Aegis locator missing")


def audit_bytes(doc_bytes: bytes, live: dict[str, Any], exists: Callable[[str], bool] = os.path.exists) -> AuditResult:
    doc_text = doc_bytes.decode("utf-8", "strict")
    live_set = running_identities_join(live)
    return AuditResult(
        (
            probe_rlcm(doc_text, live_set),
            probe_rlink(doc_text, exists),
            probe_stamp(doc_bytes),
            probe_aegis(doc_text),
        )
    )


def check_target_bytes(doc_bytes: bytes) -> AuditResult:
    doc_text = doc_bytes.decode("utf-8", "strict")
    rows: list[RuleResult] = []
    fence = find_lcm_fence(doc_text)
    if fence is None:
        rows.append(RuleResult("R-LCM", "LOCATOR_MISSING", "lcm-agents fence missing"))
    else:
        rows.append(RuleResult("R-LCM", "OK", "lcm-agents fence present"))
        tokens = prose_scope_tokens(doc_text, KNOWN_IDENTITIES | parse_lcm_agents(fence))
        if tokens:
            rows.append(
                RuleResult(
                    "R-LCM-PROSE",
                    "STALE",
                    "lcm prose re-introduces scope enumeration",
                    extra=frozenset(tokens),
                )
            )
    if find_fleet_link(doc_text) is None:
        rows.append(RuleResult("R-LINK", "LOCATOR_MISSING", "fleet ownership spec link missing"))
    else:
        rows.append(RuleResult("R-LINK", "OK", "fleet ownership spec link locator present"))
    if frontmatter_value(doc_text, "last_verified") is None:
        rows.append(RuleResult("R-STAMP", "LOCATOR_MISSING", "last_verified missing"))
    else:
        rows.append(RuleResult("R-STAMP", "OK", "last_verified present"))
    rows.append(probe_aegis(doc_text))
    return AuditResult(tuple(rows))


def read_target(path: str | pathlib.Path) -> bytes:
    target = pathlib.Path(path).expanduser()
    if not target.exists():
        raise AuditError(f"LIVENESS FAILURE: target not found: {target}")
    if not target.is_file():
        raise AuditError(f"LIVENESS FAILURE: target is not a regular file: {target}")
    data = target.read_bytes()
    if not data.strip():
        raise AuditError(f"LIVENESS FAILURE: target is empty: {target}")
    return data


def emit_stamp(doc_bytes: bytes) -> str:
    return f"verified_body_sha256: {body_digest(doc_bytes)}\n"


def fixture_live(keys: set[str] | None = None) -> dict[str, Any]:
    identities = sorted(keys or KNOWN_IDENTITIES)
    return {
        "global_config": {"context": {"engine": "lcm"}},
        "profiles": {key: {"context": {"engine": "lcm"}} for key in identities},
        "running_gateways": identities,
    }


def _doc(frontmatter: str, fence: str, link: str, body_extra: str = "") -> str:
    return (
        f"---\n{frontmatter}---\n"
        "# Agents\n\n"
        "> **Context engine:** [[Hermes LCM Context Engine — System Overview]] — the transcript-tier lossless\n"
        "> context engine (`hermes-lcm` DAG summarizer + recoverable raw store). It runs as `context.engine: lcm`\n"
        "> on the agents listed below (the rest run the built-in compressor):\n\n"
        "```lcm-agents\n"
        f"{fence}\n"
        "```\n\n"
        f"See {link} for the Apollo/Aegis ownership split.\n\n"
        "Aegis remains the break-glass recovery identity.\n"
        f"{body_extra}"
    )


def corrected_doc(with_digest: bool = False) -> str:
    link = str(pathlib.Path.home() / ".hermes" / "plans" / "archive" / FLEET_LINK_BASENAME)
    frontmatter = "last_verified: 2026-07-08\n"
    fence = ", ".join(sorted(KNOWN_IDENTITIES))
    doc = _doc(frontmatter, fence, link)
    if with_digest:
        doc = _doc(frontmatter + f"verified_body_sha256: {body_digest(doc.encode('utf-8'))}\n", fence, link)
    return doc


def stale_doc() -> str:
    dead_link = "~/.hermes/plans/2026-06-05_apollo-orchestrator-aegis-breakglass-spec.md"
    return _doc("last_verified: 2026-07-05\n", "apollo, aegis", dead_link)


def selfcheck() -> bool:
    live = fixture_live()
    corrected = corrected_doc(with_digest=True).encode("utf-8")
    stale = stale_doc().encode("utf-8")
    return (
        audit_bytes(corrected, live, exists=lambda _path: True).exit_code == EXIT_OK
        and audit_bytes(stale, live, exists=lambda _path: False).exit_code == EXIT_DRIFT
        and check_target_bytes(stale).exit_code == EXIT_OK
        and emit_stamp(corrected_doc().encode("utf-8")).startswith("verified_body_sha256: ")
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet_doc_audit")
    parser.add_argument("target", nargs="?", default=str(DEFAULT_TARGET), help="Agents.md target path")
    parser.add_argument("--live-json", help="JSON file with profiles/gateways/config fixture or probe output")
    parser.add_argument("--emit-stamp", action="store_true", help="print verified_body_sha256 for target and exit")
    parser.add_argument("--selfcheck", action="store_true", help="offline deploy health probe over a synthetic fixture")
    parser.add_argument("--check-target", action="store_true", help="assert target exists and locators are present")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.selfcheck:
        if selfcheck():
            print("SELF CHECK OK")
            return EXIT_OK
        print("SELF CHECK FAILED", file=sys.stderr)
        return 1
    try:
        doc_bytes = read_target(args.target)
        if args.emit_stamp:
            sys.stdout.write(emit_stamp(doc_bytes))
            return EXIT_OK
        if args.check_target:
            result = check_target_bytes(doc_bytes)
        else:
            live = load_live_json(args.live_json) if args.live_json else discover_live()
            result = audit_bytes(doc_bytes, live)
        sys.stdout.write(result.render())
        return result.exit_code
    except (AuditError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
