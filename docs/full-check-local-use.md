# 两项完整检查本地使用

先重启PyCharm中的API和Worker以加载最新代码，工作目录和数据库路径沿用原配置。此次开发没有操作正在运行的服务或现有运行数据库。

## 已可验证的两项

stop_work_unload：冲击危险停工撤人与卸压。relief_effects：冲击地压解危效果检验。

已实现危险性评价/设计范围、阈值与必要指标、实际动作时序、卸压方法/参数/范围/验收、撤人和限员/设备、研判签字告知、技术确认签署与解除复工的证据核对。字段缺失保持pending，不是等待人工改判。

## 调试页面与业务资料清单

API启动后打开 `http://127.0.0.1:8080/debug`。选择检查项后，页面按业务资料分类提供多附件输入，例如防冲设计/危险性评价、防冲工作日报、调度日报、卸压验收、效果检验原始数据和技术确认/解除记录。每类可添加多份本地附件，也可逐行粘贴本地路径或 HTTP/HTTPS 地址；页面会自动生成本地 manifest，再调用同一套跨文件检查。资料不齐仍可提交，结果会保留 `pending` 和证据缺口。已存在 manifest 时可切换回绝对路径模式。地址由运行服务的电脑读取，上传和地址输入均受文件类型、大小、路径和归属校验约束。

## 可直接运行的自测资料

```powershell
python -m compliance_service make-samples --output .local/validation/full-check-samples
python -m compliance_service evaluate-files --cases .local/validation/full-check-samples/cases.json --output .local/validation/full-check-evaluation
```

这会生成7份资料构成的跨文件案例及8项固定预期测试，资料不是实际煤矿记录。complete目录两项均pass；missing-signature目录效果检验pending；bad-effect目录效果检验no_pass；bad-relief目录停工撤人与卸压no_pass。

在Swagger的POST /v1/manual/runs中粘贴（manifest改成本机实际绝对路径）：

```json
{
  "sourceSystem": "acceptance-samples",
  "mineCode": "SAMPLE-MINE",
  "environment": "test",
  "checkCycle": "2026-10",
  "ruleVersion": "impact-1023-draft-v1",
  "scope": {"manifest": "C:\\Users\\86158\\Documents\\ChatGPT\\检查规则梳理\\compliance-service\\.local\\validation\\full-check-samples\\complete\\manifest.json"},
  "callbackMode": "mock",
  "items": [{"itemCode": "stop_work_unload"}, {"itemCode": "relief_effects"}]
}
```

GET /v1/runs/{run_id}填写source_system=acceptance-samples、mine_code=SAMPLE-MINE、environment=test。预期execution_state=completed、result.status=pass、delivery_state=delivered。可以查看event_evidence_chains的文件版本、页/行定位、原文和每个命题。

真实资料按local-documents.md建立自己的manifest。规则只处理已经取得的资料，不建立催报或人工改判流程。

## OCR与视觉配置

实际配置：config/models.local.toml；无密钥的模板：config/models.example.toml。OCR配置根地址，客户端自动拼/ocr，以multipart的file字段上传；视觉使用OpenAI兼容chat/completions。

已用无业务资料的中文小图验证用户接口。OCR rec_texts/rec_scores格式已适配；视觉模型注册和图片转录已验证。密钥只供调用，不进入快照、证据、日志或安装包。配置可用COMPLIANCE_MODEL_CONFIG指定其他本地文件。

清晰扫描件仍需OCR与视觉转录一致并通过事实/规则校验。不同数字、空响应或无法解释的表格会保留不可读/缺口，不能靠模型自报置信度提高通过率。DOCX图片/复杂自由文本的完整语义提取仍需更多真实材料验证。

扫描PDF需要pdftoppm可执行程序，可在ocr段设置pdf_renderer绝对路径，或从PATH查找；原生PDF不需要。max_pages限制单份文档识别页数，超过预算的页仍保留为缺口。
