# 当前系统检查流程图

核查日期：2026-10-26。依据当前工作区源码、默认检查目录和 impact-1023-draft-v2 规则底稿；说明当前实现行为，不把底稿文字直接当作执行结果。

## 查看图

- [三图查看页（支持缩放）](../../outputs/current-inspection-flow/index.html)
- [图一：系统整体检查流程 PNG](../../outputs/current-inspection-flow/01-system-flow.png) / [SVG](../../outputs/current-inspection-flow/01-system-flow.svg)
- [图二：撤人与卸压 PNG](../../outputs/current-inspection-flow/02-evacuation-relief.png) / [SVG](../../outputs/current-inspection-flow/02-evacuation-relief.svg)
- [图三：效果检验 PNG](../../outputs/current-inspection-flow/03-effect-inspection.png) / [SVG](../../outputs/current-inspection-flow/03-effect-inspection.svg)

系统图按 12 个阶段呈现入口、受理、执行、资料、解析、事实与关联、规则、门禁、保存及交付。两张规则图分别按主规则与独立 scope 补充核验分泳道，框内列出条件、局部命题和缺口处理，再统一汇总整项状态。两泳道是逻辑分工，当前代码先后调用，并非并发执行。

## 与源码对照

| 图中内容 | 代码位置 |
| --- | --- |
| HTTP 入口、授权与结果查询 | compliance_service/api.py 的 create_app |
| 受理、主执行、合并、回调 | compliance_service/service.py 的 receive_task、run_once、_execute、_evaluate、deliver_once |
| 幂等、租约、账本与 outbox | compliance_service/ledger.py 的 accept_task、claim、save_snapshot、finish、claim_delivery |
| OceanBase 与附件清单 | compliance_service/oceanbase.py 的 query；acquisition.py |
| 原件、解析、快照 | compliance_service/sources.py 的 LocalDocumentSource；parsers.py；ocr_documents.py |
| 事实与事件归属 | compliance_service/fact_extraction.py 的 extract_facts；association.py 的 associate_facts |
| 日报线索旁路 | compliance_service/daily_clues.py 的 extract_daily_clues；daily_episodes.py 的 aggregate_daily_findings |
| 撤人与卸压主规则 | compliance_service/rules.py 的 _Check.check57 |
| 效果检验主规则 | compliance_service/rules.py 的 _Check.check78 |
| 评价、设计、参数、人员、签字 | compliance_service/scope_checks.py 的 _Scope.check_event |
| 主规则初值 | compliance_service/rules.py 的 _Check.finish |
| 草案覆盖与候选门禁 | compliance_service/rulebook.py 的 apply_draft_scope |
| 内容冲突门禁 | compliance_service/rules.py 的 gate_conflicted_evidence |
| 默认材料和规则底稿 | compliance_service/data/inspection_defaults.json；impact_1023_draft_v2.json |

## 阅读与实现边界

- 每个检查项是独立 run 和 snapshot；缺证、明确未执行、内容不可读和技术错误分别处理。completed 仅表示执行收束。
- 动力事件可以直接成为停工撤人触发。普通监测要求大于批准阈值；钻屑法钻粉率指数允许等于触发。效果检验要求严格小于阈值。
- 普通 order 的逆序/相等/精度不足通常是 missing。草案停工/断电不晚于撤人/卸压、实际复工早于确认/调度解除等有专门 false 分支。图内逐一说明，不能统一等同。
- scope 使用精确时间 ≤ 比较；精确同时刻允许，同日只有日期仍不能证明先后。签字问题在尚无可比解除/复工时降为缺口。
- 远程目录查询与附件下载不证明观察范围完整；当前 remote source_complete=False 会阻止 pass。日报旁路结果只附加到主结果，不直接改变主 status。
- 结果须经过主规则初值、scope 合并、草案/候选门禁和内容冲突隔离。候选归属或冲突支持的问题不能直接作为确定违规；当前 finding 的重大隐患分类保留 not_assessed。
- 当前评价核查为通用 risk_assessment + design，未完整区分三级评价。签名为原文存在/身份/时间证据，不包含数字证书密码学验真。

## 交付与验证

新增三张 PNG、三张 SVG、支持缩放的查看页及生成脚本。source-evidence.json 保存核查源码 SHA-256 和节点位置；render-check.json 保存图像尺寸及文字边界检查。未修改业务代码、配置或运行数据库。

相关现有测试已执行：`python -m unittest tests.test_rules tests.test_scope_checks tests.test_full_checks tests.test_rulebook tests.test_fact_chains -q`，共 71 项通过。该结果支持本次实现核查，不代表真实 OceanBase、正式回调或业务准确率已经通过生产验收。

另执行全量 `python -m unittest discover -s tests -q`：380 项中 379 项通过、1 项失败。失败为 `test_frontend_static.FrontendStaticTests.test_independent_frontend_has_lazy_hash_routes_and_build_contract`，前端源码未包含测试断言所需的 `callbackMode = none` 字样。本次未改业务代码，不为绘图任务调整该断言或前端。
