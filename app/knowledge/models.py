"""Immutable domain types for knowledge links."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

DocType = Literal["book", "paper", "note"]
ResolutionStatus = Literal["resolved", "broken", "ambiguous"]
RelationType = Literal["explicit", "suggested", "provenance"]
LinkSyntax = Literal["wikilink", "markdown", "frontmatter"]


@dataclass(frozen=True)
class Governance:
    status: str = "draft"
    reviewed_at: str | None = None
    sources: tuple[str, ...] = ()
    confidence: float | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["source"] = list(data.pop("sources"))
        return data


@dataclass(frozen=True)
class DocumentRef:
    doc_id: str
    doc_type: DocType
    slug: str
    title: str
    aliases: tuple[str, ...]
    governance: Governance
    source_files: tuple[str, ...]
    headings: tuple[tuple[str, str], ...] = ()
    preview: str = ""

    def heading_map(self) -> dict[str, str]:
        return dict(self.headings)


@dataclass(frozen=True)
class ParsedLink:
    raw: str
    target: str
    alias: str | None
    anchor: str | None
    line: int
    column: int
    syntax: LinkSyntax


@dataclass(frozen=True)
class ResolvedEdge:
    source_id: str
    target_id: str | None
    relation_type: RelationType
    syntax: LinkSyntax
    status: ResolutionStatus
    candidates: tuple[str, ...] = ()
    target_anchor: str | None = None
    target_node_id: str | None = None
    source_md: str = ""
    source_line: int = 0
    source_heading: str | None = None
    alias: str | None = None
    raw: str = ""
    excerpt: str = ""
    confidence: float | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["candidates"] = list(self.candidates)
        return data
