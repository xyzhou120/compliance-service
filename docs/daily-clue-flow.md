# 防冲日报线索与多周期调试流程

当前 `stop_work_unload` 调试链按以下顺序处理：

1. 只从 `actual_record + prevention_daily` 资料提取预警/动力线索。每条线索保留日期、工作面、事件类型、事件编号（如有）、原文证据 ID 和文件定位。
2. 从调度日报、生产矿长/总工程师日分析和其他实际记录提取撤人、卸压安排。要求记录同时具备日期和工作面；计划、应当、拟执行不会当作已安排。
3. 对每个线索分别输出撤人与卸压状态：`arranged`、`explicit_not_arranged`、`planned_only`、`evidence_not_found` 或 `unknown`。
4. 明确写出“明确未安排”和“本次资料未找到安排证据”。资料不全、线索未核对或版本冲突时，问题保持 candidate/unverifiable。
5. 没有显式事件编号或连续标识时，临近日期不自动合并。只有同工作面、同类型、显式事件/连续标识和连续日期同时满足时，才形成一个 episode。线索标记已解除时切断后续归并。
6. 同一报告日期、同一资料类别存在不同附件版本时保留所有版本。内容哈希相同的重复附件不升级为版本冲突；内容不同的版本会让问题降为 `unverifiable`。

当前调试结果会同时返回 `daily_clues`、`daily_arrangements`、`daily_issues`、`daily_episodes`、`daily_summary` 和 `daily_candidate_evidence`。未核对的 OCR/VLM 候选只用于说明可能线索，不直接形成 `no_pass`。

本轮真实多轮样本脚本为 `scripts/prepare_1024_rounds.py`，生成的选样和参数位于 `.local/validation/1024-multi-round/`。下载失败的轮次不会提交任务，避免把缺失附件解释成业务未安排。
