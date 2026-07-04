"""Local Obsidian vault indexing and liveness checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_VAULT_ROOT = Path("~/Obsidian/Ace Place").expanduser()


class VaultLivenessError(RuntimeError):
    """Raised when the real target vault is absent, wrong-kind, or empty."""


@dataclass(frozen=True, order=True)
class VaultNote:
    """A matched vault note, sorted by relative path for deterministic tiebreaks."""

    path: str
    mtime: datetime

    @property
    def mtime_iso(self) -> str:
        return self.mtime.isoformat().replace("+00:00", "Z")


def _is_dot_hidden(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def _note_from_path(vault_root: Path, path: Path) -> VaultNote:
    rel = path.relative_to(vault_root).as_posix()
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return VaultNote(path=rel, mtime=mtime)


def build_index(vault_root: str | Path) -> dict[str, list[VaultNote]]:
    """Build a case-folded filename-stem index of real Markdown notes.

    Only `*.md` files are indexed. Any file with a relative path segment beginning
    with `.` is skipped, so `.git`, `.obsidian`, `.smart-env`, and similar cache
    trees cannot produce false matches. Lists are sorted by full relative path to
    make collision tiebreaks deterministic on the same host/snapshot.
    """

    root = Path(vault_root).expanduser()
    index: dict[str, list[VaultNote]] = {}
    if not root.is_dir():
        return index

    for path in root.rglob("*.md"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if _is_dot_hidden(rel):
            continue
        key = path.stem.strip().casefold()
        index.setdefault(key, []).append(_note_from_path(root, path))

    for notes in index.values():
        notes.sort(key=lambda note: note.path)
    return dict(sorted(index.items(), key=lambda item: item[0]))


def check_vault(vault_root: str | Path, *, min_notes: int = 1) -> int:
    """Assert the real target vault exists, is a directory, and has notes.

    Returns the number of indexed Markdown notes. Raises VaultLivenessError with a
    loud, cron-friendly message if the vault is missing, not a directory, or empty
    after real-note scoping exclusions.
    """

    root = Path(vault_root).expanduser()
    if not root.exists():
        raise VaultLivenessError(f"VAULT LIVENESS FAILED: missing vault root: {root}")
    if not root.is_dir():
        raise VaultLivenessError(f"VAULT LIVENESS FAILED: vault root is not a directory: {root}")

    index = build_index(root)
    note_count = sum(len(notes) for notes in index.values())
    if note_count < min_notes:
        raise VaultLivenessError(
            f"VAULT LIVENESS FAILED: no real markdown notes found under {root} after exclusions"
        )
    return note_count
