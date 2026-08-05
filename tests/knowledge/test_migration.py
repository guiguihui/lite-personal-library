from pathlib import Path

from app.knowledge.migration import apply_migration, plan_migration


def test_migration_is_dry_run_first_and_idempotent(tmp_path: Path) -> None:
    content = tmp_path / "content"
    note = content / "notes" / "hello.md"
    note.parent.mkdir(parents=True)
    original = "---\ntitle: Hello\n---\n\nBody\n"
    note.write_text(original, encoding="utf-8")
    plan = plan_migration(content)
    assert note.read_text(encoding="utf-8") == original
    assert plan["changes"][0]["doc_id"] == "note:hello"
    manifest = apply_migration(content, tmp_path / "backup")
    assert manifest["applied"] == ["notes/hello.md"]
    assert "id: \"note:hello\"" in note.read_text(encoding="utf-8")
    assert plan_migration(content)["changes"] == []
    assert (tmp_path / "backup" / "notes" / "hello.md").read_text(encoding="utf-8") == original
