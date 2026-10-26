# 两项完整检查本轮实现契约

仅扩充两项检查所需事实和断言，不新增人工改判/审批流程。数据仍来自登记原件、具定位的证据块，不能靠请求中传入pass/no_pass。

已有kind event/action/measurement/threshold/requirements不变。新增requirement来源的risk_assessment和design：

- risk_assessment.fields：approved(bool)、valid_from/to、impact_risk(bool)、risk_grade(str)、target_scope(str)。
- design.fields：approved、valid_from/to、target_scope、technical_head(str签署人姓名)、relief_methods(list[str])、relief_parameters(list[dict{name,operator,value,unit}])、relief_area(str)、withdrawal_area(str)、max_personnel(str整数)、unrelated_equipment_stop_required(bool)。scope继承事件workface，必须明确有效期和原文，不借其他矿/面。

action.fields新增字段，全部从当前原文明确提取，不默认：

- relief：method(str)、parameters(list[dict{name,value,unit}])、relief_area、acceptance_passed(bool)、acceptance_signer(str)、acceptance_time(ISO)、personnel_count(str整数)、unrelated_equipment_stopped(bool)。status=done仍表示真实施工完成，不能拿设计要求冒充。
- evacuate：withdrawal_area、expected_personnel(str整数)、withdrawn_personnel(str整数)。仍需已有route_conforms=True。
- hazard_analysis：signer、signature_present(bool)、notified(bool)、notification_time(ISO)。用于第一项研判签字/告知。
- technical_confirm：signer、signature_present(bool)、signed_at(ISO)。signer与同范围有效design.technical_head核对；签名存在是文档事实，不声称数字证书密码学验证。
- 所有事实fields可含event_time_reference(ISO)：原文明确指向触发时刻时，关联模块可据同工作面+该精确时刻找到唯一事件，标记verified；只有同日先后的关联仍candidate，不支持整项确定结论。

中文列名：记录类型(危险性评价/防冲设计)、冲击危险、危险等级、适用范围、技术负责人、解危方法全集、解危参数要求、解危区域、撤离区域、解危限员、无关设备停运要求、施工参数、验收通过、验收人、验收时间、解危人数、无关设备已停、应撤人数、实撤人数、签字人、签名存在、签署时间、已告知、告知时间、关联事件时间。列表/参数要求是原始表格中的显式JSON或严格可解释值，未知/缺失不能猜。

完整规则验证接口：scope_checks.evaluate_scope(run,snapshot)->{propositions,findings,gaps,implemented_ids}，每项proposition保留event_id/workface/code/state/evidence_refs/detail/rule_refs。已完整实现但输入缺字段时missing+具体缺口；矛盾正证按false并保留引用。implemented_ids是已实现检查能力，不是‘当前样本检查通过’。

能力ID与rulebook原有_UNCOVERED对应：risk_and_design_scope（两项）、relief_parameters_acceptance、personnel_and_equipment、signatures_and_notification（第一项）、signature_evidence（第二项）。禁止永久将所有已实现能力标‘未开发’，也不能移除未实现门槛后假通过。

第一项动态核对以上四项，第二项核对risk_and_design_scope及signature_evidence。基础条件仍由rules.py校验。scope_checks只能使用经assert_snapshot_owner验证的facts，要求actual_record/requirement来源角色正确，事件关联明确；只有candidate时后续统一降级。

无原文批次编号时可用明确关联事件时间建立可复核链接；不得把单纯‘最近事件’当已证明归属。每项结果包含逐事件证据链、所有缺口、规则来源版本。原生文件完整证据时可pass，确定问题可no_pass，缺证pending、不可读unverifiable、技术错误failed。
