#!/usr/bin/env python3
"""brief_delta -- small offline delta layer for the Greenhouse morning brief.

Reduced core: read the structured render input, keep dated JSON snapshots, fold
the retained store into a last-seen URL index for NEW / MOVED / UNCHANGED, and
use the immediate prior only for RESOLVED. No network, no producer mutation,
stdlib only. ``--selfcheck`` is an offline logic probe; ``--check-target`` is
the real-source liveness gate.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DEFAULT_SOURCE = pathlib.Path.home() / ".hermes" / "state" / "cron" / "morning-digest" / "_render_input.json"
DEFAULT_STATE_DIR = pathlib.Path.home() / ".hermes" / "greenhouse" / "brief_delta"
DEFAULT_MOVE_THRESHOLD = 5
DEFAULT_RETENTION_DAYS = 35
PRODUCER_DEDUP_DAYS = 7

NEW = "NEW"
MOVED = "MOVED"
RESOLVED = "RESOLVED"
UNCHANGED = "UNCHANGED"


class LivenessError(Exception):
    """The configured source is not safe to use for a delta run."""


@dataclass(frozen=True)
class Item:
    url: str
    norm_url: str
    score: int
    source: str
    section: str
    title: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class Snapshot:
    brief_date: date
    items: tuple[Item, ...]
    path: pathlib.Path | None = None


@dataclass(frozen=True)
class PriorRef:
    brief_date: date
    item: Item


@dataclass(frozen=True)
class StoreLoad:
    snapshots: tuple[Snapshot, ...]
    total_files: int
    corrupt: tuple[tuple[date, pathlib.Path, str], ...]


@dataclass(frozen=True)
class Delta:
    today_date: date
    prior_date: date | None
    gap_days: int | None
    regime: str
    index_folded: int
    index_total: int
    corrupt_prior: bool
    corrupt_prior_path: pathlib.Path | None
    corrupt_count: int
    classes: dict[str, list]


def normalize_url(url: str) -> str:
    """Normalize only the safe bits needed for deterministic URL matching."""
    parts = urlsplit(url.strip())
    path = parts.path
    if path == "/":
        path = ""
    elif path.endswith("/"):
        path = path[:-1]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, parts.fragment))


def _title_for(item: dict[str, Any]) -> str:
    for key in ("title", "tweet_text", "event_key", "url"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.strip().split())[:160]
    return "<untitled>"


def _parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def _item_from_raw(raw: dict[str, Any], section: str, idx: int) -> Item:
    if not isinstance(raw, dict):
        raise LivenessError(f"{section}[{idx}] is not an object")
    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise LivenessError(f"{section}[{idx}] missing non-empty url")
    score = raw.get("score")
    if not isinstance(score, int):
        raise LivenessError(f"{section}[{idx}] missing integer score")
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise LivenessError(f"{section}[{idx}] missing non-empty source")
    clean_url = url.strip()
    return Item(
        url=clean_url,
        norm_url=normalize_url(clean_url),
        score=score,
        source=source.strip(),
        section=section,
        title=_title_for(raw),
        raw=dict(raw),
    )


def flatten_source_doc(doc: dict[str, Any], fallback_date: date | None = None) -> tuple[date, tuple[Item, ...]]:
    if not isinstance(doc, dict):
        raise LivenessError("source JSON must be an object")
    selected = doc.get("selected")
    also = doc.get("also")
    if not isinstance(selected, list) or not isinstance(also, list):
        raise LivenessError("source must contain selected and also lists")

    ts = doc.get("ts")
    if isinstance(ts, str) and len(ts) >= 10:
        try:
            brief_date = _parse_date(ts)
        except ValueError as exc:
            raise LivenessError(f"source missing parseable ts: {exc}") from None
    elif fallback_date is not None:
        brief_date = fallback_date
    else:
        raise LivenessError("source missing parseable ts")

    items: list[Item] = []
    for idx, raw in enumerate(selected):
        items.append(_item_from_raw(raw, "selected", idx))
    for idx, raw in enumerate(also):
        items.append(_item_from_raw(raw, "also", idx))
    if not items:
        raise LivenessError("source has zero selected+also items")
    return brief_date, tuple(items)


def load_source(path: str | pathlib.Path) -> Snapshot:
    p = pathlib.Path(path)
    if not p.exists():
        raise LivenessError("source not found")
    if not p.is_file():
        raise LivenessError("source is not a regular file")
    try:
        text = p.read_text(encoding="utf-8")
        fallback_date = datetime.fromtimestamp(p.stat().st_mtime).date()
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LivenessError(f"source is not valid JSON: {exc.msg}") from None
    except OSError as exc:
        raise LivenessError(f"source unreadable: {exc}") from None
    brief_date, items = flatten_source_doc(doc, fallback_date)
    return Snapshot(brief_date, items, p)


def _snapshot_path(state_dir: pathlib.Path, brief_date: date) -> pathlib.Path:
    return state_dir / f"snapshot-{brief_date.isoformat()}.json"


def _date_from_snapshot_path(path: pathlib.Path) -> date | None:
    name = path.name
    if not (name.startswith("snapshot-") and name.endswith(".json")):
        return None
    try:
        return date.fromisoformat(name[len("snapshot-"):-len(".json")])
    except ValueError:
        return None


def snapshot_to_json(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "brief_date": snapshot.brief_date.isoformat(),
        "items": [
            {
                "url": item.url,
                "score": item.score,
                "source": item.source,
                "section": item.section,
                "title": item.title,
                "raw": item.raw,
            }
            for item in snapshot.items
        ],
    }


def snapshot_from_json(doc: dict[str, Any], path: pathlib.Path | None = None) -> Snapshot:
    if not isinstance(doc, dict):
        raise ValueError("snapshot must be an object")
    brief_date_raw = doc.get("brief_date")
    if not isinstance(brief_date_raw, str):
        raise ValueError("snapshot missing brief_date")
    items_raw = doc.get("items")
    if not isinstance(items_raw, list):
        raise ValueError("snapshot missing items list")

    items: list[Item] = []
    for idx, raw in enumerate(items_raw):
        if not isinstance(raw, dict):
            raise ValueError(f"items[{idx}] is not an object")
        item_doc = dict(raw.get("raw") or {})
        item_doc.setdefault("url", raw.get("url"))
        item_doc.setdefault("score", raw.get("score"))
        item_doc.setdefault("source", raw.get("source"))
        section = str(raw.get("section") or "selected")
        item = _item_from_raw(item_doc, section, idx)
        title = raw.get("title")
        if isinstance(title, str) and title.strip():
            item = Item(item.url, item.norm_url, item.score, item.source, item.section, title, item.raw)
        items.append(item)
    return Snapshot(date.fromisoformat(brief_date_raw), tuple(items), path)


def write_snapshot(state_dir: str | pathlib.Path, snapshot: Snapshot) -> pathlib.Path:
    sd = pathlib.Path(state_dir)
    sd.mkdir(parents=True, exist_ok=True)
    path = _snapshot_path(sd, snapshot.brief_date)
    payload = json.dumps(snapshot_to_json(snapshot), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8")
    return path


def load_store(state_dir: str | pathlib.Path) -> StoreLoad:
    sd = pathlib.Path(state_dir)
    if not sd.exists():
        return StoreLoad((), 0, ())
    snapshots: list[Snapshot] = []
    corrupt: list[tuple[date, pathlib.Path, str]] = []
    files = sorted(p for p in sd.glob("snapshot-*.json") if p.is_file())
    for path in files:
        snapshot_date = _date_from_snapshot_path(path)
        if snapshot_date is None:
            continue
        try:
            snapshots.append(snapshot_from_json(json.loads(path.read_text(encoding="utf-8")), path))
        except (OSError, ValueError, json.JSONDecodeError, LivenessError) as exc:
            corrupt.append((snapshot_date, path, str(exc)))
    snapshots.sort(key=lambda snap: snap.brief_date)
    corrupt.sort(key=lambda row: row[0])
    return StoreLoad(tuple(snapshots), len(files), tuple(corrupt))


def build_last_seen_index(store: tuple[Snapshot, ...], today_date: date) -> dict[str, PriorRef]:
    index: dict[str, PriorRef] = {}
    for snap in sorted(store, key=lambda row: row.brief_date):
        if snap.brief_date >= today_date:
            continue
        for item in snap.items:
            index[item.norm_url] = PriorRef(snap.brief_date, item)
    return index


def find_immediate_prior(store: tuple[Snapshot, ...], today_date: date) -> Snapshot | None:
    older = [snap for snap in store if snap.brief_date < today_date]
    return max(older, key=lambda snap: snap.brief_date) if older else None


def find_corrupt_immediate_prior(
    corrupt: tuple[tuple[date, pathlib.Path, str], ...],
    store: tuple[Snapshot, ...],
    today_date: date,
) -> tuple[date, pathlib.Path, str] | None:
    newest_valid = find_immediate_prior(store, today_date)
    newest_valid_date = newest_valid.brief_date if newest_valid is not None else None
    older_corrupt = [row for row in corrupt if row[0] < today_date]
    if not older_corrupt:
        return None
    newest_corrupt = max(older_corrupt, key=lambda row: row[0])
    if newest_valid_date is None or newest_corrupt[0] > newest_valid_date:
        return newest_corrupt
    return None


def classify(
    today: tuple[Item, ...],
    index: dict[str, PriorRef],
    immediate_prior: Snapshot | None,
    move_threshold: int = DEFAULT_MOVE_THRESHOLD,
) -> dict[str, list]:
    """Classify only the reduced immediate delta core."""
    out: dict[str, list] = {NEW: [], MOVED: [], RESOLVED: [], UNCHANGED: []}
    today_by_url = {item.norm_url: item for item in today}
    for item in today:
        prior = index.get(item.norm_url)
        if prior is None:
            out[NEW].append(item)
        elif item.section != prior.item.section or abs(item.score - prior.item.score) >= move_threshold:
            out[MOVED].append((item, prior))
        else:
            out[UNCHANGED].append((item, prior))
    if immediate_prior is not None:
        for prior_item in immediate_prior.items:
            if prior_item.norm_url not in today_by_url:
                out[RESOLVED].append(prior_item)
    out[NEW].sort(key=lambda item: (-item.score, item.norm_url))
    out[MOVED].sort(key=lambda row: (-row[0].score, row[0].norm_url))
    out[RESOLVED].sort(key=lambda item: (-item.score, item.norm_url))
    out[UNCHANGED].sort(key=lambda row: (-row[0].score, row[0].norm_url))
    return out


def _regime(prior: Snapshot | None, today_date: date, corrupt_prior: bool) -> tuple[str, int | None, date | None]:
    if corrupt_prior:
        return "corrupt-prior", None, None
    if prior is None:
        return "baseline", None, None
    gap = (today_date - prior.brief_date).days
    return "in-window" if gap <= PRODUCER_DEDUP_DAYS else "wide-gap", gap, prior.brief_date


def make_delta(today: Snapshot, store_load: StoreLoad, move_threshold: int = DEFAULT_MOVE_THRESHOLD) -> Delta:
    corrupt_prior = find_corrupt_immediate_prior(store_load.corrupt, store_load.snapshots, today.brief_date)
    if corrupt_prior is not None:
        classes: dict[str, list] = {NEW: [], MOVED: [], RESOLVED: [], UNCHANGED: []}
        return Delta(today.brief_date, None, None, "corrupt-prior", 0, store_load.total_files, True, corrupt_prior[1], len(store_load.corrupt), classes)

    immediate = find_immediate_prior(store_load.snapshots, today.brief_date)
    index = build_last_seen_index(store_load.snapshots, today.brief_date)
    classes = classify(today.items, index, immediate, move_threshold)
    regime, gap, prior_date = _regime(immediate, today.brief_date, False)
    if regime == "baseline":
        classes = {NEW: [], MOVED: [], RESOLVED: [], UNCHANGED: []}
    folded = sum(1 for snap in store_load.snapshots if snap.brief_date < today.brief_date)
    return Delta(today.brief_date, prior_date, gap, regime, folded, store_load.total_files, False, None, len(store_load.corrupt), classes)


def _change_line(status: str, item: Item) -> str:
    return f"- {status} | {item.score} | {item.source} | {item.title} | {item.url}"

def _moved_line(item: Item, prior: PriorRef) -> str:
    return f"{_change_line(MOVED, item)} | was {prior.item.section} score {prior.item.score}"


def render_delta(delta: Delta) -> str:
    counts = {name: len(delta.classes[name]) for name in (NEW, RESOLVED, MOVED, UNCHANGED)}
    prior = delta.prior_date.isoformat() if delta.prior_date else "none"
    gap = "n/a" if delta.gap_days is None else f"{delta.gap_days}d"
    lines = [
        f"brief-delta: {delta.today_date.isoformat()} | prior {prior} | gap {gap} | {delta.regime} | index folded {delta.index_folded} of {delta.index_total} snapshots",
        f"counts: {counts[NEW]} new | {counts[MOVED]} moved | {counts[RESOLVED]} resolved | {counts[UNCHANGED]} unchanged",
    ]
    if delta.corrupt_prior:
        lines.append(f"WARNING: prior snapshot unreadable; no delta emitted: {delta.corrupt_prior_path}")
    elif delta.regime == "baseline":
        lines.append("baseline: no prior snapshot; captured today without listing all items as new")
    elif delta.regime == "wide-gap":
        lines.append("wide-gap: prior snapshot is outside normal producer dedup cadence")
    if delta.corrupt_count and not delta.corrupt_prior:
        lines.append(f"WARNING: skipped {delta.corrupt_count} unreadable non-immediate snapshot(s)")

    if delta.classes[NEW]:
        lines.append("")
        lines.append("NEW")
        lines.extend(_change_line(NEW, item) for item in delta.classes[NEW])
    if delta.classes[MOVED]:
        lines.append("")
        lines.append("MOVED")
        lines.extend(_moved_line(item, prior) for item, prior in delta.classes[MOVED])
    if delta.classes[RESOLVED]:
        lines.append("")
        lines.append("RESOLVED")
        lines.extend(_change_line(RESOLVED, item) for item in delta.classes[RESOLVED])
    if delta.classes[UNCHANGED]:
        lines.append("")
        lines.append(f"UNCHANGED: {len(delta.classes[UNCHANGED])} carried over")
    return "\n".join(lines) + "\n"


def prune_store(state_dir: str | pathlib.Path, today_date: date, retention_days: int) -> list[pathlib.Path]:
    sd = pathlib.Path(state_dir)
    cutoff = today_date - timedelta(days=retention_days)
    dated = []
    for path in sorted(p for p in sd.glob("snapshot-*.json") if p.is_file()):
        snapshot_date = _date_from_snapshot_path(path)
        if snapshot_date is not None:
            dated.append((snapshot_date, path))
    if not dated:
        return []
    newest = max(snapshot_date for snapshot_date, _ in dated)
    removed: list[pathlib.Path] = []
    for snapshot_date, path in dated:
        if snapshot_date < cutoff and snapshot_date != newest:
            path.unlink()
            removed.append(path)
    return removed


def run_render(source: pathlib.Path, state_dir: pathlib.Path, move_threshold: int, retention_days: int) -> int:
    try:
        today = load_source(source)
    except LivenessError as exc:
        print(f"LIVENESS FAILURE: {exc} -- {source}", file=sys.stderr)
        return 2
    store = load_store(state_dir)
    delta = make_delta(today, store, move_threshold)
    sys.stdout.write(render_delta(delta))
    write_snapshot(state_dir, today)
    prune_store(state_dir, today.brief_date, retention_days)
    return 0


def run_check_target(source: pathlib.Path) -> int:
    try:
        snap = load_source(source)
    except LivenessError as exc:
        print(f"LIVENESS FAILURE: {exc} -- {source}", file=sys.stderr)
        return 2
    print(f"OK: {source} live, {len(snap.items)} items")
    return 0


def _synthetic_item(url: str, score: int, section: str = "selected", title: str = "Synthetic item") -> Item:
    return _item_from_raw({"url": url, "score": score, "source": "X", "title": title}, section, 0)


def run_selfcheck() -> int:
    prior = Snapshot(date(2026, 7, 2), (
        _synthetic_item("https://example.com/gone", 60),
    ))
    old = Snapshot(date(2026, 6, 24), (
        _synthetic_item("https://example.com/stable", 70),
        _synthetic_item("https://example.com/unchanged", 55),
    ))
    today = Snapshot(date(2026, 7, 3), (
        _synthetic_item("https://example.com/stable/", 75),
        _synthetic_item("https://example.com/unchanged", 55),
        _synthetic_item("https://example.com/new", 90),
    ))
    delta = make_delta(today, StoreLoad((old, prior), 2, ()), DEFAULT_MOVE_THRESHOLD)
    ok = (
        delta.regime == "in-window"
        and len(delta.classes[NEW]) == 1
        and len(delta.classes[MOVED]) == 1
        and len(delta.classes[RESOLVED]) == 1
        and len(delta.classes[UNCHANGED]) == 1
        and delta.gap_days == 1
        and "counts: 1 new | 1 moved | 1 resolved | 1 unchanged" in render_delta(delta)
    )
    if not ok:
        print("SELFCHECK FAIL: reduced immediate-delta fixture did not classify as expected", file=sys.stderr)
        return 1
    print("SELFCHECK OK: reduced immediate-delta fixture produced NEW/MOVED/RESOLVED/UNCHANGED")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brief-delta", description="Render a small delta layer for the morning brief.")
    parser.add_argument("--source", type=pathlib.Path, default=DEFAULT_SOURCE, help="structured morning-digest _render_input.json")
    parser.add_argument("--state-dir", type=pathlib.Path, default=DEFAULT_STATE_DIR, help="brief_delta snapshot store")
    parser.add_argument("--move-threshold", type=int, default=DEFAULT_MOVE_THRESHOLD, help="minimum absolute score change that classifies a carried item as MOVED")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS, help="rolling snapshot retention window")
    parser.add_argument("--render", action="store_true", help="render the delta (default action)")
    parser.add_argument("--selfcheck", action="store_true", help="offline deploy health probe over a synthetic fixture")
    parser.add_argument("--check-target", action="store_true", help="assert the real source exists/right-kind/non-empty")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.selfcheck:
        return run_selfcheck()
    if args.check_target:
        return run_check_target(args.source)
    return run_render(args.source, args.state_dir, args.move_threshold, args.retention_days)


if __name__ == "__main__":
    raise SystemExit(main())
