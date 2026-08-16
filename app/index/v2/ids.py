"""Cross-platform stable PageIndex v2 document and node identifiers."""

from __future__ import annotations

import re
from collections.abc import Sequence
import unicodedata

from .canonical import canonical_hash

__all__ = [
    "make_doc_key",
    "make_node_key",
    "normalize_relative_path",
]


_DOCUMENT_TYPES = frozenset({"book", "paper", "note"})
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:/")


def normalize_relative_path(path: str) -> str:
    """Normalize a portable relative path and reject root traversal."""
    if not isinstance(path, str):
        raise TypeError("path must be a string")
    normalized = unicodedata.normalize("NFC", path).replace("\\", "/")
    if "\x00" in normalized:
        raise ValueError("path cannot contain a NUL character")
    if normalized.startswith("/") or _WINDOWS_DRIVE.match(normalized):
        raise ValueError(f"path must be relative: {path!r}")

    parts: list[str] = []
    for part in normalized.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError(f"path cannot traverse its root: {path!r}")
        parts.append(part)
    if not parts:
        raise ValueError("path must identify a relative file")
    return "/".join(parts)


def make_doc_key(doc_type: str, slug: str) -> str:
    """Return the stable, type-namespaced identifier for one document."""
    if doc_type not in _DOCUMENT_TYPES:
        raise ValueError(f"unsupported document type: {doc_type!r}")
    if not isinstance(slug, str):
        raise TypeError("slug must be a string")
    normalized_slug = unicodedata.normalize("NFC", slug)
    if (
        not normalized_slug
        or normalized_slug != normalized_slug.strip()
        or normalized_slug in {".", ".."}
        or any(character in normalized_slug for character in ("/", "\\", ":", "\x00"))
    ):
        raise ValueError(f"invalid document slug: {slug!r}")
    return f"{doc_type}:{normalized_slug}"


def _normalize_heading(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("breadcrumb entries must be strings")
    return " ".join(unicodedata.normalize("NFC", value).split())


def make_node_key(
    doc_key: str,
    source_path: str,
    breadcrumb: Sequence[str],
    duplicate_ordinal: int,
) -> str:
    """Build a stable node key from its semantic source location."""
    if not isinstance(doc_key, str) or ":" not in doc_key:
        raise ValueError("doc_key must be a namespaced document key")
    if isinstance(breadcrumb, (str, bytes)) or not isinstance(breadcrumb, Sequence):
        raise TypeError("breadcrumb must be a sequence of strings")
    if (
        isinstance(duplicate_ordinal, bool)
        or not isinstance(duplicate_ordinal, int)
        or duplicate_ordinal < 0
    ):
        raise ValueError("duplicate_ordinal must be a non-negative integer")

    identity = {
        "doc_key": unicodedata.normalize("NFC", doc_key),
        "source_path": normalize_relative_path(source_path),
        "breadcrumb": [_normalize_heading(part) for part in breadcrumb],
        "duplicate_ordinal": duplicate_ordinal,
    }
    return f"n_{canonical_hash(identity)[:24]}"
