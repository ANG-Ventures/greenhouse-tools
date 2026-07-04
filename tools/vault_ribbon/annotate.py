"""Pure RAG response annotation logic."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, overload

from .index import VaultNote


class MalformedRAGResponse(ValueError):
    """Raised when raw RAG JSON is not a supported chunk envelope."""


def iter_chunks(rag_response: Any) -> Iterable[dict[str, Any]]:
    """Yield chunk dictionaries from the real RAG envelope or from a bare list."""

    if isinstance(rag_response, dict):
        chunks = rag_response.get("chunks")
    elif isinstance(rag_response, list):
        chunks = rag_response
    else:
        raise MalformedRAGResponse("RAG response must be a dict with 'chunks' or a bare chunk list")

    if not isinstance(chunks, list):
        raise MalformedRAGResponse("RAG response 'chunks' must be a list")

    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise MalformedRAGResponse("each RAG chunk must be an object")
        yield chunk


def parse_asof(value: str | None) -> datetime | None:
    """Parse an ISO8601 asof timestamp, requiring an explicit timezone offset."""

    if value is None:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--asof must include a timezone offset; refusing to assume local time")
    return parsed.astimezone(timezone.utc)


def _annotate_chunk(chunk: dict[str, Any], index: dict[str, list[VaultNote]], asof: datetime | None) -> dict[str, Any]:
    title = str(chunk.get("title", "")).strip().casefold()
    matches = index.get(title, []) if title else []
    first = matches[0] if matches else None
    vault_is_newer = None
    if first is not None and asof is not None:
        vault_is_newer = first.mtime > asof

    annotated = dict(chunk)
    annotated["vault"] = {
        "vault_match": first.path if first is not None else None,
        "match_count": len(matches),
        "vault_note_mtime": first.mtime_iso if first is not None else None,
        "vault_is_newer": vault_is_newer,
    }
    return annotated


@overload
def annotate(
    rag_response: dict[str, Any],
    index: dict[str, list[VaultNote]],
    asof: str | datetime | None = None,
) -> dict[str, Any]:
    ...


@overload
def annotate(
    rag_response: list[dict[str, Any]],
    index: dict[str, list[VaultNote]],
    asof: str | datetime | None = None,
) -> list[dict[str, Any]]:
    ...


def annotate(
    rag_response: dict[str, Any] | list[dict[str, Any]],
    index: dict[str, list[VaultNote]],
    asof: str | datetime | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Add per-chunk vault metadata without changing original fields or order."""

    if isinstance(asof, str) or asof is None:
        parsed_asof = parse_asof(asof)
    else:
        if asof.tzinfo is None or asof.utcoffset() is None:
            raise ValueError("asof must include a timezone offset; refusing to assume local time")
        parsed_asof = asof.astimezone(timezone.utc)

    chunks = [_annotate_chunk(chunk, index, parsed_asof) for chunk in iter_chunks(rag_response)]
    if isinstance(rag_response, dict):
        result = dict(rag_response)
        result["chunks"] = chunks
        return result
    return chunks
