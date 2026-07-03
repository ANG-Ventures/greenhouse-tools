#!/usr/bin/env python3
"""brief_delta — delta layer for the Greenhouse morning brief.

Reads the structured morning-digest render input, snapshots each run into a
bounded local store, builds a last-seen URL index over that store, and renders a
small NEW/RESOLVED/MOVED/UNCHANGED changelog. Stdlib only. See REVERSIBILITY.md.
--selfcheck = offline logic probe. --check-target = real-source liveness.
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
    """Source cannot safely drive a delta run."""


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
    url = url.strip()
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.netloc.lower()
    path = parts.path
    if path.endswith("/") and path != "/":
        path = path[:-1]
    elif path == "/":
        path = ""
    return urlunsplit((scheme, host, path, parts.query, parts.fragment))


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
    return Item(url=url.strip(), norm_url=normalize_url(url), score=score,
                source=source.strip(), section=section, title=_title_for(raw), raw=dict(raw))


def flatten_source_doc(doc: dict[str, Any], fallback_date: date | None = None) -> tuple[date, tuple[Item, ...]]:
    if not isinstance(doc, dict):
        raise LivenessError("source JSON must be an object")
    if "selected" not in doc or "also" not in doc:
        raise LivenessError("source must contain selected and also lists")
    selected = doc.get("selected")
    also = doc.get("also")
    if not isinstance(selected, list) or not isinstance(also, list):
        raise LivenessError("selected and also must be lists")
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
    items = []
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
        doc = json.loads(p.read_text(encoding="utf-8"))
        fallback_date = datetime.fromtimestamp(p.stat().st_mtime).date()
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
    ds = doc.get("brief_date")
    if not isinstance(ds, str):
        raise ValueError("snapshot missing brief_date")
    items_raw = doc.get("items")
    if not isinstance(items_raw, list):
        raise ValueError("snapshot missing items list")
    items = []
    for idx, raw in enumerate(items_raw):
        if not isinstance(raw, dict):
            raise ValueError(f"items[{idx}] is not an object")
        item_doc = dict(raw.get("raw") or {})
        item_doc.setdefault("url", raw.get("url"))
        item_doc.setdefault("score", raw.get("score"))
        item_doc.setdefault("source", raw.get("source"))
        item = _item_from_raw(item_doc, str(raw.get("section") or "selected"), idx)
        title = raw.get("title")
        if isinstance(title, str) and title:
            item = Item(item.url, item.norm_url, item.score, item.source, item.section, title, item.raw)
        items.append(item)
    return Snapshot(date.fromisoformat(ds), tuple(items), path)


def write_snapshot(state_dir: str | pathlib.Path, snapshot: Snapshot) -> pathlib.Path:
    sd = pathlib.Path(state_dir)
    sd.mkdir(parents=True, exist_ok=True)
    path = _snapshot_path(sd, snapshot.brief_date)
    path.write_text(json.dumps(snapshot_to_json(snapshot), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def load_store(state_dir: str | pathlib.Path) -> StoreLoad:
    sd = pathlib.Path(state_dir)
    if not sd.exists():
        return StoreLoad((), 0, ())
    snapshots = []
    corrupt = []
    files = sorted(p for p in sd.glob("snapshot-*.json") if p.is_file())
    for path in files:
        d = _date_from_snapshot_path(path)
        if d is None:
            continue
        try:
            snap = snapshot_from_json(json.loads(path.read_text(encoding="utf-8")), path)
        except (OSError, ValueError, json.JSONDecodeError, LivenessError) as exc:
            corrupt.append((d, path, str(exc)))
            continue
        snapshots.append(snap)
    snapshots.sort(key=lambda s: s.brief_date)
    corrupt.sort(key=lambda row: row[0])
    return StoreLoad(tuple(snapshots), len(files), tuple(corrupt))


def build_last_seen_index(store: tuple[Snapshot, ...], today_date: date) -> dict[str, PriorRef]:
    index: dict[str, PriorRef] = {}
    for snap in sorted(store, key=lambda s: s.brief_date):
        if snap.brief_date >= today_date:
            continue
        for item in snap.items:
            index[item.norm_url] = PriorRef(snap.brief_date, item)
    return index


def find_immediate_prior(store: tuple[Snapshot, ...], today_date: date) -> Snapshot | None:
    older = [s for s in store if s.brief_date < today_date]
    return max(older, key=lambda s: s.brief_date) if older else None


def find_corrupt_immediate_prior(corrupt: tuple[tuple[date, pathlib.Path, str], ...],
                                 store: tuple[Snapshot, ...], today_date: date) -> tuple[date, pathlib.Path, str] | None:
    candidates: list[tuple[date, pathlib.Path, str, bool]] = []
    for snap in store:
        if snap.brief_date < today_date:
            candidates.append((snap.brief_date, snap.path or pathlib.Path(""), "", False))
    for d, path, reason in corrupt:
        if d < today_date:
            candidates.append((d, path, reason, True))
    if not candidates:
        return None
    newest = max(candidates, key=lambda row: row[0])
    if newest[3]:
        return (newest[0], newest[1], newest[2])
    return None


def classify(today: tuple[Item, ...], index: dict[str, PriorRef], immediate_prior: Snapshot | None,
             move_threshold: int = DEFAULT_MOVE_THRESHOLD) -> dict[str, list]:
    out: dict[str, list] = {NEW: [], MOVED: [], RESOLVED: [], UNCHANGED: []}
    today_by_url = {item.norm_url: item for item in today}
    for item in today:
        prior = index.get(item.norm_url)
        if prior is None:
            out[NEW].append(item)
            continue
        score_delta = abs(item.score - prior.item.score)
        if item.section != prior.item.section or score_delta >= move_threshold:
            out[MOVED].append((item, prior))
        else:
            out[UNCHANGED].append((item, prior))
    if immediate_prior is not None:
        for prior_item in immediate_prior.items:
            if prior_item.norm_url not in today_by_url:
                out[RESOLVED].append(prior_item)
    out[NEW].sort(key=lambda item: (-item.score, item.norm_url))
    out[RESOLVED].sort(key=lambda item: (-item.score, item.norm_url))
    out[MOVED].sort(key=lambda row: (-row[0].score, row[0].norm_url))
    out[UNCHANGED].sort(key=lambda row: (-row[0].score, row[0].norm_url))
    return out


def _regime(prior: Snapshot | None, today_date: date, corrupt_prior: bool) -> tuple[str, int | None, date | None]:
    if corrupt_prior:
        return "corrupt-prior", None, None
    if prior is None:
        return "baseline", None, None
    gap = (today_date - prior.brief_date).days
    if gap <= PRODUCER_DEDUP_DAYS:
        return "in-window", gap, prior.brief_date
    return "wide-gap", gap, prior.brief_date


def make_delta(today: Snapshot, store_load: StoreLoad, move_threshold: int = DEFAULT_MOVE_THRESHOLD) -> Delta:
    corrupt_prior = find_corrupt_immediate_prior(store_load.corrupt, store_load.snapshots, today.brief_date)
    if corrupt_prior is not None:
        classes = {NEW: [], MOVED: [], RESOLVED: [], UNCHANGED: []}
        return Delta(today.brief_date, None, None, "corrupt-prior", 0, store_load.total_files,
                     True, corrupt_prior[1], len(store_load.corrupt), classes)
    index = build_last_seen_index(store_load.snapshots, today.brief_date)
    immediate = find_immediate_prior(store_load.snapshots, today.brief_date)
    classes = classify(today.items, index, immediate, move_threshold)
    regime, gap, prior_date = _regime(immediate, today.brief_date, False)
    if regime == "baseline":
        classes = {NEW: [], MOVED: [], RESOLVED: [], UNCHANGED: []}
    folded = len([s for s in store_load.snapshots if s.brief_date < today.brief_date])
    return Delta(today.brief_date, prior_date, gap, regime, folded, store_load.total_files,
                 False, None, len(store_load.corrupt), classes)


def _item_line(item: Item) -> str:
    return f"- {item.score} · {item.source} · {item.title} · {item.url}"


def render_delta(delta: Delta) -> str:
    counts = {name: len(delta.classes[name]) for name in (NEW, RESOLVED, MOVED, UNCHANGED)}
    prior = delta.prior_date.isoformat() if delta.prior_date else "none"
    gap = "n/a" if delta.gap_days is None else f"{delta.gap_days}d"
    lines = [
        f"brief-delta: {delta.today_date.isoformat()} · prior {prior} · gap {gap} · {delta.regime} · index folded {delta.index_folded} of {delta.index_total} snapshots",
        f"counts: {counts[NEW]} new · {counts[RESOLVED]} resolved · {counts[MOVED]} moved · {counts[UNCHANGED]} unchanged",
    ]
    if delta.corrupt_prior:
        lines.append(f"⚠ prior snapshot unreadable — treating as baseline: {delta.corrupt_prior_path}")
    if delta.regime == "baseline":
        lines.append("baseline — no prior snapshot; captured today without rendering all items as new")
    elif delta.regime == "in-window" and counts[MOVED] == 0 and counts[UNCHANGED] == 0:
        lines.append("in-window: additions + drop-offs, nothing resurfaced")
    elif delta.regime == "wide-gap":
        lines.append("wide-gap: continuity may be denser than normal nightly cadence")
    if delta.corrupt_count and not delta.corrupt_prior:
        lines.append(f"⚠ skipped {delta.corrupt_count} unreadable non-immediate snapshot(s) while folding index")

    if delta.classes[NEW]:
        lines.append("")
        lines.append("⭐ NEW")
        lines.extend(_item_line(item) for item in delta.classes[NEW])
    if delta.classes[RESOLVED]:
        lines.append("")
        lines.append("✓ RESOLVED / DROPPED OFF")
        lines.extend(_item_line(item) for item in delta.classes[RESOLVED])
    if delta.classes[MOVED]:
        lines.append("")
        lines.append("↕ MOVED / RESURFACED")
        for item, prior in delta.classes[MOVED]:
            age = (delta.today_date - prior.brief_date).days
            lines.append(f"- {item.score} · {item.source} · resurfaced after {age} days · score {prior.item.score}→{item.score} · {prior.item.section}→{item.section} · {item.title} · {item.url}")
    if delta.classes[UNCHANGED]:
        lines.append("")
        lines.append(f"… {len(delta.classes[UNCHANGED])} unchanged (carried over)")
    return "\n".join(lines) + "\n"


def prune_store(state_dir: str | pathlib.Path, today_date: date, retention_days: int) -> list[pathlib.Path]:
    sd = pathlib.Path(state_dir)
    cutoff = today_date - timedelta(days=retention_days)
    files = sorted(p for p in sd.glob("snapshot-*.json") if p.is_file())
    dated = []
    for p in files:
        d = _date_from_snapshot_path(p)
        if d is not None:
            dated.append((d, p))
    if not dated:
        return []
    newest_date = max(d for d, _ in dated)
    removed = []
    for d, p in dated:
        if d < cutoff and d != newest_date:
            p.unlink()
            removed.append(p)
    return removed


def run_render(source: pathlib.Path, state_dir: pathlib.Path, move_threshold: int, retention_days: int) -> int:
    try:
        today = load_source(source)
    except LivenessError as exc:
        print(f"LIVENESS FAILURE: {exc} — {source}", file=sys.stderr)
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
        print(f"LIVENESS FAILURE: {exc} — {source}", file=sys.stderr)
        return 2
    print(f"OK: {source} live, {len(snap.items)} items")
    return 0


def _synthetic_item(url: str, score: int, section: str = "selected", title: str = "Synthetic real-byte item") -> Item:
    raw = {"url": url, "score": score, "source": "X", "title": title}
    return _item_from_raw(raw, section, 0)


def run_selfcheck() -> int:
    recurring = "https://x.com/emollick/status/2072872373758382497"
    stable = "https://github.com/openai/codex-plugin-cc"
    store = []
    start = date(2026, 6, 1)
    for offset in range(8):
        d = start + timedelta(days=offset)
        items = [_synthetic_item(f"https://example.com/nightly-{offset}", 50 + offset)]
        if offset == 0:
            items.append(_synthetic_item(recurring, 90, "selected", "Recurring moved item"))
            items.append(_synthetic_item(stable, 70, "also", "Recurring unchanged item"))
        store.append(Snapshot(d, tuple(items)))
    today = Snapshot(date(2026, 6, 9), (
        _synthetic_item(recurring, 99, "selected", "Recurring moved item"),
        _synthetic_item(stable, 70, "also", "Recurring unchanged item"),
        _synthetic_item("https://example.com/new-today", 88, "selected", "New item"),
    ))
    delta = make_delta(today, StoreLoad(tuple(store), len(store), ()), DEFAULT_MOVE_THRESHOLD)
    moved_urls = {row[0].norm_url for row in delta.classes[MOVED]}
    unchanged_urls = {row[0].norm_url for row in delta.classes[UNCHANGED]}
    ok = (
        delta.gap_days == 1
        and normalize_url(recurring) in moved_urls
        and normalize_url(stable) in unchanged_urls
        and len(delta.classes[NEW]) == 1
        and len(delta.classes[RESOLVED]) == 1
        and "in-window" in render_delta(delta)
    )
    if not ok:
        print("SELFCHECK FAIL: nightly-cadence recurrence fixture did not classify as expected", file=sys.stderr)
        return 1
    print("SELFCHECK OK: nightly-cadence fixture produced NEW/RESOLVED/MOVED/UNCHANGED with 1-day prior")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="brief-delta", description="Render a delta layer for the morning brief.")
    p.add_argument("--source", type=pathlib.Path, default=DEFAULT_SOURCE,
                   help="structured morning-digest _render_input.json")
    p.add_argument("--state-dir", type=pathlib.Path, default=DEFAULT_STATE_DIR,
                   help="brief_delta snapshot store")
    p.add_argument("--move-threshold", type=int, default=DEFAULT_MOVE_THRESHOLD,
                   help="score delta greater than or equal to this marks MOVED")
    p.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS,
                   help="rolling snapshot retention window")
    p.add_argument("--render", action="store_true", help="render the delta (default action)")
    p.add_argument("--selfcheck", action="store_true", help="offline deploy health probe over a synthetic fixture")
    p.add_argument("--check-target", action="store_true", help="assert the real source exists/right-kind/non-empty")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.selfcheck:
        return run_selfcheck()
    if args.check_target:
        return run_check_target(args.source)
    return run_render(args.source, args.state_dir, args.move_threshold, args.retention_days)


if __name__ == "__main__":
    raise SystemExit(main())
