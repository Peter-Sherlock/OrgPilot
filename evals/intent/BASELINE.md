# 意图行为评测基线（ADR-0008 第 1 步、第 3a 步、第 3a.1 步与第 3a.2 步）

评测对象：`evals/intent/` 固定项目夹具上的系统行为。样本口径：`LABELING.md`（R1–R11 + R9 追问上限），
单条样本规模 dev 102 条 / test 58 条；多轮对话 `conversations.yaml` 共 10 段 36 轮。

**baseline 历史数字对应的提交为 `4ee48bf`**（第 3a 步完成、第 3a.1 步开始前）。
如需复跑：`git checkout 4ee48bf` 后执行 `uv run orgpilot eval-intent --split dev|test
--mode mock --system layered --intent heuristic|oracle`。

**多轮对话评测**：`uv run orgpilot eval-intent --conversations --system layered
--intent heuristic|oracle`（3a.1 新增，3a.2 扩充）。oracle 门槛：全部对话通过。第 3a.2 步结果：
layered + oracle **10/10 段全部通过（36/36 轮）**，未经确认群发 0、同步误触发 0；
layered + heuristic 2/10 通过（conv_01 探针补时间收敛 + conv_08 幂等），其中 **conv_01 是 3a.2
为 mock 演示设的唯一 heuristic 门槛**（必须收敛并带正确完成时间），其余对话只记录不设门槛
（探针回答的意图识别正是 3b 的目标）。单条样本运行方式：
`uv run orgpilot eval-intent --split dev|test --mode mock --system baseline|layered --intent heuristic|oracle`。
本文件只记录数字，不带生成时间；生成的详细报告不入版本控制（`evals/intent/reports/`）。

## test 集冻结

`evals/intent/test.yaml` 的 sha256：

```text
b023ded8044a9f9f0888566cdb62245c1d4f0cadd5c75035038efc87c1ec36a3
```

**之后修改 test 集需要先经过审批**（2026-10-12 的 answers_pending 修订经产品指示获批，
哈希已同步更新）；获批修改后必须同步更新此处的 sha256 与
`tests/test_intent_dataset.py::test_frozen_test_split_unchanged_without_approval`
（该测试在校验失败时会提示需要审批）。

## 三组对比（第 3a.2 步之后，当前口径 R1–R11 + R9 追问上限 + sync_started）

| 指标 | baseline（历史） | layered + heuristic（3a.2） | layered + oracle（上限） |
|---|---|---|---|
| dev 样本通过率 | 19.00%（81 失败） | 22.55%（79 失败） | **100.00%（0 失败）** |
| test 样本通过率 | 17.24%（48 失败） | 17.24%（48 失败） | **100.00%（0 失败）** |
| dev claim P / R | 41.18% / 17.50% | 37.50% / 21.43% | **100% / 100%** |
| test claim P / R | 33.33% / 10.00% | 30.00% / 15.00% | **100% / 100%** |
| dev 时间准确率 | 75.00% | 87.50% | **100%** |
| test 时间准确率 | 0.00% | 0.00% | **100%** |
| dev 错误 claim 率 | 6.25%（4/64） | 9.23%（6/65） | **0%** |
| test 错误 claim 率 | 7.69%（3/39） | 10.26%（4/39） | **0%** |
| dev 同步误触发率 | 0.00%（0/91） | 0.00%（0/91） | **0%** |
| test 同步误触发率 | 0.00%（0/52） | 0.00%（0/52） | **0%** |
| dev 未经确认群发次数 | 0/5 | 0/5 | **0/5** |
| test 未经确认群发次数 | 0/3 | 0/3 | **0/3** |
| dev 回复类别准确率 | 30.00% | 37.25% | **100%** |
| test 回复类别准确率 | 20.69% | 27.59% | **100%** |
| dev 多轮补全成功率 | 12.50%（1/8） | 62.50%（5/8） | **100%（8/8）** |
| test 多轮补全成功率 | 0.00%（0/4） | 50.00%（2/4） | **100%（4/4）** |

说明：

- **baseline 一行为历史数字**，采集于第 3a 步之前的提交（96315b2 世代，R1–R11 标注 +
  sync_started 口径）。第 3a 步把 webhook/sandbox 全部切到 MessageRouter 并删除旧路由逻辑
  之后，`--system baseline` 在运行时与 layered+heuristic 走同一条链路，仅观测口径不同
  （baseline 由响应 msg+事件推导回复类别，layered 由 router 上报类别），因此两组数字略有差异。
- **layered + oracle 满足全部验收门槛**：未经确认群发 = 0；同步误触发率 = 0；错误 claim 率 = 0；
  dev 与 test 样本通过率均 ≥ 95%（实际 100%）。0 条失败意味着当前**没有发现流水线 bug，
  也没有发现需要上报的标注问题**；oracle 与 heuristic 之间约 78 个百分点的差距全部来自
  "没有模型"，等 3b 接入真实模型后按同一口径度量模型误差。
- layered + heuristic 的提升来自结构而非关键词：第 3a.2 步为 mock 演示给 heuristic 客户端加了
  一个兜底（有待回答问题时，非命令/闲聊/确认语的消息按 answer_pending 处理，任务默认取待答任务、
  多目标任务按消息提及优先，时间仍由 TemporalResolver 解析），多轮补全 37.5% → 62.5%（dev）、
  0% → 50%（test），回复类别 33% → 37.25%（dev）；代价是错误 claim 率 4/64 → 6/65（dev）——
  含糊回复现在会以 at_risk 兜底进 claim。这是刻意用准确率换演示收敛的取舍，评测门槛只在
  conv_01（多轮对话）与 oracle（单条样本）上。其余召回差距仍是 3b 的目标空间。

## 历史基线（第 3a 步之前，96315b2 世代）

采集顺序：先评测器修订（f85f6ab，(a) 组），后时间修复（75a14c3 + 96315b2，(b) 组）；
(a) 组口径为 R1–R10 + sync_confirm 未拆分，(b) 组为 R1–R11 + sync_started。

| 指标 | (a) dev | (a) test | (b) dev | (b) test |
|---|---|---|---|---|
| 样本数 | 100 | 58 | 100 | 58 |
| claim precision | 41.18% | 20.00% | 41.18% | 33.33% |
| claim recall | 17.50% | 5.00% | 17.50% | 10.00% |
| task_id 准确率 | 76.47% | 40.00% | 76.47% | 50.00% |
| 健康状态准确率 | 53.85% | 50.00% | 53.85% | 66.67% |
| 时间准确率 | 0.00% | 0.00% | 75.00% | 0.00% |
| 错误 claim 率 | 6.25%（4/64） | 7.69%（3/39） | 6.25%（4/64） | 7.69%（3/39） |
| 同步误触发率 | 0.00%（0/91） | 0.00%（0/52） | 0.00%（0/91） | 0.00%（0/52） |
| 未经确认群发次数 | 1（样本 5） | 0（样本 3） | 1（样本 5） | 0（样本 3） |
| 回复类别准确率 | 29.00% | 17.24% | 28.00% | 18.97% |
| 多轮补全成功率 | 12.50%（1/8） | 0.00%（0/4） | 12.50%（1/8） | 0.00%（0/4） |
| 失败样本 | 87 | 51 | 85 | 51 |

历史读数说明：

- (a) → (b) 的变化来自时间修复：dev 时间准确率 0% → 75%（"下周三"按自然周解析）；
  test 的提升（R11 改写后 mock 多命中 1 条正确 claim）已在上一轮记录。
- 未经确认群发 1 次（dev_request_sync_003 命中旧关键词直接群发）在第 3a 步引入确认卡片
  流程后归零，且同步误触发保持 0。
- test 时间准确率 0% 的原因：可触发 claim 的样本时间表达（月底、晚上八点、中午、明早）
  均在现有 TemporalResolver 能力之外，或消息本身不触发 claim。
