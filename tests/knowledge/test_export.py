from pathlib import Path

from app.knowledge.export import export_llm_context


def test_export_converts_resolved_wikilinks(tmp_path: Path) -> None:
    content, pageindex, output = tmp_path / "content", tmp_path / "pageindex", tmp_path / "out"
    note = content / "notes" / "a.md"
    paper = content / "papers" / "p" / "_index.md"
    note.parent.mkdir(parents=True)
    paper.parent.mkdir(parents=True)
    pageindex.mkdir()
    note.write_text("---\nid: note:a\ntitle: A\n---\n[[paper:p|Paper]] [[missing]]\n", encoding="utf-8")
    paper.write_text("---\nid: paper:p\ntitle: P\n---\nText\n", encoding="utf-8")
    result = export_llm_context(content, pageindex, output)
    exported = (output / "notes.md").read_text(encoding="utf-8")
    assert '[Paper](lqd://paper:p "paper:p")' in exported
    assert "[[missing]]" in exported
    assert result["diagnostics"][0]["status"] == "broken"
    assert (output / "llms.txt").is_file()
