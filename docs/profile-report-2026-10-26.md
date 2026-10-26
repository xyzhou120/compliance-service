# 多轮检查耗时与不合规场景报告

本轮使用本地 `stop_work_unload` 检查流程，结果回调固定为 `none`。合成资料用于验证规则和耗时；胡家河矿井资料用于真实文件窗口验证。合成结果不能代表真实矿井准确率。

## 合成轮次

运行脚本：

```powershell
python scripts/profile_inspection_rounds.py --output .local/validation/performance-20261026-expanded
```

本轮关闭 OCR/VLM，测量原生解析、抽取、规则评估和缓存复用，避免模型网络波动掩盖流程耗时。

| 轮次 | 检查项 | 结果 | 总耗时 | 主要发现 |
| --- | --- | ---: | ---: | --- |
| complete-cold | stop_work_unload | pass | 1765 ms | 7 个文件首次解析，7 个解析缓存写入 |
| complete-warm-1 | stop_work_unload | pass | 869 ms | 7 个解析结果复用 |
| complete-warm-2 | stop_work_unload | pass | 780 ms | 7 个解析结果复用 |
| bad-relief | stop_work_unload | no_pass | 2064 ms | `relief_parameters_acceptance`：卸压参数/验收不满足 |
| missing-signature | stop_work_unload | pass | 1428 ms | 当前实现没有把该样本的签字缺失升级为已实现规则问题 |
| stop-not-immediate | stop_work_unload | no_pass | 945 ms | `stop_immediate`：停工未被明确证明立即执行 |
| power-off-not-immediate | stop_work_unload | no_pass | 971 ms | `power_off_immediate`：断电未被明确证明立即执行 |
| evacuation-route-wrong | stop_work_unload | no_pass | 1707 ms | `evacuate`、`personnel_and_equipment`：撤人路线或人员覆盖不满足 |
| bad-effect | relief_effects | no_pass | 1113 ms | `below_threshold:*`：效果值未满足严格低于阈值 |
| missing-dispatch | relief_effects | pending | 705 ms | 缺少调度解除/相关资料，不能证明复工闭合 |

耗时结论：首次原生解析约 1.8 秒，缓存复用约 0.8 秒；规则评估约 25–68 毫秒。当前主要耗时在文件解析和缓存读写，未包括扫描件 OCR/VLM。

## 真实矿井资料窗口

同一份 13 文件 manifest 的无 OCR/VLM 流程基准：`20.16 秒`，其中来源解析约 `19.95 秒`，状态仍为 `unverifiable`，覆盖缺口 277 项。该轮只用于测量文件读取、原生解析和规则流程，不能替代带识别的真实检查。

矿井：`610401015959`（胡家河）

任务周期：`2026-10`

实际已冻结资料窗口：`2026-10-14` 至 `2026-10-16`

输入：13 个附件，全部通过本地路径存在性和 SHA256 收据核对。资料包含防冲日报、调度日报、日分析、10 月 15 日调度日报双版本，以及预警临界指标/防冲设计资料。

运行编号：`c271dbdd5a284d5eb41b5e83b63ddb40`

最终状态：`unverifiable`

原因：

- 输入 manifest 明确 `source_complete=false`，只覆盖 10 月 14–16 日，不代表 10 月全月资料。
- 13 个文件全部取得，但大量 PDF 图片页需要 OCR/VLM；本轮视觉 provider 出现 `URLError`、`TimeoutError`、`ConnectionResetError`，页面状态保留为不可读或候选。
- 发现 3 条日报候选线索，均为 `candidate`，不是确定性预警事实。
- 日级问题为 2026-10-15、402101 工作面；撤人和卸压均为 `evidence_not_found`，不能直接升级为明确未安排或 `no_pass`。
- 本轮没有确定性 `findings`，没有产生业务不合规结论。

真实窗口结果文件：`.local/validation/hujiahe-1024-20261014-16/result-cj001-current-month.json`

## 结论

已经稳定复现的确定性不合规场景包括：停工未及时执行、断电未及时执行、撤人路线/人员覆盖不合规、卸压参数或验收不合规、效果值未低于阈值。真实矿井窗口当前只能报告候选和资料缺口，不能把“本次资料未找到安排证据”解释为事实上的未安排。

本轮所有运行均未发送真实回调，交付状态为 `none`。模型打开时，扫描件和视觉服务耗时会显著高于本轮无模型基准；需要单独按 provider 成功、超时、重试和缓存命中统计。

需要优先修正的覆盖缺口：`missing-signature` 用例名称表示签字缺失，但当前结果为 `pass`。这不是本轮把它当成合规结论，而是说明停工撤人与卸压的签字/告知条件仍未接入最终判定，应补规则和回归测试后再使用该场景验收。

完整输出：`.local/validation/performance-20261026-expanded/profile.json`、`.local/validation/performance-20261026-expanded/real-native-profile.json`。
