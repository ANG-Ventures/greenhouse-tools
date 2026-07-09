from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class FrontdoorSource:
    id: str
    ip: str
    path: pathlib.Path
    parser: str


DEFAULT_FRONTDOOR_SOURCES: dict[str, FrontdoorSource] = {
    "fd_216": FrontdoorSource(
        "fd_216",
        "192.168.1.216",
        pathlib.Path("~/Projects/ace-media-homelab/stacks/lan-proxy/services.yaml").expanduser(),
        "services_yaml",
    ),
    "fd_4": FrontdoorSource("fd_4", "192.168.1.4", pathlib.Path("/nonexistent/deferred-fd4-Caddyfile"), "caddyfile"),
    "fd_5": FrontdoorSource("fd_5", "192.168.1.5", pathlib.Path("/nonexistent/deferred-fd5-Caddyfile"), "caddyfile"),
    "fd_18": FrontdoorSource("fd_18", "192.168.1.18", pathlib.Path("/nonexistent/deferred-fd18-Caddyfile"), "caddyfile"),
}
DEFAULT_DEFERRED_SOURCES = frozenset({"fd_4", "fd_5", "fd_18"})
DEFAULT_MISPOINT_CONFIRMED = frozenset({"fd_216"})
ACE_NAME_RE = re.compile(r"(?<![A-Za-z0-9_.-])(?:\*\.)?[A-Za-z0-9][A-Za-z0-9.-]*\.ace\b")


def parse_services_yaml(text: str) -> tuple[list[str], list[str]]:
    names: list[str] = []
    unknown: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith((" ", "\t")) and stripped.endswith(":"):
            key = stripped[:-1].strip('"\'')
            if key.endswith(".ace"):
                names.append(key)
            elif ".ace" in key:
                unknown.append(stripped)
    return names, unknown


def parse_caddyfile_hosts(text: str) -> tuple[list[str], list[str]]:
    names: list[str] = []
    unknown: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        matches = ACE_NAME_RE.findall(line)
        names.extend(match.rstrip(",") for match in matches)
        if ".ace" in line and not matches:
            unknown.append(line)
    return names, unknown


def parse_source(path: pathlib.Path, parser: str) -> tuple[list[str], list[str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if parser == "services_yaml":
        return parse_services_yaml(text)
    if parser == "caddyfile":
        return parse_caddyfile_hosts(text)
    raise ValueError(f"unknown parser: {parser}")


def sources_from_env() -> dict[str, FrontdoorSource]:
    sources = dict(DEFAULT_FRONTDOOR_SOURCES)
    for fd_id, source in list(sources.items()):
        env = os.environ.get(f"DNS_DRIFT_SOURCE_{fd_id.upper()}")
        if env:
            sources[fd_id] = FrontdoorSource(source.id, source.ip, pathlib.Path(env).expanduser(), source.parser)
    return sources


def build(
    *,
    sources: Mapping[str, FrontdoorSource] | None = None,
    path_overrides: Mapping[str, pathlib.Path] | None = None,
    deferred_sources: set[str] | frozenset[str] | None = None,
) -> tuple[dict[str, list[str]], dict[str, dict[str, object]], list[str]]:
    configured = dict(sources or sources_from_env())
    if path_overrides:
        for fd_id, path in path_overrides.items():
            source = configured[fd_id]
            configured[fd_id] = FrontdoorSource(source.id, source.ip, path, source.parser)
    deferred = set(DEFAULT_DEFERRED_SOURCES if deferred_sources is None else deferred_sources)
    expected: dict[str, list[str]] = {}
    state: dict[str, dict[str, object]] = {}
    unknown_shapes: list[str] = []

    for fd_id in sorted(configured):
        source = configured[fd_id]
        is_deferred = fd_id in deferred
        readable = False
        names: list[str] = []
        if not is_deferred:
            try:
                names, source_unknown = parse_source(source.path, source.parser)
                readable = bool(names)
                unknown_shapes.extend(f"{fd_id}:{line}" for line in source_unknown)
            except OSError:
                readable = False
                is_deferred = True
        state[source.ip] = {"id": fd_id, "readable": readable, "deferred": is_deferred}
        if readable:
            for name in names:
                expected.setdefault(name, []).append(fd_id)
    return expected, state, unknown_shapes
