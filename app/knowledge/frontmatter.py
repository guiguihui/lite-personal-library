"""Frontmatter parsing and governance validation."""

from __future__ import annotations

from typing import Any

import yaml

from .models import Governance

VALID_DOC_TYPES = {"book", "paper", "note"}
VALID_STATUSES = {"draft", "reviewed", "archived"}


def split_frontmatter(text: str) -> tuple[dict[str, Any], str, int]:
    if not text.startswith("---"):
        return {}, text, 0
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text, 0
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            data = yaml.safe_load("".join(lines[1:index])) or {}
            if not isinstance(data, dict):
                raise ValueError("frontmatter must be a mapping")
            return data, "".join(lines[index + 1 :]), index + 1
    raise ValueError("unterminated frontmatter")


def canonical_id(doc_type: str, slug: str, value: object = None) -> str:
    expected_prefix = f"{doc_type}:"
    candidate = str(value or f"{expected_prefix}{slug}").strip()
    if doc_type not in VALID_DOC_TYPES or not candidate.startswith(expected_prefix):
        raise ValueError(f"invalid document id: {candidate}")
    suffix = candidate[len(expected_prefix) :]
    if not suffix or any(ch.isspace() for ch in suffix):
        raise ValueError(f"invalid document id: {candidate}")
    return candidate


def parse_aliases(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    values = value if isinstance(value, list) else [value]
    return tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def parse_governance(data: dict[str, Any]) -> Governance:
    status = str(data.get("status") or "draft").strip()
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status}")
    reviewed = data.get("reviewed_at")
    reviewed_at = reviewed.isoformat() if hasattr(reviewed, "isoformat") else (
        str(reviewed).strip() if reviewed else None
    )
    raw_sources = data.get("source", [])
    sources = raw_sources if isinstance(raw_sources, list) else [raw_sources]
    confidence = data.get("confidence")
    if confidence is not None:
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
    return Governance(
        status=status,
        reviewed_at=reviewed_at,
        sources=tuple(str(item).strip() for item in sources if str(item).strip()),
        confidence=confidence,
    )
