"""MinerU HTTP API 提取后端(httpx,高质量,需 API key + 网络)。

按 docs/architecture.md L165 设计:走 HTTP API(httpx 客户端),
非本地 subprocess 调 CLI。需要 MinerU API key + 网络。

API 约定(基于 MinerU 云服务典型接口):
  POST {base_url}/api/v1/extract
    multipart: file=<pdf bytes>
    json: {pages: "1-50" | null}
    → {task_id}
  GET {base_url}/api/v1/tasks/{task_id}
    → {status: "pending"|"running"|"done"|"failed", result: {markdown, images: [{name, data}]}}
  GET {base_url}/api/v1/tasks/{task_id}/download
    → zip(merged/book.md + images/)

实际 API 形态可能不同,此处给出可适配的骨架,通过环境变量
MINERU_BASE_URL / MINERU_API_KEY 配置。未配置时返回 ok=False。
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from app.pdf.base import ExtractResult

# 轮询参数
_POLL_INTERVAL_SEC = 5.0
_POLL_TIMEOUT_SEC = 1800.0  # 30 分钟


def _resolve_config() -> tuple[str, str]:
    """从环境变量读 MinerU API 配置。返回 (base_url, api_key)。"""
    import os

    base = os.environ.get("MINERU_BASE_URL", "https://mineru.net/api/v1")
    key = os.environ.get("MINERU_API_KEY", "")
    return base, key


class MineruExtractor:
    """MinerU HTTP API 提取器。高质量,需 API key + 网络。"""

    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        b, k = _resolve_config()
        self._base_url = base_url or b
        self._api_key = api_key or k

    def extract(
        self,
        input_path: Path,
        out_dir: Path,
        pages: str | None = None,
    ) -> ExtractResult:
        """提交 PDF 到 MinerU API,轮询任务,下载产物到 out_dir。"""
        start = time.time()
        log: list[str] = []
        merged_path = out_dir / "merged" / "book.md"
        images_dir = out_dir / "images"

        if not self._api_key:
            log.append("[error] MINERU_API_KEY not set")
            return ExtractResult(
                ok=False,
                source_format="pdf",
                merged_path=merged_path,
                images_dir=images_dir,
                error="MINERU_API_KEY not configured",
                duration_sec=time.time() - start,
                log=tuple(log),
            )

        try:
            import httpx  # type: ignore
        except ImportError as exc:
            return ExtractResult(
                ok=False,
                source_format="pdf",
                merged_path=merged_path,
                images_dir=images_dir,
                error=f"httpx not installed: {exc}",
                duration_sec=time.time() - start,
                log=tuple(log),
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "merged").mkdir(parents=True, exist_ok=True)
        images_dir.mkdir(parents=True, exist_ok=True)

        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            with httpx.Client(timeout=60.0) as client:
                task_id = self._submit(client, input_path, pages, headers, log)
                if task_id is None:
                    return self._fail(start, log, merged_path, images_dir, "submit failed")

                result = self._poll(client, task_id, headers, log, start)
                if result is None:
                    return self._fail(start, log, merged_path, images_dir, "poll failed/timeout")

                self._download(client, task_id, result, out_dir, merged_path, images_dir, headers, log)
        except Exception as exc:
            log.append(f"[error] {type(exc).__name__}: {exc}")
            return self._fail(start, log, merged_path, images_dir, f"{type(exc).__name__}: {exc}")

        log.append(f"[done] merged={merged_path}")
        return ExtractResult(
            ok=True,
            source_format="pdf",
            merged_path=merged_path,
            images_dir=images_dir,
            title=result.get("title", "") if isinstance(result, dict) else "",
            author=result.get("author", "") if isinstance(result, dict) else "",
            page_count=int(result.get("page_count", 0)) if isinstance(result, dict) else 0,
            duration_sec=time.time() - start,
            log=tuple(log),
        )

    def _submit(
        self,
        client: "object",
        input_path: Path,
        pages: str | None,
        headers: dict[str, str],
        log: list[str],
    ) -> str | None:
        """提交提取任务,返回 task_id。"""
        with open(input_path, "rb") as f:
            files = {"file": (input_path.name, f, "application/pdf")}
            data = {"pages": pages or ""}
            try:
                resp = client.post(
                    f"{self._base_url}/extract",
                    headers=headers,
                    files=files,
                    data=data,
                )
                resp.raise_for_status()
            except Exception as exc:
                log.append(f"[submit error] {type(exc).__name__}: {exc}")
                return None
        payload = resp.json()
        task_id = payload.get("task_id") or payload.get("id")
        if not task_id:
            log.append(f"[submit error] no task_id in response: {payload}")
            return None
        log.append(f"[submit] task_id={task_id}")
        return str(task_id)

    def _poll(
        self,
        client: "object",
        task_id: str,
        headers: dict[str, str],
        log: list[str],
        start: float,
    ) -> dict | None:
        """轮询任务状态直到 done/failed 或超时。返回 result dict。"""
        deadline = start + _POLL_TIMEOUT_SEC
        while time.time() < deadline:
            try:
                resp = client.get(
                    f"{self._base_url}/tasks/{task_id}",
                    headers=headers,
                )
                resp.raise_for_status()
            except Exception as exc:
                log.append(f"[poll error] {type(exc).__name__}: {exc}")
                time.sleep(_POLL_INTERVAL_SEC)
                continue
            payload = resp.json()
            status = payload.get("status", "unknown")
            log.append(f"[poll] status={status}")
            if status == "done":
                return payload.get("result") or {}
            if status == "failed":
                log.append(f"[poll] task failed: {payload.get('error', '')}")
                return None
            time.sleep(_POLL_INTERVAL_SEC)
        log.append("[poll] timeout")
        return None

    def _download(
        self,
        client: "object",
        task_id: str,
        result: dict,
        out_dir: Path,
        merged_path: Path,
        images_dir: Path,
        headers: dict[str, str],
        log: list[str],
    ) -> None:
        """下载产物:优先 zip,退而求其次解析 result 里的 markdown + images。"""
        # 优先:zip 下载
        try:
            resp = client.get(
                f"{self._base_url}/tasks/{task_id}/download",
                headers=headers,
            )
            if resp.status_code == 200 and resp.content:
                import zipfile
                import io

                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    zf.extractall(out_dir)
                log.append("[download] zip extracted")
                return
        except Exception as exc:
            log.append(f"[download zip warn] {type(exc).__name__}: {exc}")

        # 退而求其次:result 里直接含 markdown + images(base64)
        md = result.get("markdown") or result.get("content") or ""
        if md:
            merged_path.write_text(md, encoding="utf-8")
            log.append("[download] markdown from result field")
        for img in result.get("images") or []:
            name = img.get("name", "")
            data_b64 = img.get("data", "")
            if name and data_b64:
                try:
                    (images_dir / name).write_bytes(base64.b64decode(data_b64))
                except Exception:
                    continue

    def _fail(
        self,
        start: float,
        log: list[str],
        merged_path: Path,
        images_dir: Path,
        error: str,
    ) -> ExtractResult:
        return ExtractResult(
            ok=False,
            source_format="pdf",
            merged_path=merged_path,
            images_dir=images_dir,
            error=error,
            duration_sec=time.time() - start,
            log=tuple(log),
        )
