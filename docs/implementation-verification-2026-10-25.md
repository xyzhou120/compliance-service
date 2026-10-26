# 2026年10月25日实施与验证记录

已阅读交接 DOCX 全部正文及表格，将 T01–T07 转成 [开发待办](development-tasks.md)，完成本机可实施的 T01–T06 代码和验证。生产链路适配器已接入现有服务，真实上游、OceanBase 和专家准确率验收尚未完成。

## 主要变更

| 范围 | 文件 | 结果与原因 |
| --- | --- | --- |
| 上游协议与配置 | `upstream.py`、`runtime.py`、`api.py`、`contracts.py`、`__main__.py`、`config/production.example.toml` | 原样报文、可配置 check_url、环境变量令牌与矿井白名单；生产适配器配置后关闭调试写入口；协商前默认仅 running |
| 持久运行与交付 | `ledger.py`、`service.py` | 原文与任务项原子受理、终态重复请求幂等、暂停/恢复及 fence、冻结查询、定版 URL/payload、既有 outbox 投递、回调进度；内置 schema 1→2 迁移 |
| 资料查询与附件 | `oceanbase.py`、`acquisition.py` | 服务端资料白名单、参数化 SELECT、原始行与查询身份、附件逐 ID 收据、流式魔数/哈希校验、失败诊断、DNS/URL 限制与 Windows 安全存储名 |
| 识别恢复 | `sources.py`、`ocr_documents.py`、`parsers.py` | 原生解析与成功 provider 分层缓存，临时失败不固化，失败单元可补，暂停信号不记成解析失败 |
| 业务证据边界 | `daily_clues.py`、`daily_episodes.py`、`fact_extraction.py`、`rules.py`、`scope_checks.py` | 否定/缺证/等级候选不直接判问题；指令不证明实际执行；同日独立事件不强拼；冲突来源不作确定依据，独立确证问题保留 |
| 规则版本 | `rulebook.py`、`data/impact_1023_draft_v2.json` | 核对现存 Excel 并登记新不可变版本，v1 保留；实际差异为 F2，未把 J2/J3 参考结果作为矿井输入 |
| HTTP 回调 | `callbacks.py` | 五类 DTO、原始附件 URL、仅具体问题含 attachmentContent、可配置认证请求头；HTTP 与业务双成功；重试复用 payload/time/URL/幂等标识 |
| 本地调试页 | `debug.html`、`service.py`、`api.py`、`scripts/start-local-debug.ps1` | 手动上游参数、manifest/OceanBase 取数、自动执行、结果/文件/哈希/原文证据展示；固定 `callbackMode=none` |
| 检查项配置 | `inspection_config.py`、`data/inspection_defaults.json`、`api.py`、`debug.html` | 检查项 code、名称、处理器、OceanBase 清单 ID、角色和周期配置化；当前默认 code 保持 `stop_work_unload`；保存带版本 CAS，旧任务冻结配置 |
| 交接文档 | `README.md`、`development-tasks.md`、`acceptance-matrix.md`、`production-integration.md` | 待办、验证边界、环境配置、启动/迁移命令及联调事项 |

删除了 `service.py` 中日报后处理覆盖规则结论的第二判定入口。复用了现有账本、租约、fence、outbox 与解析链，未新增第三方依赖或旁路服务；未修改用户交接 DOCX。

## 验证证据

最终在项目目录执行：

```text
python -m unittest discover -s tests -q
Ran 366 tests in 143.528s
OK

python -m compileall -q compliance_service tests
exit code 0
```

原有基线为 256 项通过，本次最终为 366 项通过。重点新增/扩展的用例位于：`test_upstream.py`、`test_runtime.py`、`test_task_ledger.py`、`test_upstream_integration.py`、`test_oceanbase_acquisition.py`、`test_callbacks.py`、`test_recognition_recovery.py`、`test_evidence_boundaries.py`、日报与规则版本测试。更新 `test_ledger.py` 的 schema 版本断言，并增加旧库迁移与历史内容保持测试。

本地集成通过真实 DOCX 字节与原生解析执行任务受理、注入式清单查询、附件冻结、两项独立运行和持久 outbox；新增页面模拟入口自动执行本次任务，展示文件、哈希、定位和原文证据，并固定跳过回调；验证正在下载时暂停、恢复不重新查询/下载成功附件、终态重复下发不重跑、超时重投相同 payload、业务失败停止投递、附件失败进入详情，以及生产入口鉴权。HTTP 传输测试含真实本机服务收发与重定向拒绝；数据库和正式回调是注入验证，不是外部实收。

项目未配置独立 lint/typecheck 工具，未新增依赖安装这些工具。编译检查与全量测试通过。父仓库当前将整个服务视为未跟踪目录，`git diff --check` 未报告错误，但不能据此声称全部未跟踪代码完成 Git 差异审查；本轮未创建提交。

## 尚未放行的事项

1. OceanBase 实际 DDL、MySQL 兼容模式、选定 DB-API 驱动、只读账号权限与网络未联调。当前 SQL 假设适配视图有 `report_date` 等字段；无这些字段时需按真实 DDL 映射。
2. 正式 itemCode/check_url、执行标识、暂停恢复字符串、回调认证与远端幂等头尚需协议确认；本地选定的组合身份不是上游已承诺协议。
3. 清单索引不能证明资料范围完整，OB 路径保守保持 `source_complete=false`，因此不能仅凭清单齐全给出 pass。年度设计适用期、跨月事件、周验收及作废版本仍需业务证据和明确策略。
4. 未取得专家双标 gold、仲裁和隔离盲测；测试数量不能解释为准确率。当前复杂扫描版式、真实事件召回和全部生产判断未验收。
5. 当前 SQLite 验证面向单机；不代表多机高可用。任务认证是单主体令牌与矿井白名单，正式身份系统需要部署对接。
6. 网络发送先检查 DNS 地址，urllib 建连时仍会解析；正式部署应落实受控域名与出口约束。外部成功尚不能由本地 HTTP 测试证明。

联调启动和配置见 [生产适配器配置与联调说明](production-integration.md)。
