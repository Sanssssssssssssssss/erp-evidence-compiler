# 中国税票核验实验

独立分支 `experiment/tax-invoice-20260907`，基于 `0df1249`。未合并 main。

接入阿里云官方 OCR 产品下的 **VerifyVATInvoice（增值税发票核验）**，不是 OCR 识别接口，也不是完整财务审计服务。

流程：准入的原票字段 → 调用阿里云并保存原始回执 → 一起冻结为证据 → Task Compiler → Executor 调用 `verify_tax_invoice(check_id)` → Verifier → 原有 Kernel。

工具绑定原票和回执两边的字段、生成金额比较 Witness，返回引用；业务结论仍由 Agent 提交和复核。正式票样用于字段含义和适用票种比对，不以像素、字体或印章相似度判真伪。原票抽取仍由既有 Agent/材料准入流程完成，当前工具不做 OCR，也不证明抽取值一定正确。

## 开启方式

默认关闭：`ERP_COMPILER_TAX_INVOICE_ENABLED=0`，不设置也等同关闭。关闭时新审核不加载税票模板，不提供核验工具，也不查询阿里云或写入核验回执；已有证明和冻结材料不改写。配置凭据或材料中的 `verify_tax_invoice` 标记不会自动开启功能。

开关追加回归：285项通过；当前进程确认为关闭、目录保留原有6个模板。本次未调用模型或阿里云 API。

只在需要时，在启动父 Agent 的进程环境中设置开关；修改配置后重启父 Agent：

```powershell
python -m pip install -e '.[dev,tax-invoice]'
$env:ERP_COMPILER_TAX_INVOICE_ENABLED = '1' # 开启；日常使用设为 '0'
```

本机环境配置 `ALIBABA_CLOUD_ACCESS_KEY_ID`、`ALIBABA_CLOUD_ACCESS_KEY_SECRET`，临时凭据可加 `ALIBABA_CLOUD_SECURITY_TOKEN`。账号须已开通阿里云票证核验服务。工具使用官方 SDK 签名、固定官方 endpoint、关闭自动重试；不自动开通或购买服务。

将票面/XML抽取结果按原有 `SourceRecord` 准入：

```python
from app.compiler_runtime.sandbox import SourceRecord
from erp_agent_odoo.tax_invoice import wire

record = SourceRecord(
    source_id=invoice_ref, content="", kind="record",
    record_model="cn.tax_invoice", record_revision=invoice_revision,
    structured_fields=admitted_invoice_fields,
    provenance={"role": "evidence", "verify_tax_invoice": "aliyun"},
)
manifest["sources"].append(wire(record))
```

`admitted_invoice_fields` 使用以下字段，保留号码前导零；金额、日期和号码使用字符串：

| 票面/API 含义 | 准入字段 |
| --- | --- |
| 发票种类、代码、号码、日期 | `invoice_type`, `invoice_code`, `invoice_number`, `invoice_date`（YYYYMMDD） |
| 未税金额、税额、价税合计 | `amount_untaxed`, `amount_tax`, `amount_total` |
| 购买方名称、税号 | `buyer_name`, `buyer_tax_id` |
| 销售方名称、税号 | `seller_name`, `seller_tax_id` |
| 校验码后六位（适用票种） | `verify_code` |

首版支持 `01/04/10/20/31/32`。01/20 查询使用未税金额；04/10 使用校验码后六位；31/32 使用20位号码和价税合计，不要求旧式发票代码。特殊运输、机动车、区块链等票种返回暂不支持，不猜字段。

在已准入 proposal 中增加 `cn.tax_invoice.review` action，stage 为 `verify`，target 为该 `cn.tax_invoice`；records 的 `values` 包含 `invoice_source_id`。参考可运行样例的 proposal 形状。父 Agent 仍只调用原有 `evidence_reviewer(task_objective, proposal_ref, source_refs)`。原来六类 ERP 模板不新增税票核验要求；Odoo 内部 `INV/...` 编号不能作为中国税票号码查询。

每次新 review 在封存前捕获回执，保存于该 run 的 `tax-verifications/`；后续恢复使用冻结结果。原文哈希、目标快照哈希、时间、RequestId 和字段进入回执。网络失败或缺少凭据也保存证据缺口；需要新查询时创建新 review。持久化完成后不会重复查询；进程若在远端返回与本地写入之间崩溃，不能保证远端计费严格一次。

## 样例和边界

- [阿里云官方文档](https://help.aliyun.com/zh/ocr/developer-reference/api-ocr-api-2021-07-07-verifyvatinvoice)：响应示例原样保存为 `tests/compiler_child/tax_invoice_official_response.json`。保留 `322.XX`、`2018XXXX` 等脱敏字段，不用明细补答案。回放始终标为 `DOCUMENTATION_ONLY`，不能充当真实联网核验成功。
- [税务总局 2024 年第 11 号公告](https://fgk.chinatax.gov.cn/zcfgk/c100012/c5236067/content.html)：样例包含第2—4条对应的数电票字段参考；[上海税务局同文](https://shanghai.chinatax.gov.cn/zcfw/zcfgk/swzsgl/202411/t474123.html)。当前为字段层参考，没有把未下载成功的附件伪装成已完成的视觉票样比对。
- `001` 为接口返回一致，`006` 为信息不一致，`009` 为未查到；限流、权限、异常等归为不可用。未查到、缺凭据、脱敏信息都不能推出假票。状态 N/Y/H/7/8 按已准入审核规则解释，不凭空新增红冲、付款或交付结论。
- 原始回执留在本次 run 内；证明文件只绑定其哈希。保存目录应按原有证据目录权限管理。SDK 异常只保留类型，避免将签名请求写入证据。

```powershell
# 离线展示材料，不调用任何 API
python -m tests.compiler_child.tax_invoice_example
# 离线验证
python -m pytest tests/compiler_child/test_tax_invoice.py -q
# 已配置原有 DeepSeek 测试环境后：一个真实模型 child run，阿里云仍为文档回放
$env:ERP_COMPILER_TAX_INVOICE_ENABLED = '1'
python -m tests.compiler_child.tax_invoice_example --run artifacts/tax-invoice-one-run
```

真实阿里云业务查询仍需用户配置服务凭据并准入一张可查询的完整原票。官方脱敏示例不能完成这项验收。

## 2026-09-07 验收

一个真实 DeepSeek child run：Task Compiler high → Executor low → Verifier high → Kernel，最终 `COMMITTED / NOT_FOUND`，2/2 检查均判证据不足。Executor 实际调用 `verify_tax_invoice` 一次；Verifier 独立指出材料同源、脱敏字段和缺少真实查询。Kernel 离线重放完全相同，零诊断。预期结果只在测试断言中，没有喂给模型。

| Infra 阶段 | 总 token（含缓存输入） |
| --- | ---: |
| Task Compiler | 14,449 |
| Executor | 28,453 |
| Verifier | 21,308 |
| Kernel | 0 |
| 合计 | 64,210 |

模型共5轮请求，缓存输入16,384 token；三个阶段均有完整用量。未调用真实阿里云业务服务。此例与之前的大型 ERP 发票案例不同，不计算节省百分比。

完整离线回归284项通过，其中新增工具边界11项。另有一次首次启动在 SDK 初始化时失败、尚未发出请求：新安装的 openai 2.54.0 与 agents 0.17.8 的 `cache_write_tokens` 类型不兼容；依赖已固定为原实验使用的 2.41.0 / 0.17.4，未修改 Agent 推理逻辑。

本地回执：`artifacts/tax-invoice-official-model-02/`，含 request、plan、checkpoint、模型调用、事件和分阶段 token。官方响应示例 SHA-256：`0929927cbb6bb972882231126efdf44439d8dbb242c99c3f5e4bd568632fe0af`。
