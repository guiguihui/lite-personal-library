# Knowledge Links Capacity Benchmark

- Date: 2026-08-02
- Branch: `feat/knowledge-linking-governance-context`
- Runtime: Python 3.10.10 on Windows
- Target: 10,000 documents and 100,000 explicit edges
- Harness: `tests/performance/benchmark_knowledge_links.py`

## Method

The harness creates a temporary notes collection. Every document has an immutable typed ID, governance metadata, and deterministic canonical Wikilinks. The corpus and generated index are removed automatically when the process exits.

Measured phases:

1. Generate Markdown files.
2. Build the deterministic link index.
3. Drop the in-memory result and cold-load `link-index.json` from disk.
4. Run 500 backlinks, neighborhood, and preview queries against deterministic document IDs.

The query target in the integration specification is hot-cache P95 below 100 ms for backlinks/neighborhood and below 150 ms for previews.

## Baseline Before Adjacency Compression

| Metric | Result |
|---|---:|
| Corpus generation | 4.944 s |
| Index build | 12.673 s |
| Index size | 154.11 MB |
| Peak working set | 492.25 MB |
| Backlinks P95 | 0.0115 ms |
| Neighborhood P95 | 0.0221 ms |
| Preview P95 | 0.0007 ms |

The index serialized each edge in full three times: once in `edges`, once in `outgoing`, and once in `incoming`.

## Optimized Result

Adjacency lists now store integer references into the canonical `edges` array. Empty optional fields are omitted from serialized edges. Query code remains backward compatible with the earlier object-based adjacency representation.

| Metric | Result |
|---|---:|
| Documents | 10,000 |
| Explicit edges | 100,000 |
| Corpus generation | 5.285 s |
| Index build | 12.569 s |
| Cold JSON load | 0.198 s |
| Build throughput | 795.6 documents/s |
| Edge throughput | 7,956 edges/s |
| Index size | 42.99 MB |
| Peak working set | 238.89 MB |
| Backlinks P50 / P95 / max | 0.0101 / 0.0139 / 0.0252 ms |
| Neighborhood P50 / P95 / max | 0.0241 / 0.0278 / 0.0533 ms |
| Preview P50 / P95 / max | 0.0005 / 0.0007 / 0.0028 ms |
| Broken / ambiguous links | 0 / 0 |

## Outcome

- Capacity target passed.
- All query latency targets passed with substantial headroom.
- Index size decreased by 72.1%.
- Peak working set decreased by 51.5%.
- Build duration remained effectively stable.
- Cold loading a 42.99 MB index took 198 ms on this environment.

## High-Degree Hub Scenario

A second 10,000-document/100,000-edge run directed one edge from almost every document to note:doc-00000. The hub had 9,999 distinct neighbors.

| Metric | Result |
|---|---:|
| Index build | 12.122 s |
| Cold JSON load | 0.226 s |
| Index size | 42.99 MB |
| Peak working set | 238.34 MB |
| Hub backlinks P50 / P95 / max | 14.3058 / 36.5701 / 47.4848 ms |
| Hub neighborhood P50 / P95 / max | 25.4659 / 32.6559 / 36.1249 ms |
| Hub neighbors | 9,999 |
| 40-node truncation | Passed |

The high-degree path remains below the 100 ms P95 requirement.

## Remaining Performance Work

- Measure UI frame time and D3 interaction in the real PyWebView runtime; the Python harness does not measure rendering.
- Add mixed resolved, broken, ambiguous, provenance, and archived edges to measure diagnostic-heavy indexes.
- Consider streaming JSON generation only if future libraries exceed the current 10k/100k design capacity; it is not required by this result.

## Reproduction

```powershell
python tests/performance/benchmark_knowledge_links.py --documents 10000 --edges 100000 --iterations 500
python tests/performance/benchmark_knowledge_links.py --documents 10000 --edges 100000 --iterations 500 --topology hub
```
