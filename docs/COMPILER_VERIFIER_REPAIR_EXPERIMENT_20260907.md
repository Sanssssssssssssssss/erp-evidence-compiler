# Evidence Compiler Verifier / repair experiment — 2026-09-07

实验已实现，**逐项语义验收 1/2 通过，不作为已修好版本推广**。授权案例证明定向返工有效；制造案例保留了 Verifier 共享误解的反例。

## 可回退边界

- 独立本地 clone：`E:\GPTProject2\erp-evidence-compiler-verifier-experiment-20260907`。
- 分支：`experiment/verifier-repair-20260907`。
- 基线 commit：`0218fea05adb299bc1ab1a66701f5ee328e33bea`。该 commit 包含实验前原目录的未提交源码状态。
- 原目录：`E:\GPTProject2\erp-evidence-compiler`；133 个源码/文档/测试文件的字节 hash 核对未变，原 git index 未改动。
- 回执目录：`E:\GPTProject2\compiler-archives\verifier-repair-20260907`。
- 本次未修改六个模板、Kernel、Executor prompt、Task Compiler、公共 child 工具或依赖；未执行 Odoo 业务写入或真实父 Agent 集成测试。

在实验目录工作树干净时，切回基线即可停用实验，实验分支仍保留：

```powershell
git -C E:/GPTProject2/erp-evidence-compiler-verifier-experiment-20260907 switch --detach 0218fea05adb299bc1ab1a66701f5ee328e33bea
```

## 实现

1. Verifier 获取完整任务目标、整张 DAG 的节点/依赖/动作/来源范围、去重的 proposal records，以及本次运行的所有已准入原文。它仍只评估当前 focus CHECK；额外原文不能扩大 Binding 的密封来源权限。
2. 输出增加 `plan_issue`。明确的遗漏、路由或聚合问题阻止本批提交，返回 `plan_review_required` 事件并停为 `NON_CONVERGED`。Executor 不能改密封计划，需要另行编译；本实验没有自动重规划。
3. 复用现有 gap codes：材料足够但证明缺失时由 Verifier 输出 `BINDING_MISSING` / `WITNESS_MISSING`；真实材料不足仍为 `SOURCE_MISSING` / `SOURCE_AMBIGUOUS`。代码不按文案、案例 ID 或业务值推断语义。
4. 复用现有批次事务函数，保留最大有效依赖闭包，最多用新上下文追加一次失败 CHECK 及受阻下游的 Executor / Verifier。诊断属于可质疑的反馈，不是标准答案。错误 Binding 可被替换成真实缺口说明。再次失败停止，不退化为逐 CHECK 循环。

返工发生在同一来源/计划 revision 内，`retry_count` 和事件记录一次尝试；公共 RECHECK 的新 revision 语义保持不变。单次阶段内部仍可能有多次工具交互和已有的传输重试；“最多一次返工”不是 token 上限。

## 两例实测

模型：CommandCode 的 `deepseek/deepseek-v4-flash`，thinking `high`，与先前配置相同。

使用上轮已封存的错误 Executor 候选和固定计划/材料，首次 Executor 是离线重放；新的 Verifier、返工 Executor 和再次 Verifier 使用真实模型。旧 Verifier 结论与离线期望值不进入模型上下文。两例输入及验收要求在 `precommit.json` 中预先冻结，未通过后没有继续调 prompt 或重复抽样。

| 案例 | 新结果 | 验收 |
| --- | --- | --- |
| 旧版本隐私授权 | Verifier 拒收 r2→r3 的错误反驳 Binding；只返工授权 CHECK；Executor 改为缺少 r3 授权的说明，复核后 `COMMITTED / NOT_FOUND`。范围证明逐字保留。 | 2/2 CHECK 正确，通过 |
| 制造原生快照与容量缺口 | Verifier 正确收窄 portfolio 缺口到日期容量材料，但仍额外要求“确认时刻”的新快照，将已有原生快照误报为 `SOURCE_MISSING`；没有触发返工。根结果 `COMMITTED / NOT_FOUND`。 | 6/7 CHECK 正确，未通过 |

制造例的 `target_is_unique_fresh_draft_mo` 应在已有原生快照上完成证明。当前 review 只针对密封材料，并没有要求证明未来执行时刻的状态；`LIVE_ODOO` 已准入快照带有原生记录及 revision。Verifier 承认这些字段存在，却仍沿用了旧 Executor note 中的新鲜度误解。全局上下文及 prompt 提醒没有消除这一问题，不能通过自动重试保证纠正。

真实日期容量材料确实缺失，其 CHECK 保持 `NOT_FOUND` 是正确结果。禁止根据“最终根结果碰巧相同”判定制造例通过。

## 从修改角度验证

- `offline-baseline.xml`：228 passed；`offline-final.xml`：235 passed。
- 新控制覆盖：定向成功返工、永久拒绝只返工一次、note-only 证明缺口与真实材料缺口分流、返工中的协议失败保留有效证明、完整计划/未路由原文可见、计划异议停机。旧 registered resolver 保持原行为。
- `baseline-counterfactual.json`：把本轮相同的首次 Verifier 诊断交给实验前代码离线执行。授权例停在 `NON_CONVERGED`，实验版完成闭环；制造例两版都停留在相同错误缺口。这只隔离事务机制，不是旧 Verifier 准确率测试。
- `mechanism-audit.json`：两例计划、来源指纹及 proposal hash 不变；授权返工只含 1 个 CHECK，错误 Binding 移除，独立 Binding 不变；二次 Verifier 仍看到全部 3 个 DAG 节点和 7 个来源。
- 计划遗漏识别目前只有离线协议控制，两次真实模型都返回空 `plan_issue`；尚未证明真实模型能发现计划遗漏。

## Infra token 回执

来自现有 `summarize_compiler_stages`；每次模型调用的输入/输出/缓存/reasoning/传输尝试分别留存于 `replay/<case>/pass-usage.json`、`model-calls.jsonl`。下表为包含缓存输入的 total tokens，reasoning 已包含在输出中，不再重复相加。

| 阶段 | 授权例 | 制造例 |
| --- | ---: | ---: |
| Task Compiler / 首次 Executor 重放 | 0 | 0 |
| 首次 Verifier | 26,928 | 60,916 |
| 返工 Executor | 22,105 | 0（未触发） |
| 再次 Verifier | 9,076 | 0（未触发） |
| 合计 | 58,109 | 60,916 |

总计 **119,025 tokens**，4 次真实逻辑阶段、5 个 provider turns；usage 完整，无失败传输尝试。离线基线对照没有 API 用量。

## 判断与下一步

返工的事务边界成立，但触发依赖 Verifier 判断。共享的自然语言误解仍会成为错误的正常缺口；Kernel 不能证明这段判断正确。因此保留实验、不并入主目录。

下一项值得单独验证的最小实验，是让现有 Verifier 先根据原始目标、计划和来源形成判断，再阅读 Executor 的解释，检验是否能降低对错误 note 的依赖。不要增加第三个 Agent 或按 `LIVE_ODOO` 字样硬判材料充分。两例实验也未验证全新首轮 Executor 准确率、全部六类覆盖、父 Agent 集成或进程崩溃后的返工恢复；当前只验证正常返回及已处理协议异常的事务路径。
