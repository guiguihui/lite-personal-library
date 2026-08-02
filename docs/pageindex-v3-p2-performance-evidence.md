# PageIndex v3 P2 有界内存编译证据

日期：2026-08-02  
平台：Windows 10.0.22000.1574，Python 3.10.10，`windows-psapi`，10 ms 采样  
分支：`codex/pageindex-v3-deep-incremental`  
提交：`212a670e9ae7e780eb056e9751fa265f33cecb5c`  
执行模型：每一轮使用新的生产 worker 进程；Deep Audit 使用另一个短生命周期子进程

## 固定语料与等价性

- profile：`exact-50k`
- 1,000 个文档
- 50,000 个 chunks
- 2,400,000 words
- 25,026,000 Markdown bytes
- corpus SHA-256：`d6b1f5a8739350f257f525b37749aaad22a55a153d32416b11cf67629c3f8933`
- 输出 postings：5,021,405
- Generation bytes：157,007,724，与 P0/P1 旧编译器基线逐字节规模一致
- Generation：`357fe4b7b466b593de1f`

除了单元测试中的 rich Unicode、逆序输入、强制多级归并和 DF 阈值 byte oracle 外，还对该 50k Generation 显式执行了独立 Deep Audit：

```text
status=completed
audit_pid=32448
validation.ok=true
errors=[]
warnings=[]
wall≈57.9s
```

Deep Audit 不在正常构建路径中运行，也不计入下面的 worker 峰值。

## 冷全量命令

```powershell
python -m app.index.v2.benchmark `
  --content E:\pageindex-v3-exact50k-p2-20260802\content `
  --pageindex E:\pageindex-v3-exact50k-p2-20260802\pageindex `
  --synthetic-profile exact-50k `
  --full-runs 1 --incremental-runs 20 `
  --require-os-metrics `
  --output E:\pageindex-v3-exact50k-p2-20260802\report-p2-cold-noop.json
```

原始报告：`E:\pageindex-v3-exact50k-p2-20260802\report-p2-cold-noop.json`  
报告 SHA-256：`0367f4b7e29ff0a67cb534a05e996b43cce2dda33c848f3bee6aac6f2a0efee7`

## P2 冷全量结果

| 指标 | P0/P1 旧基线 | P2 | 变化/门槛 |
|---|---:|---:|---:|
| wall time | 176,943.535 ms | 159,171.240 ms | -10.04% |
| peak working set | 6,280,855,552 B | **198,983,680 B** | -96.83%，31.56 倍更小；< 512 MiB |
| observed private bytes | 6,295,187,456 B | **192,212,992 B** | -96.95%，32.75 倍更小；< 512 MiB |
| read transfer | 689,992,355 B | 1,494,914,683 B | 2.17 倍 |
| write transfer | 397,931,156 B | 829,667,227 B | 2.08 倍 |
| Generation bytes | 157,007,724 B | 157,007,724 B | 完全相同 |
| referenced Segment bytes | 215,318,913 B | 215,318,913 B | 完全相同 |

额外 I/O 来自有界 posting runs、外部归并和独立流式结构校验，是用磁盘顺序 I/O 换取稳定内存上界的明确代价。最终 Generation 和 Segment store 没有膨胀；临时 runs 在完成或失败后关闭句柄并清理。

机制计数：

- `segments_loaded = 1000`，但 `segments_loaded_peak = 1`
- `run_buffer_peak_bytes = 33,554,431`，未超过 32 MiB 配置上界
- `postings_visited = 5,021,405`，每个原始 posting 进入一次兼容编译
- `generation_bytes_written = 157,007,724`
- `full_compile_runs = 1`
- `normal_validation_runs = 1`
- `deep_validation_runs = 0`

## no-op 回归

20/20 轮均为 `no_change`：

| 指标 | P1 专项复测 | P2 同轮冷构建报告 | 门槛 |
|---|---:|---:|---:|
| wall median | 447.754 ms | 468.649 ms | — |
| wall P95 | 467.773 ms | **497.927 ms** | < 500 ms |
| wall max | 472.846 ms | 518.923 ms | — |
| peak working set P95 | 23,928,832 B | 24,092,672 B | — |
| private bytes P95 | 16,699,392 B | 16,846,848 B | — |

P2 相对最优 P1 专项 no-op 的 median/P95 分别回退 4.67%/6.45%，但 P95 仍通过 500 ms 门槛。每一轮继续满足：

- `segments_loaded = 0`
- `postings_visited = 0`
- `generation_bytes_written = 0`
- `full_compile_runs = 0`
- `normal_validation_runs = 0`
- `deep_validation_runs = 0`

## dirty、delete 与 recompile

命令：

```powershell
python -m app.index.v2.benchmark `
  --content E:\pageindex-v3-exact50k-p2-20260802\content `
  --pageindex E:\pageindex-v3-exact50k-p2-20260802\pageindex `
  --synthetic-profile exact-50k `
  --full-runs 0 --incremental-runs 0 `
  --edit-runs 1 --delete-runs 1 --recompile-runs 1 `
  --require-os-metrics `
  --output E:\pageindex-v3-exact50k-p2-20260802\report-p2-dirty-delete-recompile.json
```

原始报告：`E:\pageindex-v3-exact50k-p2-20260802\report-p2-dirty-delete-recompile.json`  
报告 SHA-256：`c713bc7b5d3d9303ed41202dad1472a634e016241840090373479cdb18ddb4a6`

| 场景 | wall | peak WS | rebuilt/deleted/reused | postings visited |
|---|---:|---:|---:|---:|
| 单文档编辑 | 140,174.696 ms | 198,475,776 B | 1 / 0 / 999 | 5,021,404 |
| 单文档删除 | 130,833.408 ms | 194,207,744 B | 0 / 1 / 999 | 5,016,378 |
| 纯 recompile | 129,201.707 ms | 194,150,400 B | 0 / 0 / 999 | 5,016,378 |

三轮都满足 `segments_loaded_peak <= 1`、`full_compile_runs = 1`、`normal_validation_runs = 1`、`deep_validation_runs = 0`。单文档编辑只比纯 recompile 多约 11 秒，而 recompile 本身仍需约 129 秒，证明当前 dirty 延迟的主导项是 schema-3 单体兼容索引的全量编译，不是 Segment 重建。

因此 P2 的结论是：

- `<512 MiB` 内存门槛已通过；
- Normal Validator 不再做第二次全量语义构建；
- 序列化、哈希和发布比较已改为有界流式处理；
- no-op P95 仍通过 `<500 ms`；
- dirty/delete 的 `<5 s` 门槛尚未通过，必须由 P3 base+delta Search View 消除 schema-3 全量编译边界；不能通过继续微调 compatibility compiler 宣称完成。

## 自动化验证

```text
python -m pytest tests/pageindex_v2 -q
224 passed, 1 skipped

python -m pytest -q
476 passed, 2 skipped
```

唯一 warning 是测试依赖中的 Starlette/httpx 弃用提示，与 PageIndex 改动无关。
