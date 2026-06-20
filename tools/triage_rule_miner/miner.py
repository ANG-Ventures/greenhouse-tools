"""triage_rule_miner — propose-only Gmail/Calendar triage filter miner.

Reads a behavior-metadata NDJSON export (produced out-of-band by the `gws` CLI),
mines the last 30 days for deterministic archive_unread patterns, and emits a
short, ranked list of pre-written one-tap filter PROPOSALS.

Hard invariants (see REVERSIBILITY.md):
  * Never mutates Gmail/Calendar state — proposes only, file-only output.
  * stdlib only.
  * Writes nothing outside the configured --out directory.
  * A proposal appears only at >= CONFIDENCE_GATE and >= MIN_SUPPORT.

v0.1 mines the `archived_unread` action only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# --- Tunables (resolved decisions D-3 / D-5) -------------------------------
CONFIDENCE_GATE = 0.90
MIN_SUPPORT = 8
MAX_PROPOSALS = 10
MINED_ACTION = "archived_unread"

VALID_ACTIONS = {
    "replied",
    "archived_unread",
    "archived_read",
    "labeled",
    "ignored",
    "deleted",
}

_DIGITS = re.compile(r"\d+")
_HEX = re.compile(r"\b[0-9a-fA-F]{6,}\b")
_WS = re.compile(r"\s+")


# --- Pipeline: load -> normalize -> cluster -> gate -> render --------------
def normalize_subject(subject: str) -> str:
    """Collapse a subject to a stable shape token (D-4).

    Hex runs and digit runs both become '#'; whitespace is collapsed. So
    "deploy succeeded #4821" and "deploy succeeded #4822" share one shape.
    """
    s = _HEX.sub("#", subject or "")
    s = _DIGITS.sub("#", s)
    s = _WS.sub(" ", s).strip()
    return s


def load(path: Path) -> list[dict]:
    """Parse an NDJSON behavior export. One JSON object per line.

    Blank lines are skipped. A malformed line raises ValueError (consumed by
    --selfcheck as the corrupt-input signal).
    """
    records: list[dict] = []
    text = Path(path).read_text(encoding="utf-8")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {lineno}: invalid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"line {lineno}: record is not an object")
        for field in ("sender", "subject", "action"):
            if field not in obj:
                raise ValueError(f"line {lineno}: missing field '{field}'")
        if obj["action"] not in VALID_ACTIONS:
            raise ValueError(f"line {lineno}: unknown action {obj['action']!r}")
        records.append(obj)
    return records


def cluster(records: list[dict]) -> dict[tuple[str, str], dict[str, int]]:
    """Group records by (sender, subject_shape); count actions within each."""
    groups: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for rec in records:
        key = (rec["sender"], normalize_subject(rec["subject"]))
        groups[key][rec["action"]] += 1
    return {k: dict(v) for k, v in groups.items()}


def _proposal(sender: str, shape: str, support: int, confidence: float) -> dict:
    label = "ops/noise"
    return {
        "pattern": {"from": sender, "subject_shape": shape},
        "action": "archive_unread",
        "support": support,
        "confidence": round(confidence, 4),
        "proposed_filter": {
            "criteria": {"from": sender},
            "action": {"removeLabelIds": ["INBOX"], "addLabelIds": [label]},
        },
        "one_tap": f"Skip inbox + label {label} for {sender}",
    }


def gate(groups: dict[tuple[str, str], dict[str, int]]) -> list[dict]:
    """Keep only patterns that are >= CONFIDENCE_GATE and >= MIN_SUPPORT for
    the v0.1 mined action. Rank by support * confidence, cap at MAX_PROPOSALS.
    """
    proposals: list[dict] = []
    for (sender, shape), counts in groups.items():
        total = sum(counts.values())
        support = counts.get(MINED_ACTION, 0)
        if total == 0:
            continue
        confidence = support / total
        if support < MIN_SUPPORT or confidence < CONFIDENCE_GATE:
            continue
        proposals.append(_proposal(sender, shape, support, confidence))
    proposals.sort(
        key=lambda p: (p["support"] * p["confidence"], p["pattern"]["from"]),
        reverse=True,
    )
    return proposals[:MAX_PROPOSALS]


def render_md(proposals: list[dict]) -> str:
    """Human-skim markdown. A weekly 5-tap review, not a nagging PR."""
    lines = ["# Triage rule proposals", ""]
    if not proposals:
        lines.append("No deterministic patterns met the confidence gate. Nothing to propose.")
        lines.append("")
        return "\n".join(lines)
    lines.append(f"{len(proposals)} proposal(s). Each proposes only — apply none, some, or all.")
    lines.append("")
    for i, p in enumerate(proposals, start=1):
        conf = int(round(p["confidence"] * 100))
        lines.append(f"{i}. {p['one_tap']}")
        lines.append(f"   - support: {p['support']} events, confidence: {conf}%")
        lines.append(f"   - shape: `{p['pattern']['subject_shape']}`")
        lines.append("")
    return "\n".join(lines)


def mine(records: list[dict]) -> list[dict]:
    return gate(cluster(records))


def write_outputs(proposals: list[dict], out_dir: Path) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "proposals.json"
    md_path = out_dir / "proposals.md"
    json_path.write_text(
        json.dumps({"version": "0.1", "proposals": proposals}, indent=2) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_md(proposals), encoding="utf-8")
    return json_path, md_path


# --- --selfcheck deploy health probe (D-1 / invariant) ---------------------
_GOOD_FIXTURE = "\n".join(
    json.dumps(
        {
            "sender": "deploys@ci.example",
            "subject": f"deploy succeeded #{4800 + i}",
            "action": "archived_unread",
            "ts": "2026-06-01T03:10:00Z",
        }
    )
    for i in range(10)
)


def selfcheck(text: str) -> bool:
    """Return True iff `text` is a well-formed export that yields >=1 proposal.

    Used by --selfcheck: known-good input -> exit 0; corrupt input -> non-zero.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        fixture = Path(td) / "behavior.ndjson"
        fixture.write_text(text, encoding="utf-8")
        records = load(fixture)  # raises ValueError on corrupt input
        proposals = mine(records)
        return len(proposals) >= 1


def _run_selfcheck() -> int:
    try:
        ok = selfcheck(_GOOD_FIXTURE)
    except Exception as exc:  # corrupt/invalid -> non-zero
        print(f"selfcheck FAIL: {exc}", file=sys.stderr)
        return 1
    if not ok:
        print("selfcheck FAIL: known-good input produced no proposal", file=sys.stderr)
        return 1
    print("selfcheck OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="triage_rule_miner",
        description="Propose-only Gmail/Calendar triage filter miner (v0.1).",
    )
    parser.add_argument("--in", dest="in_path", help="behavior.ndjson export to mine")
    parser.add_argument("--out", dest="out_dir", help="output directory for proposals")
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="deploy health probe: exit 0 on known-good input, non-zero otherwise",
    )
    args = parser.parse_args(argv)

    if args.selfcheck:
        return _run_selfcheck()

    if not args.in_path or not args.out_dir:
        parser.error("--in and --out are required unless --selfcheck is given")

    try:
        records = load(Path(args.in_path))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    proposals = mine(records)
    json_path, md_path = write_outputs(proposals, Path(args.out_dir))
    print(f"wrote {len(proposals)} proposal(s) -> {json_path} , {md_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
