# 本地文件检查与准确性验证

本轮不启动 API、常驻工作进程或外部服务。已实现独立可调用的文件解析、事实提取、两项确定性规则及离线评测；以下命令供后续需要时使用，不会因为安装或导入项目自动执行。

## 已支持的输入

PDF 原生文本按页定位，空白页保留，扫描/图片内容标识 OCR 缺口。Word 保留正文顺序、段落及表格行位置；未处理修订、图片、页脚等显式登记缺口。Excel 保留工作表、行/单元格、隐藏状态、合并关系和公式缓存，0 不变空；无缓存公式不当成计算结果。

OCR/VLM已接入可填写的config/models.local.toml（本地忽略文件）。扫描页先取得独立OCR/视觉转录，无法交叉核对的内容保持不可读；模型不直接判pass/no_pass。DOC/XLS旧格式、加密及损坏输入保留失败记录，不能静默跳过。

当前新本地文件任务默认使用用户1023暂行规则impact-1023-draft-v1。已实现条件的结果在implemented_condition_status中返回；整项尚有未覆盖要求时status保持pending并列出rule_coverage。候选事件关联不能独立支持确定违规。详见rules-1023-development.md，不能把局部条件通过解释为整份规则合规。

## 文件清单

在资料目录保存 manifest.json，例如：

```json
{
  "source_system": "local-documents",
  "mine_code": "mine-a",
  "environment": "local",
  "period": {"start": "2026-10-01", "end": "2026-10-31"},
  "source_complete": false,
  "files": [
    {"file_id": "monitoring", "path": "监测记录.xlsx", "role": "actual_record"},
    {"file_id": "dispatch", "path": "调度记录.docx", "role": "actual_record"},
    {"file_id": "measures", "path": "防冲措施.pdf", "role": "requirement"}
  ]
}
```

source_complete 仅表示该清单所声明资料范围已完整取得，不代表全矿全周期的资料一定齐备。解析或取证仍有缺口时，即使此值为 true，也不能通过。默认只在 manifest 所在目录读取相对路径；可显式设置 root，文件路径不得逃出该目录。role 可选 actual_record（实际原始记录）、requirement（批准要求/措施）、statement（报告自述）、regulation（规范原文）；后两者不能证明现场行为已执行。

原始字节会复制到本地内容寻址存储并校验 SHA256。解析缓存按矿井/来源/环境、字节版本和解析版本隔离；每轮重新绑定快照与证据 ID，旧结果不变。缓存复用只减少解析，全部相关事实仍重新判断。

需要运行时可执行单次命令；它不启动监听服务：

```powershell
python -m compliance_service check-files --manifest "C:\资料\manifest.json" --check-cycle "2026-10" --report .local/result.json
```

通过 HTTP 受理时也可使用 `scope: {"manifest": "C:\\资料\\manifest.json"}`，两入口共用相同实现，真实回调仍未启用。

## 事实提取与保守边界

结构化表格和显式“字段:值”记录支持同一行内的事件编号、工作面、时间、动作、状态、指标与单位。表头有别名支持，不要求每矿开发专用模板。无法绑定的任意自然段保留线索候选和原文，不把猜测字段直接当事实。

动作包括停止作业、断电、撤人、报告、卸压、检验、技术确认、调度放行和实际复工。时间、执行者、目标范围、立即处置、撤离路线符合性须有实际记录；“应当”“计划”“未找到记录”都不能当已执行或未执行。批准阈值和必要测点全集必须明确，不能从已出现的数据推测完整集合。

明确事件编号仍会保留；无共同编号时仅在同工作面、同日唯一精确时间段下建立候选关系，并公开依据。独立阈值与测点要求具有明确工作面和起止有效期时可派生到适用事件；冲突均保留。复杂地点别名、跨日关联、PDF无结构表格、自由文本和扫描件仍需提高覆盖，不能宣称任意版式已完整理解。

## 两项规则

当前细则版本自2026-09-23生效，按事件时间选用；缺历史细则时早期事件保留历史规则缺口。第57条普通监测触发为 >，第59条钻屑法钻粉率指数达到临界指标为 >=；第78条解危检验解除为严格 <。立即处置不设臆造的分钟期限；粗时间不能证明严格先后。

两项检查核对实际停止/断电/撤人/报告、危险研判与针对性解危，以及解危后必要指标全集、已批准有效阈值、技术负责人确认、调度解除、实际复工时序。技术负责人确认是从文件识别的法定业务事实，服务没有人工改判功能。

有正面不符合证据才输出 no_pass，并同时保留其他缺口；仅缺记录为 pending，不可读为 unverifiable。等于阈值后复工可按细则输出不符合，但不自动认定重大隐患标准中“超过”的分支。重大隐患最终定性不在本轮自动推断范围。

## 简单准确性评测

评测样例列表的每项显式填写 case_id、manifest、itemCode、checkCycle、expected_status 和 origin（synthetic_document 或 real_mine_gold）。预期结果不能由引擎自己的输出生成。

```json
[
  {"case_id": "效果等值后复工", "manifest": "案例1/manifest.json", "itemCode": "relief_effects", "checkCycle": "2026-10", "expected_status": "no_pass", "origin": "synthetic_document"}
]
```

```powershell
python -m compliance_service evaluate-files --cases "C:\资料\cases.json" --output .local/accuracy
```

报告保存标签准确率、业务判断分母与准确率、可判定率、错误通过数、漏判数、混淆矩阵和证据引用完整性。pending 不冒充正确业务判定，合成文档和真实煤矿 gold 分开记录。该工具不引入人工改判或新增审批流程，复杂分层盲测仍留 M6。
