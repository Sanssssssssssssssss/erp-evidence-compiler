# 发票检查范围更正 — 2026-09-07

- 核对本地 ERP-Bench 固定版本 `ceba3880af555129b5278e056a0c20f2fb5a0ba9`：300 个任务指令及对应 300 个 UI 指令均未提到 fiscal position；共享生成器、评分器也未要求写入或比较该字段。
- 原始开票规则检查张数/阶段、客户、付款条件和金额。68 个开票任务调用税额检查：`amount_total` 与 `amount_untaxed` 相等；初始化清空销售税。依据：[共享评分器](https://github.com/agentic-labs/erp-bench/blob/ceba3880af555129b5278e056a0c20f2fb5a0ba9/erp_bench/templates/procurement/supply_planning/checks.py.jinja2#L2371)、[开票检查调用](https://github.com/agentic-labs/erp-bench/blob/ceba3880af555129b5278e056a0c20f2fb5a0ba9/erp_bench/templates/procurement/supply_planning/test.sh.jinja2#L249)、[税初始化](https://github.com/agentic-labs/erp-bench/blob/ceba3880af555129b5278e056a0c20f2fb5a0ba9/erp_bench/templates/procurement/supply_planning/setup_scenario.py.jinja2#L733)。评分器只用于本次离线审计，未提供给模型；执行时依据已准入指令和原生数据，不能从“预算未税”推断零税。
- 额外约束来自本地归档 `odoo-six-parent-20260905/build_manifests.py:30` 的“preserve ... native fiscal position”，以及旧发票模板。该样例使用真实 Odoo 观测，但 VAT10 和政策是自建的，不能称为原始 benchmark 发票案例。
- 仅调整模板两处：税务配置字段是否必须相等/保留，由已准入政策决定；不默认要求 fiscal position。原生税额和总额检查保留，不新增比较提交工具。
- 已封存的来源、计划、期望、结果不改写；旧自建政策下的缺字段误判仍成立。后续 benchmark 材料按原始要求构造，不能沿用旧自建 VAT10/财政位置政策。新模板只影响重新编译的计划，不改变旧 checkpoint。
- 合并算术是真实 DeepSeek 响应中的 3 次 `compute_planned_witnesses` 调用；模型选择 CHECK，工具读取密封字段并调用既有计算器，模型负责语义判断和提交。来源：`compiler-executor-lean-20260907/runs/invoice/executor-1-1-responses.json` 首次响应。
- 验证：计划编译、字段算术及合并工具共 61 项离线检查通过（`test_proof_dag_compiler.py`、`test_atomic_amounts.py`、`test_planned_calculation.py`）。未调用模型，Task Compiler / Executor / Verifier 新增 API token 均为 0；未据此宣称新模板已通过真实模型回归。
