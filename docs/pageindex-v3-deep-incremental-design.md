# PageIndex v3 深层增量索引技术规格

- 状态：Draft — P0/P1 开始实施
- 日期：2026-08-02
- 基线提交：`0530c5474a1081324432a453f11591267b1fdb4c`
- 受众：PageIndex、检索、桌面运行时和测试维护者

## 1. 决策摘要

PageIndex v2 保留为确定性正确性基线。PageIndex v3 将构建边界从“增量 Segment、全量 Generation”推进到“增量 Segment、增量 Search View”。

已确认的核心决策：

1. 增量是默认路径；完整重建继续作为用户主动选择的修复、迁移和压缩操作。
2. 每文档不可变 Segment 仍是唯一事实来源。
3. 逻辑 Generation 与物理 Search View 分离。
4. 正式增量路径不再生成单体 legacy `chunks.json` 和 `inverted-index.json`。
5. legacy 单体格式只保留为完整重建或兼容导出。
6. title 和 breadcrumb posting 永不按 DF 裁剪；body 仅在 `df >= 256 && coverage >= 0.90` 时在查询视图中屏蔽。
7. 不引入数据库、消息队列或常驻搜索服务；第一版使用内容寻址文件、canonical JSON、排序 JSONL 和稀疏 offset 索引。

## 2. 实测问题与不可突破的下界

50,000 chunks 基准结果：

| 指标 | 实测 | 目标 |
|---|---:|---:|
| 无变化增量 | 约 130 s | < 500 ms |
| 单文档增量 | 约 120.6 s | P95 < 5 s |
| 峰值工作集 | 约 7.52 GiB | < 512 MiB |
| PageIndex 磁盘占用 | 约 358.5 MiB | 当前可接受 |

当前实现使用 Generation 内全局连续 chunk ID，并输出单体 JSON。任一前部文档增删 chunk 都可能使后续引用重编号；单体 JSON 也无法局部更新。因此当前默认增量路径具有以下下界：

```text
CPU  Ω(全部 chunks + 全部 postings)
I/O  Ω(完整 Generation 字节数)
```

Validator 再次加载全部 Segment 和候选产物并调用 `compile_generation()`，把一次全量构建放大成近似两次。非流式 canonical 序列化、目录全量 bytes 比较和长期 benchmark 进程进一步放大内存，但它们不是单文档性能失败的根因。

## 3. 目标复杂度

| 操作 | 目标复杂度 |
|---|---|
| no-op | O(源目录证明)，零 Segment 加载、零 Generation 写入 |
| 修改一个文档 | O(旧 Segment + 新 Segment) |
| 新增一个文档 | O(新 Segment) |
| 删除一个文档 | O(旧 Segment 摘要) |
| full/compaction | O(全部 postings)，但使用流式外排归并 |
| 构建峰值内存 | O(单文档 + 有界归并缓冲) |

## 4. 身份与物理布局

逻辑 Generation 只描述检索事实：

```json
{
  "schema_version": 3,
  "compiler_recipe_hash": "...",
  "documents": {
    "book:a": "segment-hash-a",
    "note:b": "segment-hash-b"
  }
}
```

逻辑 Generation ID 继续由规范化 recipe 和 `doc_key -> segment_hash` 计算。相同内容的 full、incremental 和 recompile 必须得到相同 Generation ID。

物理 Search View 独立寻址：

```text
pageindex/
├── generations/<generation>/manifest.json
├── views/<view_id>/manifest.json
├── objects/segments/
├── objects/search/base/
├── objects/search/deltas/
└── current.json
```

`current.json` 原子指向：

```json
{
  "generation": "...",
  "view_id": "...",
  "previous": {
    "generation": "...",
    "view_id": "..."
  }
}
```

同一 Generation 可拥有 incremental、compacted 和 full-built 等多个语义等价 View。Compaction 只产生新的 `view_id`，不改变 Generation。

## 5. P1：确定性输入证明与 no-op

P1 将 Generation 格式升为 schema 3，并增加 `input-proof.json`：

```json
{
  "schema_version": 1,
  "compiler_recipe_hash": "...",
  "documents": {
    "book:a": {
      "content_hash": "...",
      "segment_recipe_hash": "..."
    }
  }
}
```

`input_proof_sha256` 纳入 Generation core manifest。证明不包含绝对路径、任务 ID、时间戳或物理 View 布局。

no-op 必须在 `_read_base_segments()` 前执行：

1. 读取并验证 base manifest 和 `input-proof.json`；
2. 验证当前 Segment/Compiler recipe；
3. 发现当前文档并计算内容指纹；
4. 对稳定快照再次确认；
5. 输入证明完全一致时复用原 Generation；
6. 不加载 Segment，不编译，不物化，不执行 Validator 或 Shadow 对拍。

成功结果继续使用 `status=ready_to_publish`，并以 `outcome=no_change` 区分；普通构建使用 `outcome=built`。

schema 2 Generation 没有输入证明时自动回退现有构建路径，第一次成功构建迁移到 schema 3。

## 6. P2：内存所有权、流式编译和分层校验

构建阶段只传递 `StoredSegmentRef`，不传递全量 Segment 对象集合：

```text
SourceCatalog
  -> dirty StoredSegmentRef[]
  -> Generation/SearchView writer
  -> ArtifactRef(hash, bytes, records, path)
  -> Normal Validator
```

约束：

- Compiler 直接写 candidate，不再返回 `CompiledGeneration(payloads=...)`。
- canonical writer 在一次遍历中完成写盘、SHA-256 和 byte count。
- postings 使用排序 run 和有界 fan-in 的外排归并。
- 不同时保留 `normalized_postings`、`unpruned_export` 和 `exported_postings`。
- `_finalize_generation()` 比较 digest，不把两个目录读成 `dict[str, bytes]`。

Validator 分为：

- Normal：每次构建阻断执行，只验证新增/变化对象、聚合守恒、引用、排序、hash 和发布条件；禁止调用 `compile_generation()`。
- Sampled：确定性抽查复用对象；失败时保留旧 Generation 并升级 Deep。
- Deep：独立短生命周期进程执行全量语义重算，用于迁移、recipe 升级、CI、手动验证和周期审计。

## 7. P3：Base + Delta Search View

取消 Generation 内全局连续 chunk ID：

```text
doc_uid   = SHA-256(doc_key)
chunk_ref = [doc_uid, segment_hash, local_id]
```

每个 Segment 保存未裁剪字段 TF 和增量摘要：

```text
chunk_count
title/breadcrumb/body length_sum
token.df_any
token.df_body
posting_count
```

单文档变化通过 `global = base - old + new` 更新聚合。

Search View 由一个 token 排序的不可变 base 和若干 delta 组成。delta 记录：

- `parent_view`；
- `generation`；
- `replaced_docs`；
- 变化文档的新 posting fragments；
- 聚合差量及文件 hash。

查询 token 时从新 delta 向旧 delta/base 合并；首次遇到某 `doc_uid` 即确定最新状态。文档级 replacement/tombstone 可以同时处理删除和“token 从文档中消失”，无需为每个消失 token 写墓碑。

触发 Compaction 的初始门槛：

- delta 数量 > 32；或
- delta 总字节 > base 的 20%；或
- 用户主动执行完整优化。

## 8. 分字段 DF 语义

原始 title、breadcrumb、body TF 保留在 Segment 和 Search View 中。查询时根据全局 token statistics 应用：

```text
title_tf       永远有效
breadcrumb_tf  永远有效
body_tf        当 df_body >= 256 且 df_body / total_chunks >= 0.90 时视为 0
```

因此 total_chunks 或阈值变化只更新小型 policy/statistics，不重写全部 posting。该选择用少量可控磁盘空间换取可逆、深层增量的检索语义。

## 9. 运行时迁移

v2 仍是 shadow-only。当前正式路径包含后端顶层 JSON 读取，以及前端聊天一次性下载 `inverted-index.json` 和 `chunks.json` 的同步检索。因此 P3 必须同时提供：

1. 服务端 generation/view-pinned Search View reader；
2. 固定 `{generation, view_id}` 的搜索 API；
3. 聊天检索异步 API 适配；
4. v2/v3 双读结果对拍；
5. legacy `export-legacy-full`；
6. current/previous 回滚和延迟 GC。

在 reader 和聊天链路切换之前，构建端不能宣称已经摆脱单体索引。

## 10. 性能门槛

标准容量为 1,000 documents / 精确 50,000 chunks：

| 指标 | 门槛 |
|---|---:|
| no-op P95 | < 500 ms |
| 单文档修改 P95 | < 5 s |
| 删除文档 P95 | < 5 s |
| worker Peak Working Set | < 512 MiB |
| worker Peak Private Bytes | < 512 MiB |
| Normal Validator | 不得调用 `compile_generation()` |
| 查询 P95 回归 | < 10% |

性能报告必须同时证明：

- 结果：incremental 与 clean full 的逻辑 Generation 相等；
- 机制：no-op 零加载零写入，dirty 工作量只与变化规模相关；
- 资源：独立 worker 的 wall、Peak Working Set、Private Bytes 和 I/O 达标。

不得通过 `gc.collect()`、Working Set trim、关闭校验、热缓存单次测试或缩小 posting 密度宣称达标。

## 11. 实施阶段

| 阶段 | 内容 | 独立验收 |
|---|---|---|
| P0 | 独立 worker 基准、OS 资源指标、精确 50k profile | 得到可信的单轮证据 |
| P1 | schema 3 input proof、no-op 快路 | 130 s 降至源扫描成本 |
| P2 | 流式 writer、引用式 Segment、Normal/Deep | 峰值 < 512 MiB |
| P3 | stable refs、base+delta View、reader | 单文档 P95 < 5 s |
| P4 | 双读切换、Compaction、GC、legacy export | 长期运行稳定 |

P0/P1 的逐步骤实施计划位于 `docs/superpowers/plans/2026-08-02-pageindex-v3-p0-p1.md`。
