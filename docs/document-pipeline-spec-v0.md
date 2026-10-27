# 文档归一化与证据化抽取规格 v0（两项检查项首期）

状态：**v1**，2026-10-26（v0 → v1 依据：最新提交 `880ff40` 复评 + 31 页真实样本实测 + 私有 OCR/VLM 端点能力探测）。

**变更记录**
- v1：加入"最新代码复评"（`880ff40` 修了什么、还缺什么）；通道策略由"PaddleOCR-VL 主通道"改为"VL 出结构 + 经典 OCR 补召回 + Qwen 仅裁块"；写入 31 页实测基线与三条硬规则；补"实施顺序与验收"。
- v0：首版分层规格（页级路由、四道闸、字段档案 v0、溯源报文、幂等复用）。范围：**冲击危险停工撤人与卸压**、**冲击地压解危效果检验** 两项检查项。
本文件只定义"能不能说、能说到什么程度"的机制与契约，不定义判定规则本身（判定规则以 `compliance_service/rules.py` + `rulebook` 为准）。

## 0. 已确认的前提（不再讨论）

| 项 | 结论 |
| --- | --- |
| 检查项 | 先做 2 项；后续叠加，新增检查项只应改配置与规则，不改管线代码 |
| 清单 | 现阶段按 `data/inspection_defaults.json` 的 9 个资料组实现；**清单增删属于配置变更**，不作为代码前提 |
| 判定字段 | **以现有规则为准**（字段集合见第 5 节，来自 `fact_extraction.FIELD_ALIASES` 与 `rules.py` 实际消费） |
| 清单识别 | 配置检查项时显式映射；自动识别只作兜底告警，不参与判定 |
| 低质量页 | 不进最终结论展示；**必须在溯源链路保留可定位的"无法判定"记录** |
| 算力 | 私有部署：PaddleOCR-VL 1.6（主通道）+ Qwen-VL 27B（第二通道/图表/手写） |
| 人工复核 | 有人力；采用"复核即标注"，不建独立标注流程 |

## 1. 设计不变量（先于一切实现）

1. **INV-1 结论只能来自可核验证据**：任何参与判定的数值，必须能定位到 `file_hash + 页 + 区域/单元格`，且通过第 4 节的闸门。
2. **INV-2 读不出 ≠ 不存在**：页面不可读、低质量、通道冲突时，不得生成"该值不存在"的断言，只能生成"无法判定（带原因码）"。
3. **INV-3 文档级失败不得吞掉页级事实**：一份文档部分页失败时，其余页仍可形成事实，失败页单独记账。
4. **INV-4 幂等**：同一 `(file content hash, 抽取契约版本, 管线版本)` 的抽取结果必须可复用且结果一致；复用不得改变已有结论。
5. **INV-5 判定与解析分离**：解析层只输出"事实 + 证据 + 质量"，不做任何合规判断。

## 2. 页级路由（L0）

文档级判定不成立：同一份 PDF 里可能同时存在原生文本页、贴图页、扫描页、矢量图表页。**逐页分类 + 逐页选路**。

### 2.1 页类型

| 类型 | 判定信号（按顺序短路） | 走向 |
| --- | --- | --- |
| `native_text` | 文本层字符数 ≥ 200 且无大面积贴图（绘制图像面积占比 < 15%） | 直接取文本块（毫秒级，确定性） |
| `native_table` | 文本层存在且 PyMuPDF `find_tables` 命中，或字符呈行列对齐 | 表格结构重建（第 3.2 节） |
| `vector_chart` | 矢量图元密集（`get_drawings()` 数 > 20）且文本稀疏（< 300 字），且图元包围盒覆盖页宽 > 40% | 图表直读（第 3.3 节） |
| `pasted_image` | 文本层稀疏（< 200 字）但存在**实际绘制**的图像（`get_image_info()` 非空） | 图像归一化 → PaddleOCR-VL |
| `raster_scan` | 文本层 ≈ 0 且图像覆盖 ≥ 60% 页面积 | 图像归一化 → PaddleOCR-VL |
| `handwritten` | 上述任一页型中，手写检测命中（区域级标签） | 主通道 + Qwen-VL 判定 + 人工 |
| `mixed` | 同页同时满足 native_text 与 pasted_image 条件 | **分区处理**：文本区直读、图区分流 |

**必须注意的历史坑**：不能用 `page.get_images()`（资源字典里的图像对象）判断"是否有图"——真实样本里出现过"每页 32 个图像对象但实际绘制 0 张"的 Word 导出 PDF。判定一律用 `get_image_info()` / `get_image_rects()`（实际绘制）。

### 2.2 页质量分档

| 指标 | 计算 | 分档 |
| --- | --- | --- |
| 有效 DPI | 图像像素宽 / 页宽(pt) × 72 | `good ≥ 200`、`fair 120–200`、`poor < 120` |
| 清晰度 | 灰度拉普拉斯方差 | `good ≥ 300`、`fair 100–300`、`poor < 100` |
| 倾斜 | 霍夫/投影方差估计角度 | `good ≤ 0.5°`、`fair 0.5–2°`、`poor > 2°` |
| 图形密度 | 矢量图元数 / 文本字符数 | 用于区分"表格线"与"图表"（见 3.3） |

分档结果只影响**结论强度**，不影响是否抽取：`poor` 页仍尝试抽取，但抽取结果默认只能进入候选，不得直接参与判定。

## 3. 抽取通道

### 3.1 通道分工

| 页型/任务 | 主通道 | 第二通道 | 通过条件 | 实测时延 |
| --- | --- | --- | --- | --- |
| native_text / native_table | PyMuPDF（无模型） | 无 | 结构自洽即通过 | < 50 ms |
| 版面与区域划分（所有页） | `/layout_detection` | — | 输出 `chart/table/figure_title/text` + bbox | 0.2–0.7 s |
| raster_scan / pasted_image 的**表格结构** | PaddleOCR-VL `/vl_ocr`（HTML + colspan） | 经典 `/ocr` 文本行（补召回） | 结构自洽 + 与经典 OCR 无冲突 | 1.3–20 s |
| raster_scan / pasted_image 的**正文召回** | 经典 `/ocr`（行级 + 置信度） | PaddleOCR-VL | 关键数值需双通道一致 | 0.6–8 s |
| **含指数的数值**（能量 J、×10^k） | PaddleOCR-VL（LaTeX 保真） | Qwen 裁块 | **禁止**由经典 OCR 单独给出 | 同上 |
| 关键数值字段（阈值/实测值/时间/人数） | PaddleOCR-VL | **Qwen-VL 裁块复读同区域** | 规范化后逐字一致 → `verified`；否则 `CHANNEL_DISAGREE` | Qwen 裁块 6–20 s |
| 位图图表 | **Qwen-VL 裁块**（整页调用不可用：实测中位 138 s） | 与同页表格原值交叉 | 仅"有数据标签且四重校验通过"才可出数值；否则只出图表语义 | 6–20 s |
| 矢量图表 | 直读图元 + 刻度文本（无模型） | 与正文/表格交叉 | 几何自洽即通过 | < 100 ms |
| 手写区 | Qwen-VL 裁块判定 + 识别 | — | 识别不确定 → `handwritten_unresolved`，字段转人工 | 6–20 s |

### 3.4 通道纪律（由实测得出，不是偏好）

1. **Qwen 只能读裁块，不能读整页**：整页 1500×2000 JPEG 实测中位 **138 s**、最长 180 s 超时，且只覆盖 PaddleOCR-VL 数字的 67/126；裁块 6.5 s。
2. **PaddleOCR-VL 会在部分照片/扫描页漏读**：实测某页 VL 仅 65 字而同页经典 OCR 363 行。因此 VL 输出异常稀疏（且 layout 判定该页应有 table/text）时必须视为**部分失败**：先重试 `/vl_ocr_no_layout`，仍稀疏则标 `VL_UNDERREAD` 并降级。
3. **经典 OCR 只作召回补漏**：实测经典 OCR 拿到 414 个数字、VL 126 个、交集 103。数字类事实只认交集（或双通道一致），任一单通道独有的数字进候选。
4. **输入预处理**：统一长边 2000 px、JPEG q88 后再送检；原始 3000×4000 PNG（7.6 MB）会稳定超时。
5. **并发纪律**：OCR 服务与视觉模型各 **≤2 并发**（实测 6 并发直接把 nginx 上游打成 502），串行推进；429/5xx 退避 + 熔断。

### 3.2 表格结构重建（关键能力）

- 表头：**表头树**，不是第 0 行。多行表头 + 合并单元格 → 归一为 `column_id → [层级标签]`；
- 合并区：保留 `rowspan/colspan` 与 `merged_range`；合并标签按行/列下填，但**必须记录下填来源**（不得伪造单元格文本）；
- 跨页表：以"续表/表号/表头一致"三条件绑定，缺一不可；
- 列语义对齐：列 → 字段映射来自"字段档案（第 5 节）+ 同义词 + 上下文"，映射不确定时输出 `COLUMN_UNBOUND`，不得猜测。

### 3.3 图表政策

- **矢量图表**：柱=填充矩形、线=路径、轴=长直线、刻度=文本 + 坐标线性映射 → 确定性重建为 `{axis, unit, categories[], series[], values[]}`。此路径**可用于数值**（几何自洽 + 刻度单调）。
- **位图图表**：Qwen-VL 读为数据表，附加校验：①刻度单调递增；②轴为线性（等距）；③柱高/点位置与读值比例一致；④与同页表格原值一致。四项全过 → `chart_verified`；否则只记"存在图表 + 标题/轴名"。
- **默认策略**：图表数值**不作为判定唯一来源**（除矢量直读）。此策略为配置开关，按清单开启。

## 4. 四道闸（"不误判"的实现）

1. **采集质量闸**：`poor` 页产出的数值 → 只能 `candidate`；`fair` 页 → 需双通道一致。
2. **抽取自洽闸**：标签-值必须**邻接绑定**（同一行/同一单元格/标签后 32 字符内），不是"窗口内出现过"；单位与量纲必须匹配（百分比 ≠ 无量纲；MPa/kPa 需换算并记录换算）；数值范围必须合理。
3. **跨源印证闸**：同一语义值在多处出现时，必须一致才能升级；不一致 → 保留冲突，禁止二选一。
4. **发布闸**：只有 `verified` 事实可进入规则比较；`candidate` 只能回答"是否存在该材料/该表"，不能回答"数值是多少"。

## 5. 字段档案 v0（按资料组）

字段名以 `fact_extraction.FIELD_ALIASES` 的 canonical key 为准（45 个）。

| 资料组（file_name_id 前缀） | 产出事实类型 | 关键字段 | 约束 |
| --- | --- | --- | --- |
| 防冲工作日报表 `rock_ledger_1k8hqc4_rpt_wh5nwe` | event / action / measurement | `trigger_type, workface, time, stage, method, metric, point, value, unit, action, status, immediate, actor_role, target_scope, route_conforms, withdrawal_area, expected_personnel, withdrawn_personnel, event_time_reference` | 数值必须双通道一致；时间精度不得下推（日级不得补时分） |
| 调度日报 `base_base_ledger_1falhnq_x_smfptn` | action / statement | `action(停工/断电/撤人/复工), time, status, dispatch_source_role` | 只作执行证据，不与日报混算 |
| 生产矿长（总工程师）日分析记录 `rock_ledger_1k8hqc4_rec_5sdl97` | statement | `hazard_confirmed, risk_grade, signer, signature_present, signed_at, notified, notification_time` | 签字缺失不得推断为"无签字"，需原文否定证据 |
| 生产组织通知单 `rock_ledger_1k8hqc4_x_1t0mx6a` | action | `action(dispatch_release/复工), time, approved` | 复工须能绑定技术确认 |
| 防冲钻孔验收记录 `rock_ledger_1k8hqc4_rec_axez36` | action | `parameters, acceptance_passed, acceptance_signer, acceptance_time, personnel_count, unrelated_equipment_stopped` | 参数须与设计要求逐项比对 |
| 冲击危险性预警临界指标 `rock_rpt_181s4w6_x_jqiyc1` | requirement / threshold | `method, metric, point, value, unit, approved, valid_from, valid_to, target_scope` | **必须 approved=true 且在有效期内**，否则阈值不可用 |
| 防冲设计 `rock_x_iverp1_x_1t8xner` | requirement | `relief_methods, relief_parameters, relief_area, withdrawal_area, max_personnel, unrelated_equipment_stop_required, technical_head, approved, valid_from, valid_to, target_scope` | 同上；解危方法须在全集内 |
| 工作面危险性评价 `rock_rpt_181s4w6_mining_rpt_1vaqrs5` | requirement | `impact_risk, risk_grade, approved, valid_from, valid_to, target_scope` | 适用范围须覆盖事件工作面 |
| 煤层危险性评价 `rock_rpt_181s4w6_rpt_1f3n8vh` | requirement | `impact_risk, risk_grade, approved, valid_from, valid_to` | 同上 |

> 年报类（临界指标/设计/两项评价）共 4 份，均带有效期；**它们是最适合做提取复用的一类**（见第 7 节）。

## 6. 溯源与拒因报文

低质量页不进结论展示，但必须可定位、可解释、可统计。

```json
{
  "page_ref": {"file_id": "...", "page": 7, "bbox": [x0, y0, x1, y1]},
  "page_type": "raster_scan",
  "input_quality": {"dpi": 98, "sharpness": 64, "skew_deg": 3.2, "tier": "poor"},
  "status": "partially_extracted",
  "reason_codes": ["LOW_DPI(98)", "SKEW(3.2)", "OCR_CONF_BELOW(0.61)", "CHANNEL_DISAGREE(3.42|3.48)"],
  "attempts": [
    {"channel": "paddleocr_vl", "version": "1.6", "conf": 0.61},
    {"channel": "qwen_vl", "model": "...", "conf": null}
  ],
  "linked_fields": ["measurement.value@402101工作面"],
  "extracted": {"tables": 1, "cells_verified": 12, "cells_rejected": 3}
}
```

展示分层：
- **结论页**：只显示"共 N 页，其中 M 页无法判定（影响字段：…）"，不展开细节；
- **溯源页**：字段 → 页 → 区域 → 通道 → 是否双通道一致 → 拒因；
- **内部盲区标记**：`pass` 结论若存在"被拒且理论上可能容纳反证"的页，内部保留 `blind_spot: true` 供复核员审计（不对外）。

## 7. 提取幂等与复用（后续迭代方向，先固化契约）

**目标**：同一份文档在不同检查项、不同任务触发中只提取一次。

**缓存键**：`sha256(file bytes)` × `extraction_contract_version` × `pipeline_revision`（含模型版本/提示词版本/参数）。规则版本变化**不**触发重抽取（规则消费事实，不改变事实）。

**复用收益（按现有 9 个资料组估算）**：
- 跨检查项复用：7 个日/周级资料组被 2 项检查共享 → **解析量减半**；
- 年报复用：4 份年报 × 每矿每年 1 次，被 12 个月的任务反复引用 → **重复解析趋近于 0**；
- 重跑复用：任务重试、规则调整、补充证据均不重解析。

**必须如实说明的成本事实**：真正的量在**日更类**（防冲日报/调度日报/日分析/通知单，按 daily 计，每矿每月约 120 份）。这部分**无法靠复用摊薄**（每份内容都不同），只能靠两件事控制：
1. 跨检查项复用（×0.5）；
2. **事件驱动选取**：只解析"发生预警/动力现象的事件日及其前后窗口"的日报，而不是整月全量——这是规则层面的优化，等能力就绪、跑出效果后再评估，不在本期实现。

## 8. 基线测试方案（先于实现，用于定标）

**样本**：30–50 页，覆盖 ①原生文本页 ②原生表格页 ③整页贴图（如 `防冲工作日报表 (3)_副本2.pdf` 的 98 DPI 照片页）④含手写页 ⑤含柱状/折线图页。

**指标**：
| 指标 | 定义 | 用途 |
| --- | --- | --- |
| 字符准确率 | 1 − 编辑距离/人工字符数 | 判断低分辨率可用性 |
| 数值准确率 | 数值字段完全正确的比例 | **核心指标**（决定能否进判定） |
| 表格结构 F1 | 单元格边界 + 表头层级匹配 | 判断表格重建方案 |
| 拒因分布 | 各 reason_code 占比 | 判断瓶颈在采集还是算法 |

**通过门槛（建议）**：原生页数值准确率 ≥ 99.5%；照片页（98 DPI）若数值准确率 < 95%，则照片页数值一律不入判定，只形成"存在性/待人工"结论，并把采集质量治理列为并行工作。


### 8.1 首轮实测基线（2026-10-26，真实样本 31 页）

样本：document-parser 仓库 54 份真实冲击地压文档 + 用户提供的 98 DPI 照片页；渲染 200 DPI、长边压到 2000 px、JPEG q88；OCR 服务与视觉模型各 2 并发（6 并发会把 OCR 服务打成 502）。

| 指标 | 实测值 |
| --- | --- |
| 版面检测 | p50 **0.1–0.3 s**，直接给出 `chart / table / figure_title / text` 区域（照片页 18 chart + 4 table） |
| PaddleOCR-VL | p50 **1.7 s**，扫描页 16.7–17.3 s；表格输出 HTML，合并单元格以 colspan/rowspan 保留（某页 181 单元格 / 19 span） |
| 经典 OCR | p50 **1.0–2.8 s**，行级置信度可用（扫描页中位 0.74–1.00） |
| Qwen 整页 | **中位 138 s**（最长 180 s 超时）→ 不可用于整页 |
| Qwen 图表裁块 | **6.7–7.9 s** / 块 |
| 数字一致性 | 结构良好的页两通道 **100% 一致**（33/33、22/22）；全部页 VL 数字 **84%** 被经典 OCR 覆盖 |
| 指数保真 | VL（LaTeX）0 次丢失；经典 OCR 在 3000 px 高分辨率下曾丢 4 次（`4.92×10³ → 4.92×10J`） |
| 图表数值 | 抽查 2 个趋势图裁块：Qwen 均返回 `type=line, data_labels_present=false, values=[]`，置信度 0.6–0.9 —— **无数据标签的曲线图不会被估读** |

**由此确立的三条硬规则**（已写入 §3.4）：
1. 数字类事实只认"双通道一致"或"高质量页单通道 + 结构自洽"；单通道独有数字进候选。
2. VL 输出显著稀疏（且 layout 判定该页有 table/text）→ 必须重试 `/vl_ocr_no_layout`，仍稀疏则标 `VL_UNDERREAD` 降级，禁止当作"本页无字"。
3. 图表数值默认不参与判定；仅"有数据标签 + 四重校验通过"才可产出图表数值（当前样本中未出现）。

## 9. 未定项（不阻塞本期）

1. 9 个资料组的"必收/可选"归属（影响"资料不完整"口径）；
2. 清单是否缩减：**等能力改造完成后按实际核验效果再定**；
3. 事件驱动选取日更日报的策略（依赖第 7 节的复用能力上线）；
4. 图表数值是否对特定清单开放为判定来源（默认关闭）。

## 10. 最新代码复评（`880ff40`，2026-10-26）

**已修**：测试从"1 项红"转绿（现 416 项 OK）；识别阶段页级隔离、并发上限 1–2、逐页进度事件、解析结果按文件 spool；缓存版本改为常量。

**本次一并修复（见代码）**：
| 项 | 落点 |
| --- | --- |
| P0-1 单位刻度族：% 与无量纲拒绝换算 | `rules.py` `_UNITS`/`_quantity`/`compare` |
| P0-2 双模型一致不得把页面判为可读 | `ocr_documents.py`（改为 `model_transcription.requires_review`） |
| P1-3 观测窗口接入 scope 判定 | `scope_checks.py` `_inside_window` |
| P1-4 格式提示与证据缺口分层 | `fact_extraction.py` `advisories` + `sources.py` |
| P1-5 识别页预算可配置（默认 200） | `ocr_documents.py` |
| P1-6 去掉规则表未引用的第 27/59 条 | `rules.py` |

**仍未处理（不在本规格范围）**：任务身份不含矿井 / 无 taskId 静默吸收、投递预算与补投通道、单行快照内存、PDF 图表区无弃权（由私有 OCR 的 `layout_detection` 补）。

## 11. 实施顺序与验收

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| P0 语义止血 | 上表 6 项 + 8 个回归用例 | **完成**（416 测试绿 + compileall） |
| P1 归一化底座 | `compliance_service/ingest/`：PageReceipt + 页级路由 + 通道调度器（2 并发/熔断/降采样）+ 双通道一致门禁 + VL_UNDERREAD 降级 | 进行中 |
| P2 字段抽取 | canonical 文档模型 + 表格重建（表头树/续页/合并区）+ 字段档案 v0 落码 + 接 `rules.py` | 待启动 |
| P3 复用与复核 | 内容寻址归一化缓存 + 复核三动作 + 拒因看板 | 待启动 |
| P4 能力扩展 | 图表数值（矢量直读/有标签位图）、手写专项、变体库 | 待启动 |

## 12. 实施进度（2026-10-26 晚）

| 阶段 | 交付 | 测试 |
| --- | --- | --- |
| P0 语义止血 | 单位刻度族、扫描页不洗白、观测窗口、缺口分层、页预算、法条清理 | ✅ |
| P1 归一化底座 | ingest/：PageReceipt、页级路由、受控通道（2 并发/熔断/重试）、VL 欠读重试/降级、双通道数字门禁、回执入快照（开关 COMPLIANCE_PAGE_RECEIPTS） | ✅ 16 用例 |
| P2 字段层 | canonical.py（页/表/格/定位 + span + 表头树 + 续页）、fields.py（9 资料组 × 45 字段 + 邻接绑定 + 量纲）、facts_from_document 接 rules.py、units.py 单一真源 | ✅ 13 用例 |
| P3 复用与复核 | cache.py（内容寻址：文件哈希×契约×管线）、review.py（确认/修正/拒绝 → gold + 精度）、diagnostics.py（拒因分桶与优先级） | ✅ 7 用例 |

全量回归：460 项（P0 起算基线 407 → 460），compileall 通过。

## 13. P2-e 实测（真实 98 DPI 照片页，2 页）

| 页 | 页类型 | 质量档 | 状态 | 区域 | VL字 / OCR行 | 双通道一致数字 | 指数丢失(VL/OCR) | 表格 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | vector_chart | fair | extracted | 8 chart + 1 table + 1 title | 4117 / 189 | 11 | 0 / 11 | 1 |
| 2 | vector_chart | fair | partially_extracted | 15 chart + 1 table + 5 text | 1389 / 242 | 22 | 3 / 1 | 1 |

观察：
1. 经典 OCR 在真实照片页丢 11 个指数（如 4.92×10³ → 4.92×10J），与硬规则一致：指数类数值只能取 VL/Qwen；
2. 页 2 触发 VL_UNDERREAD 并降级为 partially_extracted，未伪造成完整；
3. 两张表都能重建，但字段档案没有绑定到任何字段——该页表格的表头是"全矿范围内 / 403109工作面 / …"这类区域列，不是"方法/指标/测点/实测值"这种字段列。

结论（需要业务确认）：一个文档里往往有多张表，只有其中一张承载目标字段；"文档内哪张表对应哪组字段"属于清单级业务映射，需要业务方确认后才能把绑定跑通。

## 14. 真实页表格结构的实测结论（2026-10-26 深夜）

在真实的 98 DPI 照片页上（用户样本两张图）：

1. 版面检测能正确定位表格区域（bbox 12,63,1465,1018，score 0.46）；
2. **但 vl_ocr 对该表只返回单个单元格**：<table><tr><td>微震监测统计</td></tr></table>（39 字节）；
3. **table_cell_detection 仍能给出 237 个单元格框**（几何可用，但没有文字）；
4. **/ocr 只返回 rec_texts + rec_scores，没有框**（无法把文本分配进单元格）；
5. vl_ocr_no_layout 返回一个 label=ocr 的大文本块，内容包含表格文字（"微震监测统计 / 全矿范围内 403109 工作面 …"）。

推论（下一阶段必须处理）：
- 照片页的"表格"在 VL 路径上会退化成单格，**不能假装拿到了网格**；
- 可行的替代是"区域文本绑定"：用 vl_ocr_no_layout 的整块文本，按"标签 + 值"邻接规则抽取（与既有 fact_extraction 的文本路径同源）；
- 若要恢复真实网格，需要 OCR 服务补齐"带框文本"能力（/ocr 返回 rec_boxes 或新增接口），否则 cell 框与文字永远无法对齐。

结论：**字段绑定在照片页必须走"文本行绑定"这条兜底路径**；表格网格只在 VL 成功解析出多格 HTML 时使用。这条已写入下一阶段任务。

## 15. 真实文件二次实测（2026-10-27 上午，含文本绑定兜底）

样本：胡家河矿《冲击危险性预警临界指标》PDF 前两页（170 DPI 渲染）。

| 页 | 页类型 | 状态 | VL 字数 | 表格 | 表达标分 | 文本绑定事实 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | raster_scan | partially_extracted | 51 | 0 | 0 | 0 |
| 2 | raster_scan | partially_extracted | 1017 | 1 | 0 | 0 |

结论与修正：
1. **文本绑定必须只认"整标签+短值"**：初版把法规段落里的"…微震监测方法》…："整段绑定成 method，属误抽；现已加 MAX_LABEL_LEN=12 且要求 exact 别名匹配，误抽消失（0 facts）。
2. **两页真实扫描件的表格都拿不到网格**：VL 表格 HTML 退化为单格（0 分），/ocr 不返回框，因此无法做单元格级绑定。
3. 因此真实扫描件当前只能"安全地抽不到"（0 事实、0 误判），但离可用召回还差一大截——瓶颈在 OCR 服务的输出能力，不在规则层。

待办（P2-g）：当 VL 表格退化时，用 Qwen-VL 对表格区域裁块做 markdown 转写（已验证可产出结构化表格），再走同一套四重校验；这是在不改造 OCR 服务前提下恢复表格召回的最短路径。

## 16. 表格读法 schema + 裁块视觉转写（2026-10-27，D3/D4 已确认后落地）

新增：
- compliance_service/table_schema.py：声明式"表格读法"（色阶矩阵：行标签列 + 蓝/黄/橙/红阈值列）；
- compliance_service/imageops.py：表格区域裁块（Pillow 已提为运行时依赖）；
- compliance_service/vlm.py：视觉转写的门禁修正——合并单元格从"拒绝"改为"警告"，由 binder 按右缘对齐吸收；
- ingest/pipeline.read_page(vision=..., cropper 由上层提供)：VL 表格退化为单格时调用视觉转写，成功标 TABLE_FROM_VISION、失败标 VISION_TABLE_UNVERIFIED。

真实文件实测（胡家河《冲击危险性预警临界指标》第 2 页）：

| 步骤 | 结果 |
| --- | --- |
| 版面检测 | 表格区域 [199,853,1213,1242]，score 0.96 |
| 裁块 | 141 KB |
| Qwen 表格转写 | table_verified（警告 HAS_SPANS），10 行 × 5 列 |
| 色阶 schema | 28 条阈值事实（metric=浅基点，grade=蓝/黄/橙/红，value=/ ≥12 ≥14 …） |
| 耗时 | 整页直送 92.7 s / 183 s → 裁块后 8.6 s（约 10–20 倍） |

结论：**"页 → 版面 → 裁块 → 视觉表格 → 表格读法 → 带定位的阈值事实"整链已在真实文件上跑通**；
剩余待业务确认的只是其余 8 个资料组的"表格读法"模板，以及 checker 层如何使用 grade 阈值（按事件危险等级取档）。

## 17. 20 页真实样本基线 + 跨矿版式变体（2026-10-27）

样本：从 4 家矿的 20 页真实扫描/图片页（170 DPI 渲染）跑 scripts/real_sample_chain.py（VL 通道，未开视觉兜底）。

| 指标 | 结果 |
| --- | --- |
| 页数 | 20（其中 8 页解析出表格） |
| 事实条目 | dispatch_daily 32、design 1、threshold 0 |
| 页状态 | partially_extracted 19、extracted 1 |

关键发现（真实数据证实"同名清单跨矿结构完全不同"）：

| 矿 | 临界指标表结构 |
| --- | --- |
| 胡家河 | 应力在线预警模型 / 掘进工作面 × 蓝 / 黄 / 橙 / 红（色阶阈值矩阵，28 条） |
| 文家坡 | 危险状态 a/b/c × 掘进工作面 / 回采工作面（单元格是公式，如 5 × 10^4）；另一页是 等级 / 预警指标 / 采取措施 |

处置：新增 compliance_service/variants.py —— 版式变体注册表（表头签名 + 该变体的读法），read_table 先匹配变体再回落到通用读法。
- 文家坡两页在注册变体后：threshold 条目 0 → 每页 8 条（共 16），全部标记为条件证据（formula=True），**不产生数值**；
- 变体未命中时不猜测：match_variant 要求表头相似度 ≥0.6。

下一步：把"人工确认新变体"接到复核回流（确认即注册 variant，附矿码与表头签名），之后同类版式自动命中。

## 18. 版式发现与变体闭环（2026-10-27）

精度修正（由真实样本暴露）：
1. **仅有身份列的行不再成事实**：workface/time 属于所有组共用列，若一行没有任何"本组专有字段"，不得生成该组事实（analysis 22 → 0）；
2. **表头判定收紧**：表头行不得含数字且至少 2 个非空单元格，避免把数据行（"10 / 综掘 / 42盘区 …"）当表头；
3. **疑似表头错误单独标记**：签名含数字的"候选版式"在发现列表里标 suspicious_header，不混入待确认版式。

20 页真实样本（VL 通道）精化后基线：

| 组 | 事实条目 |
| --- | --- |
| prevention_daily | 34 |
| dispatch_daily | 16 |
| threshold | 16（文家坡两页，公式型条件证据） |
| design | 1 |
| analysis | 0（只有身份列，被正确抑制） |

未覆盖版式（去重后 7 个）：

| 类型 | 签名 | 说明 |
| --- | --- | --- |
| 待确认 | 危险状态 / 掘进工作面 / 回采工作面 | 对 threshold 已覆盖（wenjiapo 变体），其他组尚未声明读法 |
| 待确认 | 序号 / 采掘类别 / 盘区名称 / 工作面名称 / 班次 / 班长 | 调度台账类，建议 field_columns |
| 待确认 | 综合预警等级 / 微震 / a / 地音 / a / 应力 | 新的色阶/等级矩阵，需业务确认语义 |
| 疑似表头错误 | 10 / 综掘 / 42盘区 / 4208运顺 / 夜班 / 张少锋 等 4 个 | 表头识别失败，需改版面/提高分辨率或走视觉兜底 |

变体闭环（P2-l）：
- review.layout_review(table, group, reader, mine_code, reviewer) → 生成审计记录 + 变体；
- VariantStore(path) 持久化（JSON + 原子写 + revision），加载时与内置 SHIPPED 合并；
- read_table(table, group, variants=store.all()) 优先命中已确认变体；未命中不猜测。

## 19. 视觉预算 + 组无关变体（2026-10-27）

新增 compliance_service/vision_budget.py 与 repair.py：
- 每页 ≤2 次、整批 ≤8 次视觉修复调用；超限记 VISION_BUDGET_SKIPPED；
- 单次 60s 超时（内部把 vision.timeout 临时压低），超时记 VISION_FAILED；
- 裁块失败不消耗预算；
- 修的是"表头可疑"的表，未通过门禁一律不采用。

变体匹配支持 group="*"（同一版式跨资料组复用），新增三个真实版式：
| 变体 | 签名 | 读法 |
| --- | --- | --- |
| dispatch_ledger_columns | 序号 / 采掘类别 / 盘区名称 / 工作面名称 / 班次 / 班长 | field_columns |
| danger_state_formula | 危险状态 / 掘进工作面 / 回采工作面 | formula_table |
| warning_level_matrix | 综合预警等级 / 微震 / a / 地音 / a / 应力 | grade_matrix（新增 a/b/c 等级 schema） |

20 页样本（同一批图）变体加入前后对比：

| 组 | 加入前 | 加入后 |
| --- | --- | --- |
| prevention_daily | 34 | 38 |
| dispatch_daily | 16 | 20 |
| analysis | 0 | 12 |
| threshold | 16 | 20 |
| design | 1 | 13 |

注意：analysis 由 0 升到 12 需抽查确认是否为真实字段（防止共用身份列回潮），已列入抽检项。

## 20. 精度优先的最终修正与基线（2026-10-27）

审计发现并修复的三处"静默过绑"：
1. 列名用子串匹配 → "危险状态"被当作 status、"监测项目"等误命中；改为**精确别名**匹配；
2. 公式/等级读法变体使用 group="*" → 条件证据被灌进 analysis（0 → 12 的假增长）；改为仅 threshold；
3. 补充真实列名别名（工作面名称、工作地点）。

修正前后 20 页同批对比：

| 组 | 修正前 | 修正后 | 说明 |
| --- | --- | --- | --- |
| prevention_daily | 38 | 18 | 去掉误绑（危险状态→status 等） |
| dispatch_daily | 20 | 4 | 同上 |
| analysis | 12 | 0 | 条件证据不再越组 |
| threshold | 20 | 20 | 变体路径不受影响 |
| design | 13 | 1 | 同上 |

结论：这是一次**用召回换精度**的修正——按"宁可漏、不可错"的约定，方向正确；后续靠"变体 + 业务确认"把召回补回来，而不是靠放宽匹配。

## 21. 阶段验收数据（2026-10-27，20 页真实样本，VL 通道）

| 指标 | 数值 |
| --- | --- |
| 页数 / 有表格页 | 20 / 8 |
| 事实条目 | prevention_daily 18、dispatch_daily 30、threshold 20、design 1、analysis 0 |
| 等级阈值事实 | 20 条（行向等级矩阵） |
| 其中超阈值 | 8 条（c/d 档） |
| 其中需立即撤人卸压 | 8 条（与业务口径一致："c 为基准，c/d 均需立即撤人卸压"） |
| 全程耗时 | 99.6 s（VL 通道，未用视觉兜底） |
| 页状态 | partially_extracted 19、extracted 1（拒绝伪造完整） |

相对上一版的变化与原因：
- dispatch_daily 4 → 30：新增 attendance_count / coal_cutting_count / planned_cutting_count / cumulative_cutting_count 四个字段（按业务口径"出勤人数、割煤刀数进判定"）；
- threshold 保持 20，并首次产出**等级 + 超阈值 + 立即动作**三要素（业务口径来自用户确认）；
- analysis 维持 0：精确匹配下没有本组专有列，不再虚高。

仍未覆盖：
1. 4 个疑似表头错误的页面（需 220 DPI 重扫或人工复核）；
2. 其余监测技术的等级基准（微震/地音/应力各自不同，待业务给表）。

## 22. 等级口径的第三条路：从文档自身读取（2026-10-27）

前提修正（用户澄清）：样本只覆盖部分文件，真实世界无法完全预知；各矿各监测技术的等级基准互不相同，业务方也无法预先穷举。

因此不再要求业务方提供"等级基准表"，改为三级取值与来源标注：

| 优先级 | 来源 | 说明 |
| --- | --- | --- |
| 1 | document | 从本文档自身文字/表格里解析出的口径，如 "c 及以上需立即撤人卸压"、"以 b 为基准"、"c/d 档需撤人卸压"；附证据片段 |
| 2 | variant | 变体默认值（如 c 基准） |
| 3 | unset | 两者都没有 → 等级事实标 baseline_source=unset，不参与判定 |

实现：compliance_service/threshold_rule.py（正则解析 + apply_rule 优先级合并），每条等级事实都带 baseline_source，便于复核追责。

真实页验证（文家坡两页）：
- 两页文字里都没有显式口径 → status=unresolved → 回落 variant 默认 c；
- 事实计数：8 条/页，over_threshold 4，immediate 4，baseline_source=variant；
- 也就是说：**系统不臆测矿方标准，而是明示"这是默认口径"**；一旦文档里写了口径，就自动以文档为准并附证据。
