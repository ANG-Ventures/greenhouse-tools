from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Sequence

from . import agh, frontdoors
from .diff import DriftItem, exit_code_for, reconcile

ROOT = pathlib.Path(__file__).resolve().parent
FLOOR_JSON = ROOT / "floor.json"


def load_floor() -> dict[str, Any]:
    return json.loads(FLOOR_JSON.read_text(encoding="utf-8"))


def render(items: Sequence[DriftItem], frontdoor_state: dict[str, dict[str, object]], *, live_count: int, floor: int) -> str:
    lines = ["DNS_DRIFT REPORT", f"live_count={live_count} floor={floor}"]
    if not items:
        lines.append("clean: no drift over readable frontier")
    for item in items:
        sub = f":{item.sub_reason}" if item.sub_reason else ""
        detail = ""
        if item.detail:
            detail = " " + json.dumps(item.detail, sort_keys=True)
        lines.append(f"{item.klass}{sub} {item.name}{detail}")
    deferred = [state["id"] for state in frontdoor_state.values() if state.get("deferred")]
    readable = [state["id"] for state in frontdoor_state.values() if state.get("readable")]
    lines.append(f"footer: readable={','.join(sorted(map(str, readable))) or 'none'} deferred={','.join(sorted(map(str, deferred))) or 'none'}")
    return "\n".join(lines) + "\n"


def _selfcheck() -> int:
    expected = {"missing.ace": ["fd_216"], "ok.ace": ["fd_216"], "amb.ace": ["fd_216", "fd_4"]}
    live = {
        "ok.ace": ("192.168.1.216", True),
        "deferred-live.ace": ("192.168.1.4", True),
        "outside.ace": ("192.168.1.99", True),
    }
    state = {
        "192.168.1.216": {"id": "fd_216", "readable": True, "deferred": False},
        "192.168.1.4": {"id": "fd_4", "readable": False, "deferred": True},
    }
    items = reconcile(expected, live, state, mispoint_confirmed={"fd_216"})
    got = {(item.klass, item.name, item.sub_reason) for item in items}
    required = {
        ("MISSING", "missing.ace", "absent"),
        ("ambiguous", "amb.ace", None),
        ("unknown_source", "deferred-live.ace", "deferred_source"),
        ("unknown_source", "outside.ace", "out_of_model"),
    }
    if not required.issubset(got):
        print(f"SELFCHECK FAIL: got {sorted(got)!r}", file=sys.stderr)
        return 1
    if exit_code_for(items) != 2:
        print("SELFCHECK FAIL: MISSING drift did not dominate deferred coverage", file=sys.stderr)
        return 1
    if render(items, state, live_count=len(live), floor=1) != render(items, state, live_count=len(live), floor=1):
        print("SELFCHECK FAIL: render was not deterministic", file=sys.stderr)
        return 1
    print("SELFCHECK PASS")
    return 0


def _source_overrides(values: Sequence[str]) -> dict[str, pathlib.Path]:
    overrides: dict[str, pathlib.Path] = {}
    for value in values:
        if "=" not in value:
            raise argparse.ArgumentTypeError("source overrides must be FD=PATH")
        fd_id, raw_path = value.split("=", 1)
        overrides[fd_id] = pathlib.Path(raw_path).expanduser()
    return overrides


def _load_live(path: pathlib.Path | None) -> dict[str, tuple[str, bool]]:
    if path is None:
        return agh.read_live()
    return agh.parse_rewrite_list(path.read_text(encoding="utf-8"))


def _floor(args: argparse.Namespace, live_count: int) -> int:
    lkg = args.lkg_name_count
    if lkg is None:
        lkg = int(load_floor()["lkg_name_count"])
    return int(float(args.floor_ratio) * int(lkg))


def _check_target(live: dict[str, tuple[str, bool]], expected: dict[str, list[str]], state: dict[str, dict[str, object]], floor: int) -> int:
    if len(live) < floor:
        print(f"DNS_DRIFT_LIVENESS_FAIL: AGH rewrite count {len(live)} below floor {floor}", file=sys.stderr)
        return 3
    readable_with_names = {fd for owners in expected.values() for fd in owners}
    readable_sources = [s for s in state.values() if s.get("readable") and s.get("id") in readable_with_names]
    if not readable_sources:
        print("DNS_DRIFT_LIVENESS_FAIL: zero readable declared frontdoor sources", file=sys.stderr)
        return 3
    deferred = [str(s.get("id")) for s in state.values() if s.get("deferred")]
    if deferred:
        print(f"DNS_DRIFT_COVERAGE_NOTE: deferred sources {','.join(sorted(deferred))}")
    print(f"DNS_DRIFT_LIVENESS_PASS: live={len(live)} floor={floor} readable_sources={len(readable_sources)}")
    return 0


def run(args: argparse.Namespace) -> int:
    try:
        live = _load_live(args.agh_json)
    except Exception as exc:
        print(f"DNS_DRIFT_LIVENESS_FAIL: AGH unreadable: {exc}", file=sys.stderr)
        return 3
    overrides = _source_overrides(args.source)
    deferred = set(args.defer) if args.defer else set(frontdoors.DEFAULT_DEFERRED_SOURCES)
    expected, state, unknown_shapes = frontdoors.build(path_overrides=overrides, deferred_sources=deferred)
    floor = _floor(args, len(live))
    if args.check_target:
        return _check_target(live, expected, state, floor)
    if len(live) < floor:
        print(f"DNS_DRIFT_CANT_MEASURE: AGH rewrite count {len(live)} below floor {floor}", file=sys.stderr)
        return 3
    if not any(s.get("readable") for s in state.values()):
        print("DNS_DRIFT_CANT_MEASURE: zero readable declared frontdoor sources", file=sys.stderr)
        return 3
    items = reconcile(expected, live, state, mispoint_confirmed=set(frontdoors.DEFAULT_MISPOINT_CONFIRMED))
    for raw in unknown_shapes:
        items.append(DriftItem("unknown_shape", raw, "parser_unrecognized"))
    code = exit_code_for(items)
    if args.json:
        payload = {
            "exit_code": code,
            "items": [item.__dict__ for item in items],
            "frontdoor_state": state,
            "live_count": len(live),
            "floor": floor,
        }
        print(json.dumps(payload, sort_keys=True, indent=2))
    else:
        print(render(items, state, live_count=len(live), floor=floor), end="")
    stored_floor = load_floor()
    stored_lkg = int(stored_floor["lkg_name_count"])
    if len(live) > stored_lkg:
        print(f"DNS_DRIFT_FLOOR_ADVISORY: live count {len(live)} > lkg {stored_lkg} — update floor.json")
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only .ace DNS rewrite drift reconciler")
    parser.add_argument("--selfcheck", action="store_true", help="offline deploy health probe")
    parser.add_argument("--check-target", action="store_true", help="real target liveness gate")
    parser.add_argument("--json", action="store_true", help="emit sensitive topology JSON to stdout")
    parser.add_argument("--agh-json", type=pathlib.Path, help="offline AGH rewrite-list fixture for tests")
    parser.add_argument("--source", action="append", default=[], help="override frontdoor source as fd_216=/path")
    parser.add_argument("--defer", action="append", default=[], help="mark this frontdoor id as deferred")
    parser.add_argument("--floor-ratio", type=float, default=0.85)
    parser.add_argument("--lkg-name-count", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.selfcheck:
        return _selfcheck()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
