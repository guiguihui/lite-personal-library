"""Dry-run-first migration for immutable IDs and governance fields."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from .frontmatter import split_frontmatter


@dataclass(frozen=True)
class MigrationChange:
    path: str
    doc_id: str
    additions: tuple[tuple[str, object], ...]


def _documents(content_dir: Path):
    for kind in ("book", "paper"):
        root = content_dir / f"{kind}s"
        if not root.is_dir():
            continue
        for directory in sorted(path for path in root.iterdir() if path.is_dir()):
            meta = directory / "_index.md"
            if meta.is_file():
                yield kind, directory.name, meta
        if kind == "paper":
            for path in sorted(root.glob("*.md")):
                if path.name != "_index.md":
                    yield kind, path.stem, path
    notes = content_dir / "notes"
    if notes.is_dir():
        for path in sorted(notes.glob("*.md")):
            if path.name != "_index.md":
                yield "note", path.stem, path


def plan_migration(content_dir: Path | str) -> dict:
    root = Path(content_dir)
    changes: list[MigrationChange] = []
    conflicts: list[dict] = []
    seen: dict[str, str] = {}
    for kind, slug, path in _documents(root):
        data, _, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        doc_id = str(data.get("id") or f"{kind}:{slug}")
        if doc_id in seen:
            conflicts.append({"id": doc_id, "paths": [seen[doc_id], path.as_posix()]})
        seen[doc_id] = path.as_posix()
        additions = []
        defaults = {"id": doc_id, "status": "draft", "reviewed_at": None, "source": [], "confidence": None}
        for key, value in defaults.items():
            if key not in data:
                additions.append((key, value))
        if additions:
            changes.append(MigrationChange(path.relative_to(root).as_posix(), doc_id, tuple(additions)))
    return {"changes": [asdict(change) for change in changes], "conflicts": conflicts, "can_apply": not conflicts}


def apply_migration(content_dir: Path | str, backup_dir: Path | str) -> dict:
    root, backup = Path(content_dir), Path(backup_dir)
    plan = plan_migration(root)
    if not plan["can_apply"]:
        raise ValueError("migration has ID conflicts")
    applied = []
    for change in plan["changes"]:
        path = root / change["path"]
        relative = path.relative_to(root)
        backup_path = backup / relative
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_path)
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
        if closing is None:
            raise ValueError(f"missing frontmatter: {relative.as_posix()}")
        additions = []
        for key, value in change["additions"]:
            if value == []:
                encoded = "[]"
            elif value is None:
                encoded = "null"
            else:
                encoded = json.dumps(value, ensure_ascii=False)
            additions.append(f"{key}: {encoded}\n")
        lines[closing:closing] = additions
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text("".join(lines), encoding="utf-8", newline="\n")
        temp.replace(path)
        applied.append(relative.as_posix())
    manifest = {"content_dir": str(root.resolve()), "backup_dir": str(backup.resolve()), "applied": applied}
    backup.mkdir(parents=True, exist_ok=True)
    (backup / "migration-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest
