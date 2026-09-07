# Verifier 最终提交协议修复 — 2026-09-07

已验证原发票缺字段故障的恢复：工具先拒收封存草稿中缺少的 11 个 `status`，真实模型随后一次调用自行补齐，11/11 CHECK 和 Kernel 结果正确。采购、制造两例在固定 Executor 候选上重新运行 Verifier，分别 9/9、7/7 正确。离线 **250 passed**。

## 原因及与上一轮的关系

原模型将分类写进 `reason`，最终 JSON 却遗漏全部结构化 `status`。该字段在旧 schema 中已经必填，旧 prompt 也明确要求填写。SDK 因校验失败抛出 `ModelBehaviorError`，旧 Runtime 直接终止，未把这类最终输出错误反馈给模型纠正。

比较 `6f2c51a` 与批量绑定版 `e77ebfb`，`verify` 和 `_run_phase` 的 AST 完全一致，Verifier prompt 也未修改；离线使用原始失败输出再次确认旧 schema 拒收。因此可以确认是原有的协议恢复缺口，不能说上一轮删除了字段或放松了校验。不过批量绑定改变了 Executor 的轨迹及候选内容，单个随机运行不能排除其对模型遗漏概率的间接影响，也不能证明远端具体在哪一层未遵守声明的 schema。

## 最小实现

仅在共享 `_run_phase` 中为两种 Verifier 输出类型接入 `submit_verification`，复用原来的 Pydantic 输出模型、`_function_tool` 错误回执和 SDK 工具完成钩子。生产代码 **净增 42 行**；没有增加 Agent、依赖或持久化模型。

模型通过工具交最终结果；第一次 schema 错误会返回具体字段位置，允许模型纠正一次。第二次仍不合格就停止。代码不从理由提取分类，不根据业务字段产生答案，也不把校验错误变成业务结论。成功后直接返回已验证的对象，无需再生成一遍最终 JSON。

原文优先复核仍然保留：正常提交必须发生在成功揭示候选后的另一个模型回合，同一回合同时请求候选和提交结论会被拒。明确的 `plan_issue` 仍允许提前停止。传输重试重置候选可见状态；原有全 CHECK 覆盖、引用边界、独立复核和 Kernel 检查继续执行。

Evidence Verifier 每个 SDK 运行最多 4 个模型回合；原来单回合的其他 Verifier 路径允许 2 回合，以容纳一次纠正。沿用原来的自动工具选择。纯文本绕过提交工具、连续两次结构错误仍然失败关闭，不自动制造可接受答案。协议纠正与原有的一次 Executor 语义返工、传输重试分别计数。

## 三例真实模型验证

模型保持 `deepseek/deepseek-v4-flash` / `high`。从公开 `e77ebfb` 重新 clone 到独立分支 `experiment/verifier-protocol-20260907`，旧目录保留。

本轮调用原生 `verify` 和 Kernel，复用上轮密封计划、原文、Executor 候选。三个初始 payload 及重新构造后揭示的候选均与原记录逐字段相同；候选 Claims、Bindings、Witnesses 未修改。没有运行 Task Compiler、Executor、父 Agent 或执行 Odoo 写入。

| 案例 | 方法 | 结果 | 本轮已知 Verifier token |
| --- | --- | --- | ---: |
| 发票 | 重放原来的只读揭示过程及缺 `status` 的模型草稿，将它作为工具提交拒收，再让真实模型继续。错误回执只指出缺字段，没有期望答案。 | 一次真实响应纠正；11/11，根结论 `SUPPORTED`。 | 65,821 |
| 采购 | 从原文优先阶段重新运行 Verifier，审查固定候选。 | 9/9；正确识别单价及计划不合规，根结论 `CONTRADICTED`。 | 84,523 |
| 制造 | 从原文优先阶段重新运行 Verifier；一次流中断由原有传输重试恢复。 | 7/7；保留真实材料缺口，接受已有快照，根结论 `NOT_FOUND`。 | ≥71,888 |

合计 27 个 CHECK 的模型结果与 Kernel 逐项一致，Kernel 无诊断错误。发票是受控的原失败继续实验，不是新的首轮 Verifier 盲测；采购和制造是两次新复核。本轮没有证明完整父 Agent→child→Odoo 端到端，也没有新增验证 Executor 语义返工能力。

## Infra 账目

沿用 `usage_from_result`、`summarize_compiler_stages`。逐响应 usage 与阶段记录核对；total 含缓存输入，reasoning 已计入输出，不能再次累加。

| 阶段 | 输入 | 其中缓存 | 输出 | 其中 reasoning | total |
| --- | ---: | ---: | ---: | ---: | ---: |
| 发票协议纠正 | 62,504 | 0 | 3,317 | 38 | 65,821 |
| 采购原文判断 | 22,701 | 0 | 5,252 | 3,810 | 27,953 |
| 采购候选复核提交 | 37,421 | 22,656 | 19,149 | 16,669 | 56,570 |
| 制造原文判断 | 19,934 | 19,840 | 7,120 | 5,784 | 27,054 |
| 制造候选复核提交 | 34,742 | 19,840 | 10,092 | 7,917 | 44,834 |
| 制造首次流中断 | 未返回 | 未返回 | 未返回 | 未返回 | 未知 |
| 被拒绝的兼容性试验 3 次 | 未返回 | 未返回 | 未返回 | 未返回 | 未知 |

Task Compiler、Executor 新增 token 均为 **0**。所有尝试合并后的已知下限为 **222,232 tokens**，完整总量未知。

第一次实现曾新增 `tool_choice=required`，真实路由在 thinking 模式下返回三次 HTTP 400：`Thinking mode does not support this tool_choice`。这是本轮尝试引入的兼容问题，随后删除该参数及相关 override，恢复原自动选择后重新冻结代码测试；失败记录完整保留，不计为零成本或成功案例。制造另外一次 `Upstream stream ended before terminal chunk` 未返回 usage，也保留为未知。测试脚本加载旧父 Agent 依赖时还曾在 API 调用前失败，改用已安装 dotenv 读取配置，该次没有构造模型请求。

## 回执与版本

完整本地回执：`E:\GPTProject2\compiler-archives\compiler-verifier-protocol-20260907`。

- `baseline.json`：原始缺字段输出的 hash、必填约束、旧解析失败以及前后关键函数等价性。
- `precommit.json`：最终模型调用前冻结的代码、输入、实验脚本 hash；早期版本另外保留。
- `offline-final.xml`：250 passed。新增测试通过真实 SDK 工具循环验证三种状态、旧输出类型、一次纠正上限、纯文本不能冒充提交、提前提交拒收和 `plan_issue` 出口；两个旧 wire 测试同步改为检查工具提交边界。
- `runs/invoice/recorded-error-feedback.json`、`continuation-input.json`：原失败草稿和真实校验回执；这是受控重放，不标作本轮模型新犯的错误。
- `runs/*/model-calls.jsonl`、`provider-turns.jsonl`、`verifier-*-responses.json`、`verifier-*-items.json`：包含实际新调用的分阶段账目、模型响应及工具交互。
- `provider-rejected-required/`：三次兼容性拒绝及未知 usage；未覆盖失败记录。
- `audit.py` / `audit.json`：完整候选、结果与账目核对；旧工作目录仍在 `e77ebfb` 且工作树干净。

修复留在独立分支。工作树干净时可用 `git switch --detach e77ebfb61677db10d4bea13a48be0d572fed4ea6` 回到上一版。该实验验证了已知缺字段故障的有界恢复；自然语言判断正确性仍需独立复核和更广泛的案例验证。
