# 开发交接验收矩阵

状态定义：`已具备`=当前代码/测试有直接证据；`部分`=有本地实现但存在明确范围限制；`待联调`=需真实上游、OceanBase、回调或专家样本；`未开始`=尚无实现证据。

| 编号 | 验收主张 | 证据/测试入口 | 当前状态 | 放行限制 |
| --- | --- | --- | --- | --- |
| T01 | 候选、否定句、查无记录不越级为 `no_pass` | `test_daily_clues.py`、`test_evidence_boundaries.py`、`test_rulebook.py`；默认 `impact-1023-draft-v2` | 已具备本地证据 | 三个反例已回归；源 SHA256 为 e3fc04ca7346bb91bd8868b24efcd42155df1ae68c2a41a5f01a63a0da1e1247；法规一致性仍需业务审定 |
| T02 | 原样任务受理、每项独立、重复可解释、控制响应与结果分离 | `upstream.py` 契约测试；`api.py` 入口测试；任务原文快照 | 部分 | 正式 check_url、status、执行标识、暂停/恢复和认证待上游确认 |
| T03 | 只读白名单查询、参数化、查询快照、附件 ID 去重/版本保留 | `oceanbase.py` repository 测试；查询快照结构测试 | 部分 | 真实 OB DDL、驱动、账号权限和行字段待验证 |
| T04 | 文件字节版本、页/行/单元格定位、候选质量门可重放 | `sources.py`、`fact_extraction.py`、`ocr_documents.py` 测试 | 部分 | 任意版式和扫描件准确率未验收；模型失败恢复需真实轮次 |
| T05 | 两项规则按前件、时序、适用范围和事件归属判定 | `rules.py`、`association.py`、`daily_episodes.py`、跨文件测试 | 部分 | 真实业务 gold、跨月适用、版本作废和专家复核待完成 |
| T06 | DTO 字段、五类状态、URL 快照、2xx+业务成功、同 payload 重试 | `callbacks.py` 契约/注入传输测试；`ledger.py` outbox 测试 | 部分 | 正式回调域名、认证、幂等和实收回执待联调 |
| T07 | 真实 gold 双标、盲测分层指标和放行阈值 | `evaluation.py`；`.local/validation` 仅合成/历史记录 | 待联调 | 当前不能声称生产准确率或 M6 通过 |
| T06-Debug | 页面模拟任务、自动执行、文件及证据查看、不回调 | `test_debug_tasks.py`、`test_runtime.py`、浏览器本地验证 | 已具备本地证据 | local/test；真实 OB 配置仍需验证 |
| T03-Config | 检查项 code、清单 ID、资料周期和月份策略可配置，配置版本进入任务快照 | `test_inspection_config.py`、`test_inspection_config_api.py`、`test_configured_execution.py`、`test_oceanbase_acquisition.py` | 已具备本地证据 | 真实环境的 file_name_id、report_time_month 字段和业务周期标签待联调 |

## 必测场景

- 小于、等于、大于阈值；不同方法、单位和比较方向。
- 只有图例/泛化等级、否定句、明确未安排、资料未找到、OCR 不可读。
- 同页多工作面、多日期、同日两个独立事件、跨日显式关联、解除后再次预警。
- 同字节重复附件、同名不同 ID、相反版本、作废/权威版本、缺一天资料。
- OB 查询失败、附件魔数/哈希失败、OCR/视觉临时失败与单元重试。
- HTTP 200 但业务失败、超时后远端已收、回调重试同 payload、回调失败不重跑检查。
- 跨矿、跨运行、跨环境证据引用；错误矿井或工作面不能闭合事件。

## 变形测试原则

删除必要证据不能提高结论；替换矿井/工作面不能闭合；调整文件顺序不改变语义；重复字节不能制造新事件；等价单位转换不改变合法比较；把“明确未安排”替换成“未找到记录”应降为缺证。

## 当前可确认与不可确认

已可在本机验证：现有 SQLite 账本租约/恢复、输入快照和证据归属校验、PDF/DOCX/XLSX 原生解析、合成样本的五类终态、mock outbox 基本语义以及静态代码/单元测试。

仍不可由本地证据确认：真实 OceanBase 查询和附件清单一致性、生产身份授权、正式 check_url 与任务状态机、真实回调业务成功与幂等、扫描件/复杂版式业务准确率、专家 gold 和 M6 盲测放行。

任何交付报告必须分别写出“本地模拟通过”“真实环境已联调”“等待外部条件”，不得用 `completed`、模型置信度或静态页面代替业务正确性证明。
## 2026-10-25 实施状态

- T01：规则语义入口已统一，候选/否定句/查无记录不会直接产生 `no_pass`；已核对当前 Excel 并登记新 revision，旧 v1 保留。
- T02：任务适配、任务账本、幂等指纹、控制状态和生产令牌入口已实现；正式 status、执行标识、check_url 与认证仍需上游确认。
- T03：OceanBase 注入式只读 repository、资料白名单、查询快照、附件按 ID 下载/哈希收据已实现；真实 DDL/驱动/权限未联调，`source_complete=false`。
- T04/T05：原生解析、OCR/provider 失败单元重试、证据边界和事件关联已有本地实现与回归测试；复杂真实版式和专家样本未验收。
- T06：回调 DTO、URL 策略、持久 outbox、HTTP/业务成功确认和受限重试已实现；T06-Debug 页面入口已实现，固定不回调；正式回调域名、认证、幂等实收未验证。
- T07：评测框架已存在；真实 gold、双标仲裁、独立盲测及放行阈值尚未提供。

本次最终执行 `python -m unittest discover -s tests -q`：366 项通过，141.246 秒；`compileall` 通过。详见 [实施与验证记录](implementation-verification-2026-10-25.md)。
