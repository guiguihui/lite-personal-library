from __future__ import annotations

import json
from pathlib import Path

from app.knowledge.indexer import build_link_index
from app.knowledge.queries import get_backlinks, get_neighborhood
from app.knowledge.wikilinks import normalize_lookup_key, parse_links


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_parser_skips_code_and_external_links() -> None:
    links = parse_links("[[note:ok|好]]\n`[[note:no]]`\n[站内](../notes/ok.md#标题)\n[外链](https://example.com)\n")
    assert [(item.target, item.anchor, item.syntax) for item in links] == [
        ("note:ok", None, "wikilink"),
        ("../notes/ok.md", "标题", "markdown"),
    ]
    assert normalize_lookup_key(" Ａ  B ") == "a b"


def test_build_queries_and_determinism(tmp_path: Path) -> None:
    content = tmp_path / "content"
    pageindex = tmp_path / "pageindex"
    _write(content / "notes" / "a.md", "---\nid: note:a\ntitle: A\nstatus: reviewed\n---\n# A\n[[paper:p|P]]\n")
    _write(content / "notes" / "b.md", "---\nid: note:b\ntitle: B\n---\n[[paper:p]]\n")
    _write(content / "papers" / "p" / "_index.md", "---\nid: paper:p\ntitle: Paper\n---\n# Paper\n[[note:a]]\n")
    pageindex.mkdir()

    index = build_link_index(content, pageindex)
    first = (pageindex / "link-index.json").read_bytes()
    build_link_index(content, pageindex)
    assert (pageindex / "link-index.json").read_bytes() == first
    assert len(index["edges"]) == 3
    assert isinstance(index["outgoing"]["note:a"][0], int)
    assert get_backlinks(index, "paper:p")["total"] == 2
    graph = get_neighborhood(index, "note:a", 40)
    assert {node["id"] for node in graph["nodes"]} == {"note:a", "paper:p"}
    assert graph["truncated"] is False
    assert json.loads(first)["schema_version"] == 1


def test_ambiguous_title_stays_out_of_graph(tmp_path: Path) -> None:
    content, pageindex = tmp_path / "content", tmp_path / "pageindex"
    _write(content / "notes" / "source.md", "---\ntitle: Source\n---\n[[Same]]\n")
    _write(content / "books" / "one" / "_index.md", "---\ntitle: Same\n---\n")
    _write(content / "papers" / "two" / "_index.md", "---\ntitle: Same\n---\n")
    pageindex.mkdir()
    index = build_link_index(content, pageindex)
    assert len(index["diagnostics"]["ambiguous"]) == 1
    assert index["incoming"]["book:one"] == []
    assert index["incoming"]["paper:two"] == []
