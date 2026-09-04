# 当前 Compiler 真实模型验证：未通过 Executor 交接门槛

日期：2026-09-03。分支：`codex/compiler-v1-odoo-capability`；基础 HEAD：`19e24022868af1fa9b3f52b614746aea5357cae5`，包含工作区未提交改动。本轮没有提交、推送或改动 Kernel / Fine Verifier。

## 结论

本轮确实重新调用了 CommandCode，不是重放旧模型回答。

- 修复后的 10 个完整 ERP-Bench 材料场景和 1 个外部方案案例，模型选方案及结构校验通过。
- 这不等于完整 Compiler 验收通过：计划中仍有缺失的检查依赖、没有具体定位方法的取证要求，以及不存在的执行实现。
- `Executor = 0`、`Fine Verifier = 0`。遵守“Compiler 未通过不得进入下游”的门槛，未用模型调用撞缺失工具。
- 没有覆盖 ERP-Bench 的全部 29 个任务家族；这 10 个是此前讨论的 5 个主场景和 5 个补充变体。外部案例还覆盖付款与库存销毁。不得写成“所有业务已经泛化通过”。

## 测试到底喂了什么

每个 ERP 场景均提供完整、未裁剪的 `instruction.md` 和 `environment/scenario_data.json`。模型必须调用 `read_source` 阅读全部来源；来源文件和 SHA-256 均保留。

Manager 请求由测试模拟：自然语言审核目标、候选动作与记录、冻结来源引用。不传指定的 Pack ID。完整注册目录交给模型自行选择。测试预期单独放在 `inputs/offline-expectations.json`，不进入模型上下文。

候选动作有意不是 oracle 答案：例如按已有订单交替提出确认/取消，或提出明确标记为 hypothetical 的草稿。其数量、日期可能不正确，应该由后续审核发现。原始来源仍然完整。**这些候选记录不是 live Odoo snapshot，不能将这轮称作真实 Odoo 业务执行。**

新测试生成器不读取 `cases.json`、`solution`、benchmark verifier 或历史模型回答。外部付款/销毁控制仅复用此前冻结的输入材料和目录，不复用其模型回答。

## 实际模型回答与中文点评

表中“通过”仅指选择方案及生成结构合法的 DAG，未证明业务 verdict 或可直接执行。

| 场景 | 真实回答选择的检查方案（中文） | 点评 | tokens | 秒 | 当前回执 |
|---|---|---|---:|---:|---|
| 2032 接单筛选 | 销售订单接单/拒单审核 | 确认与取消都包含接单规则；检查间的数据依赖仍有缺口 | 44,686 | 18.93 | `repaired_cases/2032/receipt/` |
| 2016 固定首付 | 普通销售确认、采购确认、客户发票过账 | 正确保留首付、普通、尾款三个阶段；没有误加接单筛选 | 82,683 | 71.36 | `repaired_cases/2016/receipt/` |
| 2272 采购取消与替代采购 | 取消受影响承诺、采购确认 | 取消不等待替代方案；替代采购等待取消后置状态 | 43,536 | 21.32 | `repaired_cases/2272/receipt/` |
| 2053 筛选后开票 | 接单/拒单审核、采购确认、发票过账 | 修复后新建草稿的 release 动作也走接单筛选，不能绕过规则 | 44,311 | 16.64 | `repaired_cases/2053/receipt/` |
| 2217 制造、采购与开票 | 普通销售确认、采购确认、制造确认、发票过账 | 选择正确，但供应/BOM/产能执行实现尚不存在 | 50,168 | 34.37 | `repaired_cases/2217/receipt/` |
| 2024 百分比首付 | 普通销售确认、采购确认、客户发票过账 | 同一发票方案覆盖百分比首付；不是新造一个 case 专属方案 | 42,894 | 24.62 | `supplemental_cases/2024/receipt/` |
| 2086 优先保留产能 | 普通销售确认、采购确认、制造确认 | 方案选择正确；完整材料被重复读取，效率不合格 | 125,332 | 44.77 | `supplemental_cases/2086/receipt/` |
| 2171 禁止采购成品 | 普通销售确认、采购确认、制造确认 | 采购动作仍需审核，不应因为候选可能违规就不安排检查；尚未证明执行器能正确区分成品与组件 | 47,846 | 28.41 | `supplemental_cases/2171/receipt/` |
| 2205 筛选与减少供应商数 | 接单/拒单审核、采购确认、制造确认 | 包括新建草稿在内的确认动作正确选择筛选；是否能落实供应商数量目标尚未执行验证 | 51,836 | 19.09 | `supplemental_cases/2205/receipt/` |
| 2290 制造取消与重新安排 | 取消受影响承诺、采购确认、制造确认 | 选择正确；替代采购有取消依赖，制造 release 没有同样依赖，阶段语义仍需明确 | 58,073 | 25.65 | `supplemental_cases/2290/receipt/` |
| 外部付款 + 库存销毁 | 付款放行审核、库存销毁授权审核 | 两动作分别绑定自己的材料；未选供应商主数据干扰方案。`semantic_evidence` 执行路径仍不存在 | 5,991 | 9.69 | `external_catalog/receipt/` |

外部案例的实际来源绑定：

- 付款 → `policy:high-risk:v7` + `request:payment:7qj4`。
- 销毁 → `policy:high-risk:v7` + `request:disposal:9lm2`。

每个回执目录中的 `task-compiler-output.json` 是本次原始结构化回答，`executor-plan.json` 是该次回答经 Runtime 展开得到的计划。

## 已找到并修复的问题

### 阶段标签使接单筛选被绕过

2053 原文要求订单数量在 17–21、交期至少 12 天。修复前，同一确认操作如果 Manager 写 stage=`release`，只能匹配普通销售方案；新建草稿因此没有接单筛选。

真实修复前证据：`full_cases/2053/receipt/task-compiler-output.json`，其中 `action-3` 选择 `sales_order_release.v1`。原始 reasoning 明确依据 stage 作了这个选择。

修复不是改候选答案或业务规则：

1. 在注册目录中明确两种销售方案各自的业务适用条件。
2. `confirm` / `release` 的兼容性不再强迫模型选择某个业务方案；存在接单筛选时，release 同样能选择筛选方案。
3. 提示词要求根据完整材料的政策语义选择，不能仅依靠阶段名称或“是不是 seeded 订单”。

反例测试修复前：`1 failed, 1 passed`，失败为筛选方案不接受 release。

修复后真实证据：`repaired_cases/2053/receipt/task-compiler-output.json`，同一个 `action-3` 改选 `sales_order_disposition.v1`。输入候选与原始材料未改变。2016、2217 没有误选筛选方案；2205 也正确采用筛选。

**特别纠正**：`full_cases` 最初离线预期也沿用了旧的 stage 区分，因而 `routing_matches_offline_expectations=true` 不能作为业务语义正确的证据。读取原文后才发现该预期遗漏；原文件未被改写，新预期只用于修复后的回归。本报告否定 `full_cases/2053` 的业务适用性通过结论。

## 为什么尚不进入 Executor

### 1. 取证要求仍不是完整执行指令

例如 `request_identity_matches_order` 把客户请求的数量和标识列为 `BOUND_SOURCE / STRUCTURED`。但 2016 的请求数量、交期是在原文句子里；完整 `scenario_data.json` 没有一个统一的结构化 request 数组。

目前计划给出了“找 quantity”，没有给出“从哪份来源的哪一段抽取、怎样映射到目标记录”。这不是缺原始材料，也不是证明模型能力不足，是计划没有把实际材料形态交代清楚。

### 2. CHECK 之间需要的数据依赖没有接全

`proposal_matches_policy_outcome` 的证据配方使用 `UPSTREAM_CHECK/per_request_acceptance`，但 `repaired_cases/2032/receipt/first-frontier-proof-plan.json` 中取消动作对应 CHECK 的 `upstream_check_ids` 为空，没有连接到 `acceptance_predicates_match_requested_action`。

因此现在只能说动作级 DAG 存在，不能说每个检查的输入依赖已经完整。此项本轮未修复，不交给 Executor 猜。

### 3. 有检查名称，不代表已有执行实现

注册目录有 36 种 CHECK 合同；当前 ERP resolver 注册的仅是取消相关的 3 项。`plan-audit.json` 列出每个首批 CHECK 对应的缺失实现，例如供应、BOM、产能、字段匹配与外部语义审核。

这不意味着应该再造 33 个业务专用函数。它说明当前“只允许调用注册检查”的执行路径尚不能承接这套通用计划。不能通过默认 true、mock 或让模型自行编造工具结果绕过。

对于取消的 3 项，注册存在也不意味着输入齐全：当前公开 scenario 不含所有实时 Odoo 锁定/账单状态；本轮没有提供 live snapshot。`strict_executor_ready=null` 的意思是未验证，不是通过。

### 4. 制造替代阶段仍有待明确的差异

2290 的替代采购需要取消后的状态，制造 release 则没有相同依赖。不能凭“目录里有制造 Pack”断言整个修复业务已覆盖。是否必须采用相同执行顺序，需要按动作与资源释放关系确认，不能一律增加更严格限制。

## 实际成本：包括不成功的设计与修复前调用

完整机器可核查统计：`observation-summary.json`。

- 共 17 次新的逻辑 Compiler 调用：1 次入口 smoke、5 次修复前、5 次修复后、5 次补充场景、1 次外部案例。
- 共 36 次 provider response，不把它们说成 17 次 HTTP 请求。
- 总 token：857,456。
- input：813,966，其中 cached input 180,736；uncached input 633,230。
- output：43,490，其中 reasoning 38,502。reasoning 已包含在 output，不能再次相加。
- 各次调用耗时合计 403.41 秒；这是调用时长之和，不是本轮工作的实际历时。
- 金额：`null`，没有可靠的账单价格，未虚构美元成本。
- 2016 修复后用了 3 轮、82,683 token；2086 用了 3 轮、125,332 token。按先前 70k 的单例效率目标，两者效率 FAIL，不重跑来美化结果。
- 2086 的 `events.jsonl` 明确显示两份完整来源被各读取两次，是额外 token 成本的直接证据。该效率问题本轮保留，没有继续扩大修改。

配置：CommandCode compatible、`https://api.commandcode.ai/provider/v1`、`deepseek/deepseek-v4-flash`、high。除最初 smoke 使用原入口默认外，本轮显式限定 `max_turns=3`、`max_output_tokens=4096`。全部密钥只从现有 `.env` 加载。

## 证据完整性与限制

17 个 SQLite session 的 `PRAGMA integrity_check` 全部返回 `ok`。每次的 reasoning、events、model-calls、结构化输出、SDK response 记录、DAG 和 checkpoint 文件均存在。没有以旧 SQLite 冒充新会话。

`raw-provider-responses.json` 当前保存的是 SDK 层输出，不是 HTTP 原始字节；其中 ModelResponse 被旧 `_jsonable` 处理成 repr 字符串。`new_items` 保留可解析工具调用内容。这会限制逐 response 自动复算，不应称为完善的原始网络抓包。

`summary.json` 的 `passed=true` 是现有 runner 的路由/结构通过，不是全部业务检查、Executor、Verifier 或 Kernel 通过。全局验收以本报告的 **未通过交接门槛** 为准。

## 改动与确定性检查

- `src/erp_agent_odoo/capabilities/proof_templates.json`：修复销售确认阶段名称导致的方案适用性限制，目录 schema_version=6。
- `tests/compiler_v1/proof_corpus/task_compiler_prompt.md`：按政策语义选择；将“读两份来源”修正为“读所有来源”。
- `tests/compiler_v1/proof_corpus/task_compiler_probe.py`：保存目录、来源 manifest 和代码 hashes；允许设置有限轮数与输出上限。
- `tests/compiler_v1/proof_corpus/validate_current_compiler.py`：仅测试工具，生成公开来源候选、运行新模型调用、检查交接实现、离线核查保存产物；不接入产品 Runtime。
- `tests/compiler_v1/proof_corpus/test_proof_dag_compiler.py`：增加 confirm/release 阶段等价覆盖反例。
- 相关四组确定性测试最终 `41 passed`；这不是 41 个业务模型测试。当前 Python 环境未安装 ruff，所以没有宣称 lint 通过。

本轮未引入新依赖、未写新 Executor、未修改 Manager、未触碰 Compiler Kernel/Fine Verifier、未接 MCP、未跑 Harbor eval。这里是有明确失败门槛的 Compiler 验证结果，不是最终完成声明。
