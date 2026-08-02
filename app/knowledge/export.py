"""Export library content as generic Markdown for LLM consumers."""

from __future__ import annotations

import re
from pathlib import Path

from .catalog import build_catalog
from .models import ParsedLink
from .resolver import resolve_link

_WIKILINK = re.compile(r"(?<!!)\[\[([^\[\]\n]+)\]\]")


def _convert(text: str, source, catalog) -> tuple[str, list[dict]]:
    diagnostics: list[dict] = []

    def replace(match: re.Match[str]) -> str:
        body = match.group(1)
        target_part, separator, alias = body.partition("|")
        target, anchor_separator, anchor = target_part.partition("#")
        link = ParsedLink(match.group(0), target.strip(), alias.strip() if separator else None, anchor.strip() if anchor_separator else None, 0, 0, "wikilink")
        edge = resolve_link(source, link, catalog)
        if edge.status != "resolved" or not edge.target_id:
            diagnostics.append({"source_id": source.doc_id, "raw": match.group(0), "status": edge.status, "candidates": list(edge.candidates)})
            return match.group(0)
        label = link.alias or catalog[edge.target_id].title
        fragment = f"#{link.anchor}" if link.anchor else ""
        return f"[{label}](lqd://{edge.target_id}{fragment} \"{edge.target_id}\")"

    return _WIKILINK.sub(replace, text), diagnostics


def export_llm_context(content_dir: Path | str, pageindex_dir: Path | str, output_dir: Path | str) -> dict:
    content_root, output_root = Path(content_dir), Path(output_dir)
    catalog = build_catalog(content_root, Path(pageindex_dir))
    output_root.mkdir(parents=True, exist_ok=True)
    diagnostics: list[dict] = []
    outputs = []
    for kind in ("book", "paper", "note"):
        sections = []
        for ref in sorted((item for item in catalog.values() if item.doc_type == kind), key=lambda item: item.doc_id):
            body_parts = []
            for source_file in ref.source_files:
                converted, issues = _convert(Path(source_file).read_text(encoding="utf-8"), ref, catalog)
                diagnostics.extend(issues)
                body_parts.append(converted)
            sections.append(f"<!-- id: {ref.doc_id} -->\n\n" + "\n\n".join(body_parts))
        name = f"{kind}s.md"
        (output_root / name).write_text("\n\n---\n\n".join(sections) + ("\n" if sections else ""), encoding="utf-8")
        outputs.append(name)
    llms = "# LQ-D Library\n\n" + "\n".join(f"- [{name}]({name})" for name in outputs) + "\n"
    (output_root / "llms.txt").write_text(llms, encoding="utf-8")
    return {"outputs": ["llms.txt", *outputs], "diagnostics": diagnostics}
