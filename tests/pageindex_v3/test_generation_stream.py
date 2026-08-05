from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.index.v2.artifacts import ArtifactRef
from app.index.v2.canonical import canonical_bytes, canonical_hash
from app.index.v2.object_store import StoredSegmentRef
import app.index.v2.streaming_json as streaming_json_module
import app.index.v3.generation as generation_module
import app.index.v3.generation_stream as generation_stream_module
from app.index.v3.generation import (
    LogicalGenerationError,
    LogicalGenerationReceipt,
    build_logical_generation,
)
from app.index.v3.generation_stream import validate_generation_stream
from app.index.v3.models import GenerationRecipe


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _stored_ref(pageindex: Path, doc_key: str) -> StoredSegmentRef:
    payload = f"opaque Segment for {doc_key}".encode("utf-8")
    segment_hash = hashlib.sha256(payload).hexdigest()
    path = (
        pageindex
        / "objects"
        / "segments"
        / segment_hash[:2]
        / f"{segment_hash}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    doc_type, slug = doc_key.split(":", 1)
    return StoredSegmentRef(
        segment_hash=segment_hash,
        path=path,
        byte_size=len(payload),
        doc_key=doc_key,
        doc_type=doc_type,
        slug=slug,
        content_hash=_digest(f"content:{doc_key}"),
        segment_recipe_hash=_digest("segment-recipe"),
    )


def _proof(
    refs: tuple[StoredSegmentRef, ...],
    recipe: GenerationRecipe,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "compiler_recipe_hash": canonical_hash(recipe.as_dict()),
        "documents": {
            ref.doc_key: {
                "content_hash": ref.content_hash,
                "segment_recipe_hash": ref.segment_recipe_hash,
            }
            for ref in reversed(refs)
        },
    }


def _generation(
    tmp_path: Path,
) -> tuple[Path, tuple[StoredSegmentRef, ...], LogicalGenerationReceipt]:
    pageindex = tmp_path / "pageindex"
    refs = (
        _stored_ref(pageindex, 'note:中文🙂"quoted"'),
        _stored_ref(pageindex, "book:alpha"),
        _stored_ref(pageindex, "note:zeta"),
    )
    recipe = GenerationRecipe(body_df_min=17)
    receipt = build_logical_generation(
        reversed(refs),
        _proof(refs, recipe),
        recipe,
        tmp_path / "generation",
    )
    return pageindex, refs, receipt


def test_validate_generation_stream_collects_at_most_one_ref_map(
    tmp_path: Path,
) -> None:
    pageindex, refs, receipt = _generation(tmp_path)

    assert validate_generation_stream(
        receipt,
        pageindex,
        check_cancelled=lambda: None,
    ) == {}
    recipes: list[GenerationRecipe] = []
    collected = validate_generation_stream(
        receipt,
        pageindex,
        check_cancelled=lambda: None,
        collect_refs=True,
        recipe_observer=recipes.append,
    )

    assert tuple(collected) == tuple(sorted(ref.doc_key for ref in refs))
    assert collected == {
        ref.doc_key: ref for ref in sorted(refs, key=lambda ref: ref.doc_key)
    }
    assert recipes == [GenerationRecipe(body_df_min=17)]


def test_validate_generation_stream_does_not_call_whole_document_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex, _refs, receipt = _generation(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("whole-document Generation helper was called")

    monkeypatch.setattr(
        streaming_json_module,
        "load_bounded_canonical_json",
        forbidden,
    )
    monkeypatch.setattr(
        generation_module,
        "validate_logical_generation_inputs",
        forbidden,
    )
    monkeypatch.setattr(Path, "read_bytes", forbidden)

    assert validate_generation_stream(
        receipt,
        pageindex,
        check_cancelled=lambda: None,
    ) == {}


def test_validate_generation_stream_handles_unicode_across_tiny_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex, refs, receipt = _generation(tmp_path)
    monkeypatch.setattr(generation_stream_module, "_STREAM_CHUNK_SIZE", 3)

    collected = validate_generation_stream(
        receipt,
        pageindex,
        check_cancelled=lambda: None,
        collect_refs=True,
    )

    assert set(collected) == {ref.doc_key for ref in refs}


def _artifact_ref(path: Path, relative_path: str, records: int) -> ArtifactRef:
    payload = path.read_bytes()
    return ArtifactRef(
        relative_path=relative_path,
        sha256=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
        records=records,
    )


@pytest.mark.parametrize(
    "field",
    ("manifest_ref", "input_proof_ref"),
)
def test_validate_generation_stream_rejects_artifact_hash_mismatch(
    tmp_path: Path,
    field: str,
) -> None:
    pageindex, _refs, receipt = _generation(tmp_path)
    original = getattr(receipt, field)
    bad_ref = replace(original, sha256="0" * 64)
    bad_receipt = replace(receipt, **{field: bad_ref})

    with pytest.raises(LogicalGenerationError, match="hash|mismatch"):
        validate_generation_stream(
            bad_receipt,
            pageindex,
            check_cancelled=lambda: None,
        )


@pytest.mark.parametrize("field", ("manifest_ref", "input_proof_ref"))
def test_validate_generation_stream_rejects_artifact_size_mismatch(
    tmp_path: Path,
    field: str,
) -> None:
    pageindex, _refs, receipt = _generation(tmp_path)
    original = getattr(receipt, field)
    bad_receipt = replace(
        receipt,
        **{field: replace(original, byte_size=original.byte_size + 1)},
    )

    with pytest.raises(LogicalGenerationError, match="size"):
        validate_generation_stream(
            bad_receipt,
            pageindex,
            check_cancelled=lambda: None,
        )


def test_validate_generation_stream_rejects_noncanonical_document(
    tmp_path: Path,
) -> None:
    pageindex, _refs, receipt = _generation(tmp_path)
    manifest_path = receipt.candidate_dir / "manifest.json"
    with manifest_path.open("ab") as stream:
        stream.write(b" ")
    rebound_receipt = replace(
        receipt,
        manifest_ref=_artifact_ref(
            manifest_path,
            "manifest.json",
            receipt.document_count,
        ),
    )

    with pytest.raises(LogicalGenerationError, match="trailing|canonical|invalid"):
        validate_generation_stream(
            rebound_receipt,
            pageindex,
            check_cancelled=lambda: None,
        )


def test_validate_generation_stream_rejects_manifest_proof_mismatch(
    tmp_path: Path,
) -> None:
    pageindex, _refs, receipt = _generation(tmp_path)
    proof_path = receipt.candidate_dir / "input-proof.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    first_key = next(iter(proof["documents"]))
    proof["documents"][first_key]["content_hash"] = _digest("replacement-content")
    proof_path.write_bytes(canonical_bytes(proof))
    rebound_receipt = replace(
        receipt,
        input_proof_ref=_artifact_ref(
            proof_path,
            "input-proof.json",
            receipt.document_count,
        ),
    )

    with pytest.raises(LogicalGenerationError, match="attestation|mismatch"):
        validate_generation_stream(
            rebound_receipt,
            pageindex,
            check_cancelled=lambda: None,
        )


def test_validate_generation_stream_propagates_chunk_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex, _refs, receipt = _generation(tmp_path)
    monkeypatch.setattr(generation_stream_module, "_STREAM_CHUNK_SIZE", 3)

    cancellation = RuntimeError("cancelled in observer")
    calls = 0

    def check_cancelled() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise cancellation

    with pytest.raises(RuntimeError) as captured:
        validate_generation_stream(
            receipt,
            pageindex,
            check_cancelled=check_cancelled,
        )

    assert captured.value is cancellation
    assert calls == 2


def test_validate_generation_stream_does_not_reclassify_parser_typed_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pageindex, _refs, receipt = _generation(tmp_path)
    prefix = b'{"artifact_kind":"logical_generation","document_count":'
    monkeypatch.setattr(generation_stream_module, "_STREAM_CHUNK_SIZE", len(prefix))

    cancellation = streaming_json_module.BoundedJsonError("cancelled while reading")
    calls = 0

    def check_cancelled() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise cancellation

    with pytest.raises(streaming_json_module.BoundedJsonError) as captured:
        validate_generation_stream(
            receipt,
            pageindex,
            check_cancelled=check_cancelled,
        )

    assert captured.value is cancellation


def test_validate_generation_stream_supports_empty_generation(
    tmp_path: Path,
) -> None:
    recipe = GenerationRecipe()
    receipt = build_logical_generation(
        (),
        _proof((), recipe),
        recipe,
        tmp_path / "empty-generation",
    )

    assert validate_generation_stream(
        receipt,
        tmp_path / "pageindex-does-not-need-an-object-store",
        check_cancelled=lambda: None,
        collect_refs=True,
    ) == {}


def test_validate_generation_stream_requires_exact_candidate_file_set(
    tmp_path: Path,
) -> None:
    pageindex, _refs, receipt = _generation(tmp_path)
    (receipt.candidate_dir / "unexpected.txt").write_text(
        "extra",
        encoding="utf-8",
    )

    with pytest.raises(LogicalGenerationError, match="file set"):
        validate_generation_stream(
            receipt,
            pageindex,
            check_cancelled=lambda: None,
        )
