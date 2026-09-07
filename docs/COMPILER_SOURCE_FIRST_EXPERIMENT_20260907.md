# Evidence Compiler 原文优先复核实验 — 2026-09-07

本轮 **2/2 案例通过，9 个 CHECK 与预期一致**；上轮仍错误的制造快照 CHECK 完成了一次定向返工。结论限于这两个固定错误候选，不代表全新首轮 Executor、六类 ERP 全覆盖或父 Agent 集成已经通过。

## 实现与边界

在同一个 Verifier 调用里，最初只提供密封计划、proposal、政策和本次全部已准入原文。Executor 的 Claims、Bindings、Witnesses、note、上游证明和返工诊断暂不进入模型上下文。

Verifier 通过一个只读 `reveal_candidate` function call 提交每个 focus CHECK 的简短材料判断：`SUFFICIENT`、`MISSING` 或 `AMBIGUOUS`，并说明原文依据。工具记录第一次判断后，才返回候选证明。Verifier 随后依据原文审查候选；初步判断不是标准答案，也不会被 Kernel 自动转换成结论。若它改变判断，需要说明原文依据。

工具只检查覆盖全部 focus CHECK、无重复 ID、理由非空等协议要求。它不识别业务关键词，也不按案例 ID、金额或原文判断答案。漏审不能解锁；跳过工具直接交最终结论会被拒收；重复调用返回原先冻结的材料判断。明确的 `plan_issue` 可以在揭示候选之前停机。

本次相对 `f7ab2fb`：Runtime 净增 46 行，Verifier prompt 增加 12 行，共 58 行生产内容。复用已有 `_function_tool` 和 `_run_phase`，不增加 Agent、依赖、持久化模型或公共 child 工具。六个模板、Kernel、Task Compiler、Executor prompt、一次返工上限保持不变。

每次 Verifier 仍是一段同一 Agent 的逻辑会话，但本轮实际需要 **2 次 provider 请求**：原文判断并调用工具，然后审查候选并交最终结果。不能把这一变化描述成“没有增加模型调用”。

## 两例结果

模型保持 `deepseek/deepseek-v4-flash` / thinking `high`。继续使用上轮相同的两个旧 Executor 候选、原始材料和固定计划；首次 Executor 重放和 Task Compiler 均不调用 API。原始旧 Verifier 结论、离线期望值均未进入模型输入。两例各运行一次，没有调参后反复抽样。

| 案例 | 原文初审 | 候选复核及返工 | 最终结果 |
| --- | --- | --- | --- |
| 旧版本隐私授权 | 在看候选前确认：协议存在，但只有 r2 签字，缺少 r3 授权。 | 拒收将缺失推为反驳的 Binding；只返工授权 CHECK，移除错误终端证明。 | `COMMITTED / NOT_FOUND`；2/2 CHECK 正确。 |
| 制造原生快照与日期容量缺口 | 在看旧 note 前，确认目标原生快照 `SUFFICIENT`；日期容量材料 `MISSING`。 | 揭示旧“还需确认时刻新快照”说明后，没有沿用它，输出 `BINDING_MISSING`。Executor 为已有原生快照补齐终端 Binding，第二次复核接受。 | `COMMITTED / NOT_FOUND`；7/7 CHECK 正确，快照 CHECK 为 `SUPPORTED`，真实日期容量缺口仍为 `NOT_FOUND`。 |

制造例的目标 CHECK 路径是：原文足够 → 候选没有完成证明 → 仅返工这个 CHECK → 接受补齐后的证明。没有为了得到强结论而补造缺失的容量记录，也没有重跑其余六个 CHECK。

## 验证回执

回执目录：`E:\GPTProject2\compiler-archives\verifier-source-first-20260907`。

- `precommit.json`：预先冻结的输入 hash、基线和验收范围；案例来源仍是前轮相同的封存错误候选。
- `offline-final.xml`：235 passed。更新两个现有测试，验证延迟可见、完整 focus 覆盖、重复调用保留初审、提前终结拒收；已有返工上限和错误恢复测试继续通过。
- `mechanism-audit.json`：实际 SDK 回执中，每次 Verifier 第一条模型响应为 `reveal_candidate`，随后才有包含候选的 tool output；没有协议重试或第三次 provider 请求。原文判断与事件回执、工具输出一致。
- 两例计划、来源指纹、proposal hash 均保持不变。每例只返工一个 CHECK，已提交的独立 Bindings 逐字段保持不变。
- 各案例的 `events.jsonl` 保存初审判断；`*-responses.json`、`*-items.json` 保存实际工具交互；`pass-usage.json` / `model-calls.jsonl` 保存 infra 阶段用量。没有 Odoo 业务写入。

## Infra token 成本

使用已有 `summarize_compiler_stages` 统计逻辑阶段，并用 `usage_from_result` 拆分 SDK provider 响应；两者总量一致。下表 total tokens 包含缓存输入，reasoning 已在输出 token 内，不重复累计。

| 阶段 | 授权例 | 制造例 |
| --- | ---: | ---: |
| 首次 Verifier：原文初审 | 9,984 | 32,800 |
| 首次 Verifier：候选复核 | 20,494 | 57,551 |
| 返工 Executor | 22,803 | 49,216 |
| 再次 Verifier：原文初审 | 10,301 | 20,481 |
| 再次 Verifier：候选复核 | 8,479 | 20,952 |
| 合计 | **72,061** | **181,000** |

总计 **253,061 tokens**，6 个真实逻辑阶段、12 个 provider turns，usage 完整，无失败传输尝试。Task Compiler 与首次 Executor 重放新增 tokens 为 0。

上轮为 119,025 tokens，本轮增加 134,036（约 112.6%）；上轮制造例没有触发返工，这个总量差异包含新增的有效修复成本。只比较两例都具备的首次 Verifier：87,844 → 120,829，增加约 37.6%。不把一次随机模型运行的成本差异当作固定开销系数。

## 可回退版本与判断

实验仍位于 `E:\GPTProject2\erp-evidence-compiler-verifier-experiment-20260907`，分支 `experiment/verifier-source-first-20260907`。上轮 commit `f7ab2fb52a037f76cc401ad6677a2a600a3bc661` 和分支 `experiment/verifier-repair-20260907` 都保留。原目录 `E:\GPTProject2\erp-evidence-compiler` 的 133 个源码/文档/测试文件 hash 未变。

在实验目录工作树干净时，回到上一版：

```powershell
git -C E:/GPTProject2/erp-evidence-compiler-verifier-experiment-20260907 switch experiment/verifier-repair-20260907
```

这次结果支持继续保留“先读原文，再看候选”的方向：模型实际先识别了已有快照，候选复核也没有被旧 note 带偏。但一次固定候选对照不足以证明普遍降低误判。初审仍可能自行误解，后续复核也可能推翻正确初审，Kernel 依然不能证明自然语言判断正确。两个计划都没有触发 `plan_issue`，真实模型发现计划遗漏的能力尚未验证。

当前显著代价是额外原文初审及长上下文的成本。后续应先用少量未见案例验证这一方向，再评估同一密封 revision 内复用初审能否降低返工成本；本轮不继续叠加流程或针对制造关键词写规则。版本保持在独立实验分支，未并回原目录。
