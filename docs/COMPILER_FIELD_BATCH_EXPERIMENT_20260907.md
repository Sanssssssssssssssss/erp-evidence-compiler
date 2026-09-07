# Evidence Compiler 字段批量绑定实验 — 2026-09-07

本轮保留为实验，未晋升：离线 **242 passed**；三个真实模型案例中，采购 **9/9**、制造 **7/7** 判断正确，发票因 Verifier 最终输出缺少全部 11 项 `status` 而返回 `NON_CONVERGED`。不把文字理由中的判断补成结构化答案，也没有重抽失败案例。

## 版本与实现

基线 `6f2c51a6cff387a67c898bbb9d0bf57f885fc7f8` 已推送到公开仓库 `Sanssssssssssssssss/erp-evidence-compiler` 的 `experiment/verifier-shared-context-20260907` 分支，远端 SHA 已核对。`main` 仍是 `be038686869ad345a5456959184e737244557b22`。

本轮从该远端重新 clone，在 `experiment/executor-field-batch-20260907` 分支修改；此前目录保留。生产代码只改 Runtime 和 Executor prompt，合计净增 **45 行**，另加一个参数化测试文件。

Evidence Executor 的单字段工具替换为 `bind_record_fields`：同一记录和 revision 的字段一次提交，Runtime 逐项复用原有 `bind_record_field_claim`。模型继续选择字段、谓词和证明关系；确定性工具读取字段真实值，校验来源、revision、指纹和 JSON pointer，并返回每项成功或错误。成功项保留，失败项可单独重试。

模型回执省去每项重复的来源、主体和完整 locator，保留实际 Claim ID、值、谓词和元数据。完整 Claim 仍进入证明存储。没有增加可见工具数量、Agent、依赖或业务规则；父 Agent 的 child run 接口、Task Compiler、六个模板、Kernel、Verifier 及一次语义返工上限保持不变。Verifier 源码和 prompt 与基线一致。

## 实验方法

模型使用 `deepseek/deepseek-v4-flash`，Executor 和 Verifier 均为 `high`。每例从密封计划和空证明 checkpoint，经原生 child 后端 `_run_compiler` 恢复，执行全新 Executor、原文优先 Verifier 和 Kernel。没有重放旧 Executor 候选，也没有新调用 Task Compiler、父 Agent 或写入 Odoo；材料是此前真实 Odoo 适配实验封存的原文与快照。

三个首轮 Executor 的输入 payload 与各自历史对照逐字段相同；历史 Executor prompt 均为基线 v8/high。期望结果独立保存，未进入模型输入。每例仅一次新运行，未在模型运行期间修改代码。对照是历史运行，Verifier 历史版本不同；本轮不能证明端到端因果节省率，也没有按 token 估算美元费用。

| 案例 | 结果 | 历史 Executor total / 请求轮数 | 新 Executor total / 请求轮数 |
| --- | --- | ---: | ---: |
| 发票 | `NON_CONVERGED`；最终提交失败 | 1,032,850 / 18 | 608,695 / 11，少 41.07% |
| 采购 | `COMMITTED / CONTRADICTED`；9/9 正确 | 829,374 / 19 | 至少 214,050 / 6；完整用量未知 |
| 制造 | `COMMITTED / NOT_FOUND`；7/7 正确 | 327,515 / 8 | 258,095 / 7，少 21.20% |

采购正确识别整体计划与单价不合规；制造正确保留计划和排程容量的材料缺口，同时接受已有目标快照。模型 assessments 与 Kernel 各 CHECK 结果均逐项核对。三例都没有进入语义返工，不能据此声称返工机制得到了新的覆盖验证。

## Infra 分阶段 token

复用 `usage_from_result` 和 `summarize_compiler_stages`，逐 provider 响应与阶段账目相互核对。下表包含缓存输入，reasoning 是输出的子集，不再重复相加。标有 `≥` 的行列出已知下限，完整值未知。

| 案例 / 阶段 | 输入 | 其中缓存 | 输出 | 其中 reasoning | total |
| --- | ---: | ---: | ---: | ---: | ---: |
| 发票 Executor | 550,068 | 488,576 | 58,627 | 41,607 | 608,695 |
| 发票 Verifier ≥ | 148,491 | 120,832 | 17,197 | 11,584 | 165,688 |
| 采购 Executor ≥ | 150,136 | 134,656 | 63,914 | 57,452 | 214,050 |
| 采购 Verifier | 59,208 | 45,056 | 18,290 | 15,024 | 77,498 |
| 制造 Executor | 180,964 | 148,096 | 77,131 | 69,149 | 258,095 |
| 制造 Verifier | 102,948 | 56,832 | 23,146 | 18,518 | 126,094 |

Task Compiler 新增 token 为 **0**，因为复用计划；Kernel 和离线重放不调用模型。本轮总计 **至少 1,450,120 tokens**，6 个逻辑阶段，已收到 32 次 provider 响应；其中一个 Verifier 阶段失败。

发票 Verifier 的三次响应均有 usage，但既有 infra 对失败阶段保守标记不完整，因此保留已知下限。采购 Executor 第一次响应有非空工具调用，却在 SDK usage 中输入、输出、total 全是零；未保存原始 wire usage 是否存在，无法判断是上游零值异常还是 SDK 缺失默认值，不能按免费调用计费或给出采购精确节省率。原始账目不改写；审计副本用现有 `partial_metrics` 标记未知。

## 协议与边界观察

- 发票有一次把 JSON 文档当作结构化记录、revision 留空的批量调用：7 项全部被拒，随后模型改用文档引用完成提交。这是一次工具调用内的 7 个字段错误。
- 采购有一次批量参数 JSON 被截断；工具返回错误后，模型重新提交成功。制造有一次 `submit_check` 多传字段，同样恢复。没有放宽校验。
- 发票 Verifier 已成功读取候选，又重复调用 `reveal_candidate` 且覆盖不全，第二次被拒；最终 JSON 缺少全部 `status`，SDK 校验失败。最终输出错误没有像普通工具输入错误一样反馈给 Agent 进行协议修复，整个批次回滚。此轮未修改 Verifier，尚不能归因于批量工具，但它阻止了整轮验收通过。
- 制造 Verifier 也重复揭示了一次候选，返回相同冻结初审后完成。重复阅读仍有 token 成本。

下一步优先用封存的发票候选单独验证 Verifier 最终提交的协议恢复，让模型自己补齐缺失字段，再走原有严格验证。不要从理由中提取并代填答案，也不必重跑昂贵 Executor。这尚未实现或验证。

## 回执与回退

本地完整回执位于 `E:\GPTProject2\compiler-archives\compiler-field-batch-20260907`。原始材料、模型响应和 SQLite 会话留在本地，不随公开源码提交。

- `precommit.json`：模型运行前冻结的源码、基线和输入 hash、独立期望值。
- `run.py`：真实 child checkpoint 恢复入口；`runs/*/` 中保存每阶段输入、SDK 响应、工具交互、原始 usage 和最终证明。
- `mechanical_replay.py` / `mechanical-replay.json`：零模型调用，历史字段操作按相同记录分组，逐字段比较旧工具与批量工具的值、类型、ID、元数据、错误和完整 EvidenceIR；发票 113→23、采购 87→29、制造 50→14 次调用。字符量下降不是 token 实测值。
- `audit.py` / `audit.json`：输入一致性、空初始证明、完整来源与计划可见性、代码 hash、实际结论和分阶段 usage 核对。`accounted-model-calls.jsonl` 是包含 usage 异常标记的审计副本，原始日志保留。
- `runs/invoice/revealed-candidate.json` 与 `rejected-final-output.json`：发票已揭示的候选及拒收输出，可用于后续固定候选实验。

离线检查命令（已运行，242 passed）：

```powershell
& E:/GPTProject2/erp-openai/.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp .test-tmp-field-batch-check2-20260907
```

无需合并或替换旧目录即可回退。在本轮实验工作树干净时：

```powershell
git switch --detach 6f2c51a6cff387a67c898bbb9d0bf57f885fc7f8
```

字段批量绑定有减少交互的信号，离线等价与边界检查通过；当前 **2/3** 的真实验收结果不足以晋升为默认版本。
