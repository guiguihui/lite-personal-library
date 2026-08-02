# PageIndex v3 P0/P1 性能证据

日期：2026-08-02
平台：Windows，`windows-psapi`，10 ms 采样
执行模型：每轮启动一个全新的生产 worker 进程

## 固定语料

- profile：`exact-50k`
- 1,000 个文档
- 50,000 个 chunks
- 2,400,000 words
- 25,026,000 Markdown bytes
- corpus SHA-256：`d6b1f5a8739350f257f525b37749aaad22a55a153d32416b11cf67629c3f8933`

## 冷全量对照

命令：

```powershell
python -m app.index.v2.benchmark `
  --content E:\pageindex-v3-exact50k-final\content `
  --pageindex E:\pageindex-v3-exact50k-final\pageindex `
  --full-runs 1 --incremental-runs 20 `
  --synthetic-profile exact-50k --require-os-metrics `
  --output E:\pageindex-v3-exact50k-final\report.json
```

| 指标 | 实测 |
|---|---:|
| full wall time | 176,943.535 ms |
| peak working set | 6,280,855,552 bytes（5.850 GiB） |
| observed peak private bytes | 6,295,187,456 bytes（5.863 GiB） |
| read transfer | 689,992,355 bytes |
| write transfer | 397,931,156 bytes |
| Generation bytes | 157,007,724 bytes |
| Segment store bytes | 215,318,913 bytes |

报告 SHA-256：`f1d201783582cf352da0188adc1b1722a86c99fd3e8e53908e6e0203f9b84ed3`

该结果仍然失败于 P2/P3 的全量内存与耗时目标，不能被 P1 的 no-op 收益掩盖。

## 最终 no-op 复测

在同一固定 Generation 上执行 20 个全新进程：

```powershell
python -m app.index.v2.benchmark `
  --content E:\pageindex-v3-exact50k-final\content `
  --pageindex E:\pageindex-v3-exact50k-final\pageindex `
  --full-runs 0 --incremental-runs 20 `
  --synthetic-profile exact-50k --require-os-metrics `
  --output E:\pageindex-v3-exact50k-final\report-noop-secure-p0p1-final.json
```

| 指标 | 实测 | P1 门槛 |
|---|---:|---:|
| wall time median | 447.754 ms | — |
| wall time P95 | **467.773 ms** | < 500 ms |
| wall time max | 472.846 ms | — |
| worker duration P95 | 319.111 ms | — |
| peak working set P95 | 23,928,832 bytes（22.820 MiB） | — |
| observed private bytes P95 | 16,699,392 bytes（15.926 MiB） | — |
| read transfer P95 | 26,904,983 bytes | — |
| write transfer P95 | 2,116 bytes | — |
| outcomes | 20/20 `no_change` | 全部 `no_change` |
| distinct worker PIDs | 20/20 | 每轮独立进程 |

每一轮同时满足：

- `segments_loaded = 0`
- `postings_visited = 0`
- `generation_bytes_written = 0`
- `deep_validation_runs = 0`

报告 SHA-256：`0762a6be652eead1cb953b5c12d6c6f92e0dd2dd85adea3141819876679a8a32`

相对于同一份 50k 全量对照，no-op 的 P95 wall time 低约 378 倍，峰值工作集低约 262 倍。P1 性能门槛通过；单文档 dirty build 和全量构建的峰值内存仍属于 P2/P3。
