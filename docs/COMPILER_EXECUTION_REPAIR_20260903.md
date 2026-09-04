# Compiler 执行链修复记录（2026-09-03）

状态：完整执行链的受控正确性已通过；成本验收未全部通过，仍是独立实验候选，不是“所有 ERP 场景已完成”。

## 冻结点

- 完整本地冻结：`d22c20a`，包含原始实验记录。
- 已公开推送的代码冻结：`54cd5927f25936fe7da2eb928572fac19752cb96`。
- 远端：`Sanssssssssssssssss/odoo-erp-agent`，分支 `codex/compiler-code-freeze-20260903`。
- 本次修复单独发布到 `codex/compiler-execution-repair-20260903`，以代码冻结为父提交，不携带本地原始记录的提交历史。
- 原始 reasoning、events、SQLite 未随这次公开推送发布；公开发布原始记录需要另外明确授权。
- 新修复仍隔离在实验工作区，不并入主线，不接 Manager、MCP 或 Odoo 写入。

## 实际调用链

1. 应用传入候选动作、审核目的、完整冻结材料；不替模型指定 Pack。
2. `src/erp_agent_odoo/evidence_review.py::compile_review` 让一个模型调用从传入的已注册目录选择检查方案及对应材料。
3. `src/erp_agent_odoo/capabilities/proof_dag.py` 校验选择，并把方案展开成具体 CHECK 和数据依赖。代码只确定检查内容，不能生成业务事实或结论。
4. `backend/app/compiler_runtime/runtime.py` 一次 Executor 工具循环读取原文/记录字段、提出带来源的事实与关系、调用数值工具、提交各 CHECK。
5. 独立 Fine Verifier 看到完整相关原文、候选动作及证明候选，逐项审核；Kernel 检查来源、引用闭包和计算重放。通过且依赖闭合的 CHECK 才进入 checkpoint。

一个 revision 的正常批处理为一次逻辑 Executor、一次逻辑 Verifier；工具后续轮次及网络重试另计。协议失败不能算业务拒绝，失败记录不改成成功。未完成的 checkpoint 保持可恢复；修复进入下一 revision，已通过的独立分支保留。

## 新旧保证不混淆

新增独立 `policies/evidence_action_review_v1.json`：支持非结构化材料的模型语义审核，并保留来源及计算校验。原 `odoo_erp_action_plan_v1.json` 的注册 resolver 保证没有降低。

语义审核不是数学证明自然语言：Verifier 可以独立发现事实解释错误，但仍可能判断错误。Kernel 不能证明签字真实性、授权人的真实身份，或现实系统在快照之后没有变化。

## 修复的共享问题

- 原 ERP 入口只允许注册 resolver，但目录里的多数业务检查没有 resolver；现在显式使用语义审核路径，不再为每个业务编造专用解析器。
- 补齐真正的 CHECK 数据依赖，区分单动作检查与整组候选动作检查；可行性不再冒充全局最优。
- Executor 直接得到通过沙箱读取的完整冻结材料，避免反复读同一来源。
- 文档唯一原文片段可自动定位；重复片段必须消歧，显式错误行号仍拒绝。
- 记录字段用原始 JSON Pointer、revision、值；已核验的原生 JSON 小数使用十进制拼写计算。文档数字仍要求 Decimal 字符串。
- 活动证明只取最新提交及其依赖闭包，被替代/拒绝候选不进入下一 revision。
- 给 Verifier 提供确定性的引用 ID 闭包，不替它接受事实或生成结论。
- 新审核模式的计算工具只接收已有引用 ID，由 Sandbox 在 Claim、Witness、当前 CHECK 的已配置 Policy 中唯一查找类型；未知/碰撞拒绝，旧 typed 接口和计算语义保留。
- Compiler 保持 high；新语义路径 Executor、Verifier 使用 low。曾尝试 disabled，但 provider 仍报告大量 reasoning，因此不能把配置文字当执行证据。

### 额外发现：共享 Provider 参数与输出预算

原 `backend/app/agents/thinking.py` 把 Responses 形状的 `reasoning.effort` 发给 CommandCode 的 Chat Completions；因此此前日志中的 low/disabled 不能证明请求实际采用了该强度。现按 Chat 形状发送 `thinking.type` 与 `reasoning_effort`，其他接口原行为不动。CommandCode 文档确认 Chat 端点，但没有保证这些扩展参数全部透传或被上游执行；本轮也没有证明参数变动是成败的唯一原因。[CommandCode 接口](https://commandcode.ai/docs/provider)、[DeepSeek 参数协议](https://api-docs.deepseek.com/guides/thinking_mode/)。

先经参数契约红测 11 项失败，再绿测 28/28。`wire-disabled/request.json` 保存的是实际 HTTP 请求体（无 headers/key），不是自行拼写的预期配置。真实小请求返回完整 JSON，119 token、6.072 秒，provider 报告 reasoning 20 token；不声称 disabled 等于零计费 reasoning。

`final-contradicted-low` 又耗尽原 8,192 输出预算，说明 low 不是硬保证。新模式现在直接在生产调用中设置 Compiler 1 turn / 4,096，Executor 6 turns / 8,192，Verifier 1 turn / 16,384；输出上限同时包含 reasoning 与最终答案。测试入口只采集回执，不再暗中修改预算。transport retry 另计，因此 6 turns 不是跨重试的总响应硬上限；每个完整 child 的 70k token / 240 秒仍单独验收。

## 真实实验：不只发票

材料是明确标注的合成控制组，不冒充实时 Odoo：一份规则文档、一条付款记录、一份质量官签字销毁材料。外部目录含付款放行与库存销毁两个业务类型；三个 CHECK 检查金额限制、付款授权、销毁授权。预期答案仅在运行结束后用于断言，不进入模型上下文。

原始目录统一为 `tests/compiler_v1/artifacts/execution_repair/`。

| 运行 | 实际结果 | 已知用量与问题 |
|---|---|---|
| `live-supported-20260903T0919` | 未完成 Executor | 数字类型、把币种用于数值比较；失败前用量不全，不能报总 token |
| `live-supported-v2` | 未完成 Executor | 字段路径/候选参数边界不清；HTTP 500 与截断工具参数；用量不全 |
| `live-supported-v3` | Executor 提交 3 项，Verifier 未输出 JSON | 84,659 token，8 个已报告模型响应，151.158 秒；Verifier 的 8,192 输出 token 全用于 reasoning |
| `live-supported-v4` | Executor 提交 3 项，Verifier JSON 截断，未提交最终证明 | 已知 token 下界 54,306；V 用量缺失，不能把摘要的 6 当完整响应数；253.601 秒；一次连接重试 |
| `verifier-v4-isolation` | 同一候选的 V-only，未通过 | 16,578 token，65.464 秒；所谓 low 仍耗尽 8,192 reasoning token |
| `verifier-v4-none` | 同一候选的 V-only，未通过 | 16,578 token，76.883 秒；所谓 disabled 仍耗尽 8,192 reasoning token，随后发现协议参数错 |
| `verifier-v4-wire-correct` | 同一 v4 候选的 V+Kernel 通过 | 14,273 token，51.608 秒；正确 Chat 请求；不是全新完整 child |
| `final-supported` | 全新 C/E/V，V JSON 截断 | 45,991 token，115.207 秒；3 项 Executor 提交正确；V 的 8,192 输出中 7,399 reasoning，已闭合的两项也缺 status；原样拒绝 |
| `verifier-final-low-wire` | 同一 final-supported 候选的 V+Kernel 通过 | 14,990 token，57.274 秒；显式 Chat reasoning_effort=low，模型完整返回 3 项状态；Kernel 无诊断 |
| `final-supported-low` | **全新完整 child：COMMITTED / SUPPORTED** | C1/E1/V1，5 个实际响应，48,909 token，110.946 秒；3 项完整业务审核与 Kernel 均通过 |
| `final-contradicted-low` | 全新反例在 V 输出协议失败，未提交 | 59,355 token，148.945 秒；Executor 正确提出超限，V 的 8,192 输出全是 reasoning，没有审核 JSON |
| `verifier-contradicted-budget` | 同一反例候选 V+Kernel：CONTRADICTED | **额外** 20,615 token，105.665 秒；12,292 输出中 11,570 reasoning，3 项审核完成，无 Kernel 诊断；不是完整 child |
| `final-contradicted-complete` | **全新完整 child：COMMITTED / CONTRADICTED** | C1/E1/V1，8 个响应，77,136 token，165.302 秒；正确性 PASS，**70k 效率 FAIL** |
| `final-missing-complete` | **全新完整 child：COMMITTED / NOT_FOUND** | C1/E1/V1，8 个响应，73,092 token，149.775 秒；真实缺少付款 screening 结果，不能凭审批人齐全放行；正确性 PASS，**70k 效率 FAIL** |

最后三组完整对照分别验证：材料足够且满足条件、付款金额超限、付款授权材料缺失。三组的金额、授权与销毁 CHECK 均实际经过 Executor、Fine Verifier 和 Kernel，不是 Compiler 提前按预期答案返回。每组都是一个全新会话；没有把 V-only 重放拼接成完整运行。

它们不是同一代码快照的统计 A/B：SUPPORTED 完整运行早于原始审核目标补传、16k Verifier 预算和非流式 reasoning 修复；CONTRADICTED 完整运行早于派生 reasoning 的最终回答过滤；missing 完整运行已包含这些修复，但早于最后的引用 ID 工具接口简化。各完整目录保存 `code-hashes.json`；最后接口改动只做下述隔离验证，不把早期成功补记成最终版本的重跑。

总消费不能只计算最后成功三例：本目录 14 份 `trial_summary.json` 的逐调用已报告 token 合计 **530,790**，另有真实协议小探针 **119**、最后引用接口探针 **1,351**，即已知下界 **532,260 token**；另外 4 条调用记录缺失 usage，实际总量未知。缓存输入包含在 token 内，不等于按非缓存单价计费；没有账单价格，不能编造金额。失败实验全部保留。这一轮的反复输出截断及工具参数恢复仍产生了明显成本，不应描述成低成本方案已经完成。

隔离重放不重跑 Compiler/Executor、不继承旧 Verifier 结论，也不修补截断 JSON。独立审计核对候选、原始材料与计划哈希以及实际模型返回。一次低 effort 成功不证明 provider 的推理预算控制稳定；已保存反例，不能从日志标签推断模型确实关闭 reasoning。

最后一次隔离还包含审核目标字段补传，不是纯单变量实验：独立审计发现原 `task_objective` 被通用句替代，可能丢失“只审部分/审核整体”的范围。现复用 `ProofPlan.objective` 原样保留，并提供给 E/V；旧 checkpoint 不被改写。对应红例 3 项失败转绿，`objective-budget-regression.xml` 总计 **106/106**。原反例 59,355 加隔离 20,615 实际共 79,970 token，不能把失败调用从消费中抹掉。

`final-contradicted-complete` 的额外成本来自两次恢复：数值操作数的 currency 属性一有一无；随后把 Claim ID 当作注册 `policy_ref`。工具都拒绝错误参数，模型最终修正并提交；没有放宽数值引擎或伪造政策字段。这个真实错误仍值得后续优化，不为刷绿重复跑同题。历史 `passed` 字段只判断语义正确性；新 probe 已分别输出 `correctness_passed`、`efficiency_passed`，总 `passed` 要两者都通过，原历史回执不改写。

missing 也出现了相同的两次参数恢复：16 次工具调用中，文本绑定 5 次、记录字段绑定 5 次、计算 3 次、提交 3 次。审计把根因分开：引用类型已在现有索引中确定，不该让模型猜；币种关联却不一定已被证明，不能自动补齐或抹掉。

最后一个接口修复因此只消除引用类型猜测：模型向 `compute_witness` 传 ID 字符串，Runtime 唯一查表后仍进入原计算路径，不按“policy”这个业务词猜类型。未知/冲突 ID、越过本 CHECK 的政策参数仍拒绝；相同操作产生与旧 typed 调用完全相同的 Witness。币种错误不做静默修补。红例 4 项失败转绿，最后定向回归 **132/132**（`publish-final-ref.xml`）。

双审计批准后，`reference-ids-live` 完成 **1 次真实 HTTP、1,351 token、4.95 秒** 的模型选参→实际工具→原 Witness 重放。只给预绑定的测试事实，没有传入计算结果；实际有序操作数必须为 requested_amount、policy_limit，不能用自己与自己比较骗过断言。无 transport retry，输出上限 2,048。它是小型工具接口实验，不是完整 child，也没有重测或改写前面的效率 FAIL。其原始 HTTP request/response、工具返回及 summary 保留；没有伪造一次不存在的 SDK/SQLite 会话。

两位审计者随后均读取真实 HTTP、工具返回与 fixture，独立重放 `90.25 <= 100`，再次 ACCEPT；确认实际模型只传 ID，没有替它补写操作数、结果或引用类型。

## 独立审计结论

两名独立审计者分别检查 Runtime/事务与业务语义，都对三组完整控制的实际结论及保存的回执给出 ACCEPT。最后 missing 审计实际重新运行 Kernel，结果与保存的 proof/checkpoint 一致；三份来源 SHA、proposal/plan/artifact 哈希、SQLite 与 SDK 的调用参数/结果也逐项相符，没有传入 expected、variant、oracle 或旧运行结论。

ACCEPT 范围是受控正确性与可追溯性，**不包含效率达标或全部 ERP 泛化**。missing 的三个 CHECK 为 SUPPORTED / NOT_FOUND / SUPPORTED；总体 NOT_FOUND 带有缺失 screening 的阻断义务。`COMMITTED` 仅表示审核结果已经保存，**不表示付款获准**。缺失或矛盾都不能被应用解释成可以执行高风险动作。

## 离线证据与限制

- 新执行链、事务、来源接口、原 DAG 及旧 Phase 9 batch 回归：`final-regression.xml` 为 **104/104**；包含 E/V 协议错误归因的红绿反例，原先 V 错误被记作 Executor，现只修事件阶段。
- `frozen-plan-audit.json`：11 份之前真实 Compiler 路由结果可在当前目录展开。只是结构检查，不代表 11 份业务都经 Executor/Verifier 通过。
- 旧后端 5 个测试文件共 237 项：冻结版本 103 失败、134 通过；修复中初查 112 失败、125 通过。独立审计逐项修正 7 个猜测行号的正向 fixture、2 个过时计数/状态断言后，最终同组 134 通过、103 失败，失败名单与冻结版本完全相同（`frozen-base-audit.xml`、`legacy-final.xml`）。没有通过放宽来源校验消除失败；不能宣称旧测试全绿。
- 另加 provider/thinking/context 回归共 149 项：148 通过，`test_prompt_prefix_hash_snapshots_are_stable` 失败。独立冻结/当前对照得到完全相同的 tenant policy hash 不匹配（`frozen-prefix-isolated.xml`、`current-prefix-isolated.xml`），未改预期值掩盖失败。
- 目前要求一份已接纳的规则/指令来源；多份规则冲突的优先级没有偷偷猜测。
- 需要原生状态的检查在没有相应快照时只能报缺失；候选动作不能替代实际系统状态。
- 没有证明任意文件、任意业务、任意规模都可用；本轮不扩展新 DSL、不跑 50 例。

## 重现命令

仅使用仓库私有 `.env` 中的 CommandCode；不得使用官方 DeepSeek，也不要把 key 写入命令或报告。

```powershell
$env:PYTHONPATH="$PWD\backend;$PWD\src"
$env:INVOICE_AGENT_ENV_FILE="$PWD\.env"
& E:\GPTProject2\erp-openai\.venv\Scripts\python.exe -m pytest tests/compiler_v1/execution_repair -q -p no:cacheprovider --basetemp tests/compiler_v1/artifacts/execution_repair/pytest-fresh
# 付费验证必须使用新的 output-dir，禁止覆盖旧记录。
& E:\GPTProject2\erp-openai\.venv\Scripts\python.exe tests/compiler_v1/execution_repair/probe.py --variant supported --output-dir tests/compiler_v1/artifacts/execution_repair/live-fresh
```

真实调用保留来源/目录/计划、各阶段原始响应与 reasoning、工具记录、SQLite、checkpoint、模型调用与摘要。失败调用不保证 provider 报告 usage；缺失必须保持未知，不能填零。

注意旧回执中的 reasoning token 数不等于 reasoning 原文已完整保存：实际 HTTP 小探针表明 CommandCode 可使用 `reasoning` 字段，而已安装 SDK 非流式转换只读取 `reasoning_content`。例如 `verifier-contradicted-budget` 的 11,570 reasoning token 有用量报告，但旧 SDK 回执没有对应正文。不能事后回填或声称这些旧原文可恢复。现已复用既有 Chat 适配器，仅对真实 CommandCode 主机、非流式回复、缺失 canonical 字段的情况原样补别名；不改正文、工具、usage 或 SDK 安装。相关红例 4 项失败转绿，别名相关回归 45/45。

独立审计又发现旧 `reasoning_full` 提取会把最终回答 JSON 也算作推理正文。修复只复用既有条目类型过滤函数，不另建观察层；红例 2 项失败转绿。`final-contradicted-complete` 的原始 SDK response 没丢正文，但旧派生 `reasoning_full` 有上述混入，不重写历史记录。最新 missing 完整运行使用修复后的提取。该阶段回归 **128/128**（`publish-final.xml`）；再计引用接口反例后是上述 **132/132**，没有新增完整业务重跑来改变历史分数。

## 查看具体输出

每个 `final-*` 目录中：

- `request.json`、`catalog.json`、`source-snapshot.json`：实际候选动作、可选方案和全部冻结原文。
- `compiler-answer.json`、`proof-plan.json`：真实模型选择和随后校验的可执行 DAG。
- `executor-items.json`、`session.sqlite`：本次真实工具调用、返回值及会话；`*-attempt-*-sdk-responses.json` 保留各次 transport 的原始模型响应，不靠最终摘要猜测。
- `artifact.json`、`proof.json`、`checkpoint.json`：独立模型复核结果、Kernel 重放及最后活动证明。
- `model-calls.jsonl`、`reasoning.txt`、`events.jsonl`、`trial_summary.json`：用量、实际返回的 reasoning、事件和结果。没有报告的 TTFT 保持 null。

例如 `final-supported-low` 是一次全新完整 child；它有 19 次工具调用（9 次文本绑定、6 次字段绑定、1 次计算、3 次提交），0 次工具错误。原文已在受控入口完整提供，因此没有重复调用 read_source。独立审计确认 SQLite 与全部调用参数/结果一致，原始 V JSON 自带三个明确状态，重新 Kernel replay 与保存结果一致。

以后定位失败先看阶段与原始响应：协议/传输失败先在原 candidate 上隔离重放；业务规则反例才修计划或证明语义。相同失败没有新假设不重复付费跑全链。只在隔离层和确定性回归通过后，做一个全新完整 child，避免把拼接回放宣称整体成功。
