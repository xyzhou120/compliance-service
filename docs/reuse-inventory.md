# 可复用能力清单（document-parser → compliance-service）

状态：2026-10-26。来源仓库：`document-parser`（第三方团队内网 GitLab）。
**前置条件（未满足前不得把代码搬进来）**：
1. 对方给出一次**完整提交**的 commit SHA（当前工作区有 55 个已修改 + 59 个未跟踪文件，核心文件不在任何提交里）；
2. 书面确认授权与知识产权归属（仓库无 LICENSE，`authors = "Your Team <team@example.com>"`）。

## Tier 1：直接 vendor（低耦合、实测可用）

| 能力 | 文件 | 用途 | import 代价 | 实测证据 |
| --- | --- | --- | --- | --- |
| Office 逐格归一化 | `backend/app/services/office_source_adapter.py` | xlsx/xls/docx → 逐格 units（`raw_value/stored_value/formula_cache_status`）+ `merged_ranges` + 隐藏行列 + `coverage/can_claim_absence`；图表/嵌入对象记 `EMBEDDED_OBJECT_NOT_READ` 缺口 | +169 模块（含 asyncpg/minio 只因包 `__init__` 副作用，可裁剪） | 两家矿同名清单表头完全不同仍无损读出；带图表 xlsx → `content_complete=false`；多行合并表头保留 3 段 merged_ranges |
| 数据契约 | `backend/app/schemas/extraction_contracts.py` | `PdfLocator / SpreadsheetLocator(单元格区间+合并区) / WordLocator(XML部件路径) / ImageLocator(像素框)`；`TemporalValue` 精度规则 | **+0 模块**（纯 pydantic） | 直接 import 成功；可作我们 canonical 文档模型的基线 |
| 页存在性回执 | `backend/app/services/document_metadata/extractor.py` | 用 `get_drawings()` 判"页面非空"，不把空 OCR 当空白 | 低 | 代码评审确认；其 `pdf_page_presence@1` 回执结构可借鉴 |

## Tier 1.5：移植语义（照设计写，不照代码搬）

| 语义 | 出处 | 我们要落成的规则 |
| --- | --- | --- |
| 不可读页不得断言不存在 | `services/candidate_retrieval.page_readable` | 页级 `unreadable` → 禁止产生 `not_found`，只能 `incomplete` |
| 闭合才允许 not_found | `services/field_completion` | 只有"范围闭合 + 无未决候选 + 无风险码"才允许"确认不存在" |
| 三 complete 才 absent | `schemas/extraction_contracts.TargetCoverage` | 目标级缺省值必须由 content/target/binding 三个 complete 支撑 |

## Tier 2：只作第二意见（可选）

- `GET /api/v1/parse/runs/{id}/results`（`universal_result/1`）：只用 `semantic_state` / `coverage_complete` / `available_for_downstream` 三个字段做交叉校验。
- **禁止**读取 `confidence` / `mean_confidence`（平台自身也禁止把它当准确率）。
- `/api/v1/publications/{id}` 恒返回 `PARTIAL`（`publication_delivery/1` 无生产者），不得作为交付契约。

## Tier 3：明确不复用

| 不要 | 原因 |
| --- | --- |
| 平台服务（PostgreSQL+Redis+MinIO+Celery） | 服务身份被锁 publications、批量通道锁 legacy、100 份 3–15 h |
| `parsers/pdf_parser.py` | 只做文本层；真实样本 7/7 是图像 PDF，对它完全无用 |
| rule pack / 清单抽取链 / legacy 引擎 | 与我们 9 个资料组的字段档案耦合，且没有上游判定 hook |
| AGENTS.md 的双写义务 | 我们无法维护它 76KB 的契约文档 |

## 已知缺口（不要指望它补）

图表识别（零实现，且图表页会被判"扫描页"送 OCR、OCR 文本混入正文参与候选）、多层表头（表头恒取第 0 行）、手写（零支持）、真实 OCR 读图（零测试）、字段准确率（无裁决 gold，评测器返回 `NO_ADJUDICATED_GOLD`）。

## 与我们私有 OCR 服务的关系

图表检测、表格 HTML（含 colspan/rowspan）、指数保真（LaTeX）、行级置信度由**私有 OCR 服务**提供（`/layout_detection` 0.1–0.7 s、`/vl_ocr`、`/ocr`、`/table_cell_detection`），不需要 document-parser 的 PDF 侧归一化。
