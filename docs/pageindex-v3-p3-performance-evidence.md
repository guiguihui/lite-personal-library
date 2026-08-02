# PageIndex v3 P3 性能收敛记录

本记录只陈述已经完成、可复现的证据。为避免继续占用机器数小时，未完成的 `20 edit + 20 delete + optimize` 精确 50k 长跑已停止，不计入验收结果。

## 已完成证据

- 完整回归：`python -m pytest -q`，结果为 `1103 passed, 7 skipped`，耗时 76.15 秒。
- 精确 50k、fresh-process no-op 共 20 轮：中位数 351 ms，P95 370 ms，最大 403 ms，满足 `< 500 ms` 门槛。
- 精确 50k、fresh-process 单文档增量抽样 1 轮：wall 4090 ms，满足 `< 5 s` 门槛。
- 该轮 worker 峰值工作集 54,874,112 bytes（约 52.3 MiB），Private Bytes 约 47.6 MiB，均低于 512 MiB。
- supervisor 发布边界的 Delta 全文件认证耗时 31.775 ms。
- 单文档增量机制计数：source 317 ms、dirty 90 ms、Generation 216 ms、Delta 2434 ms、fast artifact validation 251 ms；Segment rebuild/load 各 1，postings visited 5,043，Base posting scan 0，bytes written 3,007,349。
- 工作量硬上界：变更 Segment 215,309 bytes；postings 上界 20,576；写入上界 15,278,496 bytes。上述抽样均未越界。

对应预检数据位于 `E:\pageindex-v3-p3-preflight-20260803`。已观察的逻辑 Generation 为 `75ffde65e8a292f5193f3fb0b7cd31ab01287185e2887eea88a47cf6`，Search View 为 `eb7122f4cbd7f93e0db0d705543489608114b13c5e7c8aeb75a22f42bca620`。

## 结论与边界

现有证据已经证明此前三个主要失败点得到数量级修复：no-op 从约 130 秒降到 P95 370 ms；单文档增量从约 120.6 秒降到 4.09 秒；worker 峰值工作集从约 7.52 GiB 降到约 52.3 MiB。

这不是完整的最终性能认证：大规模 edit/delete 的 20 轮分布、显式 optimize 和查询 P95 对比尚未跑完。功能回归覆盖这些路径，但不能替代分布式性能样本。后续需要发布级证据时，应复用 `app.index.v3.benchmark` 在空闲机器上运行完整矩阵；本次不以未完成目录或中断结果宣称通过。

正常增量构建使用的是受信任进程内的 fast artifact validation：它验证 lineage、replacement/stat 边界、witness 与 Delta token/DF 一致性、token-count zero crossing、canonical 编码和新增 Delta 全文件哈希。它不等同于独立 Deep 语义重算；Deep validation 继续作为显式、非阻断审计路径。
