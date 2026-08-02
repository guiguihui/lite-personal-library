"""Parse Wikilinks and ordinary internal Markdown links."""

from __future__ import annotations

import re
import unicodedata

from .models import ParsedLink

_WIKILINK = re.compile(r"(?<!!)\[\[([^\[\]\n]+)\]\]")
_MARKDOWN_LINK = re.compile(r"(?<!!)\[([^\]\n]+)\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def normalize_lookup_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().casefold()
    return " ".join(value.split())


def _split_target(value: str) -> tuple[str, str | None, str | None]:
    target_part, separator, alias = value.partition("|")
    target, anchor_separator, anchor = target_part.partition("#")
    return target.strip(), alias.strip() if separator and alias.strip() else None, anchor.strip() if anchor_separator and anchor.strip() else None


def _is_internal_markdown_target(target: str) -> bool:
    lowered = target.casefold()
    return not (lowered.startswith(("http://", "https://", "mailto:", "tel:", "data:", "javascript:")) or target.startswith("#"))


def parse_links(markdown: str) -> tuple[ParsedLink, ...]:
    results: list[ParsedLink] = []
    fenced_by: str | None = None
    for line_number, line in enumerate(markdown.splitlines(keepends=True), 1):
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)[0]
            fenced_by = None if fenced_by == marker else (marker if fenced_by is None else fenced_by)
            continue
        if fenced_by:
            continue
        masked = list(line)
        for code in re.finditer(r"(`+)(.*?)\1", line):
            masked[code.start() : code.end()] = " " * (code.end() - code.start())
        visible = "".join(masked)
        for match in _WIKILINK.finditer(visible):
            target, alias, anchor = _split_target(match.group(1))
            if target:
                results.append(ParsedLink(line[match.start():match.end()], target, alias, anchor, line_number, match.start() + 1, "wikilink"))
        for match in _MARKDOWN_LINK.finditer(visible):
            target = match.group(2).strip()
            if _is_internal_markdown_target(target):
                path, _, anchor = target.partition("#")
                results.append(ParsedLink(line[match.start():match.end()], path, match.group(1).strip(), anchor or None, line_number, match.start() + 1, "markdown"))
    return tuple(sorted(results, key=lambda item: (item.line, item.column)))
