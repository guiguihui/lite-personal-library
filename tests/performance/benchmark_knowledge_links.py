"""Synthetic capacity benchmark for the knowledge-link index.

The generated corpus lives in a TemporaryDirectory and is never committed.
Run from the repository root:

    python tests/performance/benchmark_knowledge_links.py --documents 10000 --edges 100000
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.knowledge.indexer import build_link_index  # noqa: E402
from app.knowledge.queries import get_backlinks, get_neighborhood, get_preview  # noqa: E402


def _rss_mb() -> float | None:
    try:
        import psutil

        memory = psutil.Process().memory_info()
        value = getattr(memory, 'peak_wset', memory.rss)
        return round(value / 1024 / 1024, 2)
    except (ImportError, OSError):
        pass
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
            return round(counters.PeakWorkingSetSize / 1024 / 1024, 2)
        except Exception:
            return None
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(usage / (1024 if sys.platform != "darwin" else 1024 * 1024), 2)
    except Exception:
        return None


def _percentile(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return ordered[index]


def generate_corpus(content: Path, documents: int, edges: int, topology: str) -> None:
    notes = content / "notes"
    notes.mkdir(parents=True)
    base, remainder = divmod(edges, documents)
    for source in range(documents):
        count = base + (1 if source < remainder else 0)
        targets = []
        if topology == "hub" and source != 0 and count:
            targets.append(0)
        remaining = count - len(targets)
        targets.extend((source + step + 1) % documents for step in range(remaining))
        links = [f"[[note:doc-{target:05d}]]" for target in targets]
        text = (
            "---\n"
            f"id: note:doc-{source:05d}\n"
            f"title: Document {source:05d}\n"
            "status: reviewed\n"
            "confidence: 0.9\n"
            "---\n\n"
            f"# Document {source:05d}\n\n"
            + " ".join(links)
            + "\n"
        )
        (notes / f"doc-{source:05d}.md").write_text(text, encoding="utf-8", newline="\n")


def timed_queries(index: dict, documents: int, iterations: int) -> dict:
    timings = {"backlinks": [], "neighborhood": [], "preview": []}
    for iteration in range(iterations):
        doc_id = f"note:doc-{(iteration * 7919) % documents:05d}"
        for name, query in (
            ("backlinks", lambda: get_backlinks(index, doc_id)),
            ("neighborhood", lambda: get_neighborhood(index, doc_id, 40)),
            ("preview", lambda: get_preview(index, doc_id)),
        ):
            started = time.perf_counter_ns()
            query()
            timings[name].append((time.perf_counter_ns() - started) / 1_000_000)
    return {
        name: {
            "p50_ms": round(statistics.median(samples), 4),
            "p95_ms": round(_percentile(samples, 0.95), 4),
            "max_ms": round(max(samples), 4),
        }
        for name, samples in timings.items()
    }


def run(documents: int, edges: int, iterations: int, topology: str = "uniform") -> dict:
    with tempfile.TemporaryDirectory(prefix="lqd-knowledge-bench-") as root:
        root_path = Path(root)
        content, pageindex = root_path / "content", root_path / "pageindex"
        generated_at = time.perf_counter()
        generate_corpus(content, documents, edges, topology)
        generation_sec = time.perf_counter() - generated_at
        build_at = time.perf_counter()
        index = build_link_index(content, pageindex)
        build_sec = time.perf_counter() - build_at
        actual_edges = len(index["edges"])
        if len(index["documents"]) != documents or actual_edges != edges:
            raise AssertionError(f"expected {documents}/{edges}, got {len(index['documents'])}/{actual_edges}")
        del index
        gc.collect()
        load_at = time.perf_counter()
        index = json.loads((pageindex / "link-index.json").read_text(encoding="utf-8"))
        load_sec = time.perf_counter() - load_at
        query_results = timed_queries(index, documents, iterations)
        hub_queries = {}
        for name, query in (
            ("backlinks", lambda: get_backlinks(index, "note:doc-00000")),
            ("neighborhood", lambda: get_neighborhood(index, "note:doc-00000", 40)),
        ):
            samples = []
            for _ in range(100):
                started = time.perf_counter_ns()
                result = query()
                samples.append((time.perf_counter_ns() - started) / 1_000_000)
            hub_queries[name] = {
                "p50_ms": round(statistics.median(samples), 4),
                "p95_ms": round(_percentile(samples, 0.95), 4),
                "max_ms": round(max(samples), 4),
                "total": result.get("total", result.get("total_neighbors")),
                "truncated": result.get("truncated"),
            }
        return {
            "topology": topology,
            "documents": documents,
            "edges": actual_edges,
            "generation_sec": round(generation_sec, 3),
            "build_sec": round(build_sec, 3),
            "cold_load_sec": round(load_sec, 3),
            "documents_per_sec": round(documents / build_sec, 1),
            "edges_per_sec": round(edges / build_sec, 1),
            "index_mb": round((pageindex / "link-index.json").stat().st_size / 1024 / 1024, 2),
            "peak_rss_mb": _rss_mb(),
            "diagnostics": {key: len(value) for key, value in index["diagnostics"].items()},
            "queries": query_results,
            "hub_queries": hub_queries,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--documents", type=int, default=10_000)
    parser.add_argument("--edges", type=int, default=100_000)
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--topology", choices=("uniform", "hub"), default="uniform")
    args = parser.parse_args()
    if args.documents < 2 or args.edges < args.documents:
        parser.error("documents must be >= 2 and edges must be >= documents")
    print(json.dumps(run(args.documents, args.edges, args.iterations, args.topology), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
