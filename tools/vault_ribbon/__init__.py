"""Vault Ribbon: annotate RAG chunks with exact Obsidian filename-stem matches."""

from .annotate import MalformedRAGResponse, annotate, iter_chunks, parse_asof
from .index import DEFAULT_VAULT_ROOT, VaultLivenessError, VaultNote, build_index, check_vault

__all__ = [
    "DEFAULT_VAULT_ROOT",
    "MalformedRAGResponse",
    "VaultLivenessError",
    "VaultNote",
    "annotate",
    "build_index",
    "check_vault",
    "iter_chunks",
    "parse_asof",
]
