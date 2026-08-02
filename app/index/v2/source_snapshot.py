"""Bounded-parallel, single-read stable source proof capture."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .canonical import canonical_hash
from .ids import make_doc_key, normalize_relative_path
from .catalog import DocumentSource, discover_documents
from .input_proof import proof_from_fingerprints, validate_input_proof


SOURCE_HASH_WORKERS = 4
_READ_BUFFER_BYTES = 1024 * 1024

_FileState = tuple[str, str, int, int, int, int, int]
_DirectoryState = tuple[tuple[str, int, int, int, int], ...]
_Topology = tuple[tuple[str, tuple[str, ...]], ...]


class _SourceChanged(RuntimeError):
    """A source changed while its already-open handle was being hashed."""


@dataclass(frozen=True, slots=True)
class _PreparedFile:
    relative: str
    path: Path


@dataclass(frozen=True, slots=True)
class _PreparedSource:
    doc_key: str
    files: tuple[_PreparedFile, ...]


@dataclass(frozen=True, slots=True)
class StableCatalogSnapshot:
    """One content-hashed catalog plus a reusable metadata stability envelope."""

    content_dir: Path
    sources: tuple[DocumentSource, ...]
    proof: dict[str, object]
    directory_state: _DirectoryState
    topology: _Topology
    file_state: tuple[_FileState, ...]
    _proof_sha256: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "content_dir", Path(self.content_dir).resolve())
        object.__setattr__(self, "sources", tuple(self.sources))
        validated_proof = validate_input_proof(self.proof)
        object.__setattr__(self, "proof", validated_proof)
        object.__setattr__(self, "_proof_sha256", canonical_hash(validated_proof))
        object.__setattr__(self, "directory_state", tuple(self.directory_state))
        object.__setattr__(self, "topology", tuple(self.topology))
        object.__setattr__(self, "file_state", tuple(self.file_state))

    def validated_proof(self) -> dict[str, object]:
        """Return a detached proof and reject mutation after capture."""

        if canonical_hash(self.proof) != self._proof_sha256:
            raise RuntimeError("stable catalog snapshot proof was mutated")
        return validate_input_proof(self.proof)

    def verify_unchanged(
        self,
        check_cancel: Callable[[], None] = lambda: None,
    ) -> bool:
        """Check topology and file identity without hashing source contents."""

        if not callable(check_cancel):
            raise TypeError("check_cancel must be callable")
        check_cancel()
        try:
            prepared_sources = _prepare_sources(self.content_dir, self.sources)
            with ThreadPoolExecutor(
                max_workers=3,
                thread_name_prefix="pageindex-source-verify",
            ) as executor:
                directory_future = executor.submit(
                    _catalog_directory_state,
                    self.content_dir,
                )
                topology_future = executor.submit(
                    _rescan_catalog_topology,
                    self.content_dir,
                )
                state_future = executor.submit(_file_state, prepared_sources)
                directory_state = directory_future.result()
                topology = topology_future.result()
                file_state = state_future.result()
        except (FileNotFoundError, NotADirectoryError, _SourceChanged):
            return False
        check_cancel()
        return (
            directory_state == self.directory_state
            and topology == self.topology
            and file_state == self.file_state
        )


def _catalog_directory_state(root: Path) -> _DirectoryState:
    directories = [root]
    for name in ("books", "papers", "notes"):
        category = root / name
        if not category.is_dir():
            continue
        directories.append(category)
        if name in {"books", "papers"}:
            directories.extend(
                path
                for path in category.iterdir()
                if path.is_dir()
            )
    records: list[tuple[str, int, int, int, int]] = []
    for path in sorted(set(directories), key=lambda value: value.as_posix()):
        metadata = path.stat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise FileNotFoundError(path)
        relative = "." if path == root else path.relative_to(root).as_posix()
        records.append(
            (
                relative,
                int(metadata.st_mtime_ns),
                int(metadata.st_ctime_ns),
                int(metadata.st_ino),
                int(metadata.st_dev),
            )
        )
    return tuple(records)


def _source_topology(sources: Sequence[DocumentSource]) -> _Topology:
    return tuple(
        sorted(
            (
                source.doc_key,
                tuple(Path(relative).as_posix() for relative in source.files),
            )
            for source in sources
        )
    )


def _sorted_entry_names(directory: Path, *, directories: bool) -> tuple[str, ...]:
    with os.scandir(directory) as entries:
        names = [
            entry.name
            for entry in entries
            if (
                entry.is_dir(follow_symlinks=True)
                if directories
                else entry.is_file(follow_symlinks=True)
            )
        ]
    return tuple(sorted(names, key=normalize_relative_path))


def _rescan_catalog_topology(root: Path) -> _Topology:
    """Rediscover supported source topology without per-entry stat calls."""

    result: list[tuple[str, tuple[str, ...]]] = []

    books_root = root / "books"
    if books_root.is_dir():
        for directory_name in _sorted_entry_names(
            books_root,
            directories=True,
        ):
            if directory_name.startswith("_"):
                continue
            directory = books_root / directory_name
            files = _sorted_entry_names(directory, directories=False)
            if "_index.md" not in files:
                continue
            chapters = tuple(
                name
                for name in files
                if name.endswith(".md") and name != "_index.md"
            )
            result.append(
                (
                    make_doc_key("book", directory_name),
                    (
                        f"books/{directory_name}/_index.md",
                        *(
                            f"books/{directory_name}/{chapter}"
                            for chapter in chapters
                        ),
                    ),
                )
            )

    papers_root = root / "papers"
    if papers_root.is_dir():
        for directory_name in _sorted_entry_names(
            papers_root,
            directories=True,
        ):
            if directory_name.startswith("_"):
                continue
            directory = papers_root / directory_name
            files = _sorted_entry_names(directory, directories=False)
            if "_index.md" not in files:
                continue
            result.append(
                (
                    make_doc_key("paper", directory_name),
                    (f"papers/{directory_name}/_index.md",),
                )
            )

    notes_root = root / "notes"
    if notes_root.is_dir():
        files = _sorted_entry_names(notes_root, directories=False)
        for name in files:
            if not name.endswith(".md") or name == "_index.md":
                continue
            result.append(
                (
                    make_doc_key("note", Path(name).stem),
                    (f"notes/{name}",),
                )
            )

    seen: set[str] = set()
    for doc_key, _files in result:
        if doc_key in seen:
            raise ValueError(f"duplicate document key during topology scan: {doc_key}")
        seen.add(doc_key)
    return tuple(sorted(result))


def _assert_within_root(path: Path, root: Path, original: Path) -> Path:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"source file escapes content root: {original}") from exc
    return path


def _prepare_sources(
    root: Path,
    sources: Sequence[DocumentSource],
) -> tuple[_PreparedSource, ...]:
    """Resolve every unique parent once and reject ancestor path escapes."""

    parent_cache: dict[Path, tuple[Path, frozenset[str]]] = {}
    prepared: list[_PreparedSource] = []
    for source in sources:
        files: list[_PreparedFile] = []
        for raw_relative in source.files:
            relative_path = Path(raw_relative)
            relative = normalize_relative_path(relative_path.as_posix())
            raw_parent = relative_path.parent
            cached_parent = parent_cache.get(raw_parent)
            if cached_parent is None:
                unresolved_parent = root / raw_parent
                resolved_parent = _assert_within_root(
                    unresolved_parent.resolve(strict=True),
                    root,
                    unresolved_parent,
                )
                with os.scandir(resolved_parent) as entries:
                    symlink_names = frozenset(
                        entry.name for entry in entries if entry.is_symlink()
                    )
                parent_cache[raw_parent] = (resolved_parent, symlink_names)
            else:
                resolved_parent, symlink_names = cached_parent

            unresolved = resolved_parent / relative_path.name
            resolved = unresolved
            if relative_path.name in symlink_names:
                resolved = _assert_within_root(
                    unresolved.resolve(strict=True),
                    root,
                    root / relative_path,
                )
            files.append(_PreparedFile(relative=relative, path=resolved))
        prepared.append(_PreparedSource(doc_key=source.doc_key, files=tuple(files)))
    return tuple(prepared)


def _metadata_state(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(metadata.st_mode),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
        int(metadata.st_ino),
        int(metadata.st_dev),
    )


def _file_state(sources: Sequence[_PreparedSource]) -> tuple[_FileState, ...]:
    result: list[_FileState] = []
    for source in sources:
        for prepared_file in source.files:
            metadata = prepared_file.path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                raise FileNotFoundError(prepared_file.path)
            result.append(
                (
                    source.doc_key,
                    prepared_file.relative,
                    int(metadata.st_size),
                    int(metadata.st_mtime_ns),
                    int(metadata.st_ctime_ns),
                    int(metadata.st_ino),
                    int(metadata.st_dev),
                )
            )
    return tuple(result)


def _hash_open_file(
    path: Path,
    check_cancel: Callable[[], None],
) -> tuple[str, os.stat_result]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise FileNotFoundError(path)
        remaining = int(before.st_size)
        while remaining:
            payload = stream.read(min(_READ_BUFFER_BYTES, remaining))
            if not payload:
                raise _SourceChanged(path)
            digest.update(payload)
            remaining -= len(payload)
            check_cancel()
        after = os.fstat(stream.fileno())
    if _metadata_state(after) != _metadata_state(before):
        raise _SourceChanged(path)
    return digest.hexdigest(), after


def _fingerprint_source(
    source: _PreparedSource,
    check_cancel: Callable[[], None] = lambda: None,
) -> tuple[str, tuple[_FileState, ...]]:
    records: list[dict[str, str]] = []
    states: list[_FileState] = []
    for prepared_file in source.files:
        digest, metadata = _hash_open_file(prepared_file.path, check_cancel)
        records.append(
            {
                "path": prepared_file.relative,
                "sha256": digest,
            }
        )
        states.append(
            (
                source.doc_key,
                prepared_file.relative,
                int(metadata.st_size),
                int(metadata.st_mtime_ns),
                int(metadata.st_ctime_ns),
                int(metadata.st_ino),
                int(metadata.st_dev),
            )
        )
    return canonical_hash(records), tuple(states)


def capture_stable_catalog(
    content_dir: Path,
    *,
    segment_recipe_hash: str,
    compiler_recipe_hash: str,
    check_cancel: Callable[[], None],
    max_workers: int = SOURCE_HASH_WORKERS,
) -> StableCatalogSnapshot | None:
    """Hash every source once and return its reusable stability envelope.

    A None result means source topology or file identity changed during
    capture. Later verification compares only stat and topology facts.
    """

    if (
        isinstance(max_workers, bool)
        or not isinstance(max_workers, int)
        or max_workers < 1
    ):
        raise ValueError("max_workers must be an integer >= 1")
    if not callable(check_cancel):
        raise TypeError("check_cancel must be callable")
    root = Path(content_dir).resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)

    check_cancel()
    try:
        directory_state_before = _catalog_directory_state(root)
        sources = discover_documents(root)
        topology_before = _source_topology(sources)
        prepared_sources = _prepare_sources(root, sources)

        def fingerprint(
            source: _PreparedSource,
        ) -> tuple[str, str, tuple[_FileState, ...]]:
            check_cancel()
            source_fingerprint, source_states = _fingerprint_source(
                source,
                check_cancel,
            )
            return source.doc_key, source_fingerprint, source_states

        if prepared_sources:
            workers = min(max_workers, len(prepared_sources))
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="pageindex-source-hash",
            ) as executor:
                pairs = list(executor.map(fingerprint, prepared_sources))
        else:
            pairs = []
        state_at_hash = tuple(
            state
            for _doc_key, _fingerprint, source_states in pairs
            for state in source_states
        )
    except (FileNotFoundError, NotADirectoryError, _SourceChanged):
        return None

    fingerprints = {
        doc_key: fingerprint_value
        for doc_key, fingerprint_value, _source_states in pairs
    }
    if len(fingerprints) != len(pairs):
        raise ValueError("duplicate document key during source proof capture")
    proof = proof_from_fingerprints(
        fingerprints,
        segment_recipe_hash,
        compiler_recipe_hash,
    )
    snapshot = StableCatalogSnapshot(
        content_dir=root,
        sources=tuple(sources),
        proof=proof,
        directory_state=directory_state_before,
        topology=topology_before,
        file_state=state_at_hash,
    )
    if not snapshot.verify_unchanged(check_cancel):
        return None
    return snapshot


def capture_stable_input_proof(
    content_dir: Path,
    *,
    segment_recipe_hash: str,
    compiler_recipe_hash: str,
    check_cancel: Callable[[], None],
    max_workers: int = SOURCE_HASH_WORKERS,
) -> dict[str, object] | None:
    """Compatibility wrapper returning the unchanged v2 proof payload."""

    snapshot = capture_stable_catalog(
        content_dir,
        segment_recipe_hash=segment_recipe_hash,
        compiler_recipe_hash=compiler_recipe_hash,
        check_cancel=check_cancel,
        max_workers=max_workers,
    )
    return None if snapshot is None else snapshot.validated_proof()

__all__ = [
    "SOURCE_HASH_WORKERS",
    "StableCatalogSnapshot",
    "capture_stable_catalog",
    "capture_stable_input_proof",
]
