"""Command-line interface for Vault Ribbon."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, TextIO, cast

from .annotate import MalformedRAGResponse, annotate
from .index import DEFAULT_VAULT_ROOT, VaultLivenessError, build_index, check_vault

DEFAULT_RAG_URL = "http://192.168.1.216:8765"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Annotate RAG chunks with exact Obsidian vault matches.")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--annotate", action="store_true", help="read RAG JSON from stdin and write annotated JSON")
    modes.add_argument("--selfcheck", action="store_true", help="run an offline logic probe against a self-built fixture")
    modes.add_argument("--check-vault", action="store_true", help="assert the real target vault exists and has notes")
    parser.add_argument("--vault-root", default=str(DEFAULT_VAULT_ROOT), help="local Obsidian vault root")
    parser.add_argument("--asof", default=None, help="ISO8601 RAG corpus as-of time; must include timezone")
    parser.add_argument("--rag-url", default=DEFAULT_RAG_URL, help="pinned RAG endpoint for callers; no network is used by this POC")
    parser.add_argument("--stats", action="store_true", help="emit one JSON stats line to stderr")
    return parser


def _fixture_selfcheck() -> None:
    with tempfile.TemporaryDirectory(prefix="vault-ribbon-selfcheck-") as temp:
        root = Path(temp)
        (root / "Canon.md").write_text("canon\n", encoding="utf-8")
        (root / "Nested").mkdir()
        (root / "Nested" / "Canon.md").write_text("collision\n", encoding="utf-8")
        (root / ".obsidian").mkdir()
        (root / ".obsidian" / "Hidden.md").write_text("ignored\n", encoding="utf-8")

        index = build_index(root)
        response = {"chunks": [{"title": "canon", "text": "x"}, {"title": "absent", "text": "y"}]}
        annotated = cast(dict[str, Any], annotate(response, index))
        first = annotated["chunks"][0]["vault"]
        second = annotated["chunks"][1]["vault"]
        assert first["match_count"] == 2
        assert first["vault_match"] == "Canon.md"
        assert second["vault_match"] is None
        assert second["vault_is_newer"] is None


def _emit_stats(data: dict[str, object], stderr: TextIO) -> None:
    print(json.dumps(data, sort_keys=True), file=stderr)


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    args = _parser().parse_args(argv)
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr

    if args.selfcheck:
        try:
            _fixture_selfcheck()
        except Exception as exc:
            print(f"SELFCHECK FAILED: {exc}", file=stderr)
            return 1
        print("SELFCHECK OK", file=stdout)
        return 0

    if args.check_vault:
        try:
            note_count = check_vault(args.vault_root)
        except VaultLivenessError as exc:
            print(str(exc), file=stderr)
            return 2
        print(f"VAULT LIVENESS OK: notes={note_count}", file=stdout)
        return 0

    if args.annotate:
        try:
            rag_response = json.load(stdin)
            index = build_index(args.vault_root)
            annotated = annotate(rag_response, index, args.asof)
        except (json.JSONDecodeError, MalformedRAGResponse, OSError, ValueError) as exc:
            print(f"ANNOTATE FAILED: {exc}", file=stderr)
            return 2
        json.dump(annotated, stdout, sort_keys=True, separators=(",", ":"))
        stdout.write("\n")
        if args.stats:
            chunks = annotated["chunks"] if isinstance(annotated, dict) else annotated
            matched = sum(1 for chunk in chunks if chunk["vault"]["vault_match"] is not None)
            newer = sum(1 for chunk in chunks if chunk["vault"]["vault_is_newer"] is True)
            unknown = sum(1 for chunk in chunks if chunk["vault"]["vault_is_newer"] is None)
            _emit_stats(
                {
                    "chunks_annotated": len(chunks),
                    "matched": matched,
                    "newer": newer,
                    "notes_indexed": sum(len(notes) for notes in index.values()),
                    "unknown": unknown,
                },
                stderr,
            )
        return 0

    return 2
