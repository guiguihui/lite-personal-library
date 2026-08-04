"""Pinned Library projections over authenticated PageIndex V3 Segments."""

from __future__ import annotations

import copy

from app.index.v3.reader import PinnedSearchView
from app.index.v3.segment_projection import DocumentProjection


class LibraryV3Error(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def detail(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "retryable": self.code == "VIEW_CHANGED"}


class LibraryV3Service:
    """Serve list/read authorization from one immutable V3 view pin."""

    def __init__(self, view: PinnedSearchView) -> None:
        self.view = view

    @property
    def pin(self) -> dict[str, str]:
        return self.view.pin.as_dict()

    def require_pin(self, generation: str | None, view_id: str | None) -> None:
        if generation is None and view_id is None:
            return
        if generation != self.view.pin.generation or view_id != self.view.pin.view_id:
            raise LibraryV3Error("VIEW_CHANGED", "知识库索引已更新，请刷新后重试")

    def _find(self, doc_type: str, slug: str) -> DocumentProjection:
        doc_key = f"{doc_type}:{slug}"
        owners = self.view.documents()
        uid = next((candidate for candidate, owner in owners.items() if owner.doc_key == doc_key), None)
        if uid is None:
            raise LibraryV3Error("DOCUMENT_NOT_FOUND", f"找不到文档：{doc_key}", status_code=404)
        return self.view.get_document_projections((uid,))[0]

    def list_documents(self, doc_type: str) -> dict[str, object]:
        projections = self.view.get_document_projections()
        docs: list[dict[str, object]] = []
        for projection in projections:
            document = copy.deepcopy(dict(projection.document))
            if document.get("type") != doc_type:
                continue
            document.update(
                {
                    "doc_uid": projection.doc_uid,
                    "doc_key": projection.doc_key,
                    "segment_hash": projection.segment_hash,
                }
            )
            docs.append(document)
        docs.sort(key=lambda item: (str(item.get("title", "")).casefold(), str(item.get("id", ""))))
        return {"type": doc_type, "docs": docs, **self.pin}

    def read_document(
        self,
        doc_type: str,
        slug: str,
        *,
        generation: str | None = None,
        view_id: str | None = None,
    ) -> dict[str, object]:
        self.require_pin(generation, view_id)
        projection = self._find(doc_type, slug)
        payload = copy.deepcopy(dict(projection.document_tree))
        payload.update(
            {
                "doc_uid": projection.doc_uid,
                "doc_key": projection.doc_key,
                "segment_hash": projection.segment_hash,
                "source_files": [dict(item) for item in projection.source_files],
                **self.pin,
            }
        )
        return payload

    def authorize_source(
        self,
        doc_type: str,
        slug: str,
        source_md: str,
        *,
        generation: str | None = None,
        view_id: str | None = None,
    ) -> str:
        self.require_pin(generation, view_id)
        projection = self._find(doc_type, slug)
        relative = source_md.replace("\\", "/")
        if relative.startswith("content/"):
            relative = relative[len("content/") :]
        allowed = {str(item["path"]) for item in projection.source_files}
        if relative not in allowed:
            raise LibraryV3Error("CONTENT_OUT_OF_SYNC", "正文文件不属于当前 V3 文档快照")
        return relative
