# 两项本地可用版本验证

2026-10-23，代码0.4.0。两项均可通过本地API或单次命令运行；此次未重启用户服务，也未改写用户的运行数据库。

第一项已增加评价/设计适用范围、解危方法/参数/范围/验收、撤人和解危限员/设备、研判签字告知；第二项已增加设计适用范围、技术负责人签署/身份及签署与解除复工时序。基础触发、阈值、必要指标和动作时序继续执行。签名核验指原件事实，不声称密码学鉴真。

## 验证结果

- 目标PyCharm虚拟环境全套207项测试通过，compileall通过。
- 7份原生文件组成完整跨文件案例，两项均pass。缺签名、效果不满足和施工参数不满足等8项固定预期全部匹配，错误通过0；均标记synthetic_document，不能代替实矿准确率。
- HTTP测试覆盖两个item受理、执行、结果查询和mock delivered。
- 离线0.4.0 wheel构建及空目录两项文件检查通过；full_rule_check_complete=true，证据链可追溯。
- wheel不含models.local.toml和本地密钥；未增加第三方依赖。

最终机器摘要和日志在.local/validation/full-checks，案例在.local/validation/full-check-samples，8项评测在.local/validation/full-check-evaluation。

## 模型接口实测

OCR实际协议为根地址/ocr的multipart file，rec_texts/rec_scores响应已适配。视觉模型注册及图片JSON转录已验证；对无煤矿业务内容的中文图片两候选一致。设置response_format=json_object后，一页测试扫描PDF也完成独立转录核对并进入可读证据块。

候选中小数点、正负号、单位大小写或文字不一致不能经归一化误放行。仅有单路识别、空文本、坏JSON和冲突时仍不可读。转录一致不替代语义验证、规则核验和实矿准确率。私有配置不得整体输出到日志、快照或结果。

## 独立复核

已修复并复验：未批准设计不参与参数/签字人反证；尚未解除或复工时签名缺陷只形成缺口。完整样本仍两项pass，两个反例均pending。数值0.8/08混淆已有回归。

## 使用边界

本地两项流程已可操作，不等于任意版式、自由文本、别名或跨日事件均可无歧义理解。真实冲击地压完整gold、OceanBase和正式回调仍需各自验收。专用lint/typecheck工具未安装，不将编译当类型检查。

本地操作见full-check-local-use.md。在PyCharm重启API和Worker后使用新代码和本地模型配置。
