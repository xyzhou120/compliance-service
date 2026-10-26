# 检查项与 OceanBase 清单配置

当前默认检查项 code 为 `stop_work_unload`，页面直接使用该 code 受理任务、保存历史并生成结果。上游确定正式 code 后，在本地调试页“检查项与清单配置”中修改检查项 code（例如 `CJ001`）并保存；新任务直接使用新 code。业务处理器保持“停工撤人与卸压”，不再增加一层上游 code 映射。已受理任务保存原 code、资料目录、配置版本和查询月份，配置更新不改变其结果、查询范围或暂停/恢复定位。

配置包含每个检查项的名称、启用状态、业务处理器，以及每类资料的清单名称、OceanBase `file_name_id`、资料类别、证据角色和上报周期。不同环境使用各自的配置文件；默认配置见 [inspection_defaults.json](../compliance_service/data/inspection_defaults.json)。调试服务首次保存时写入账本旁的 `inspection-config.json`，也可以通过 `COMPLIANCE_INSPECTION_CONFIG` 或 `service.inspection_config_path` 指定路径。页面保存需要当前配置版本号，若已被其他进程修改，先重新载入再保存。

目前允许 `annual`、`semiannual`、`monthly`、`daily`、`weekly`。查询年份使用任务 `checkCycle` 的年份，月份默认使用服务受理任务时的当前月（Asia/Shanghai），并冻结到本轮任务。页面可改为任务周期月或固定月份。对于 `checkCycle=2026-10` 且选中月份 `10`，查询条件为：

| 上报周期 | 查询条件 |
| --- | --- |
| 年报 | `report_time_year = 2026` |
| 半年报 | `report_time_year = 2026 AND report_time_month IN ('01','07')` |
| 月报、日报 | `report_time_year = 2026 AND report_time_month = '10'` |
| 周报 | 暂按选中月份查询，后续与真实数据对齐 |

SQL 由服务使用参数占位符生成，`file_name_id` 只来自已保存的服务端配置。清单登记日期和正文事件日期仍分开保存；上报周期不能代替设计或阈值有效期。若任务周期是 2026-06、当前月是 10，初步策略会查询 2026 年 10 月的月/日报，但结果的 `checkCycle` 仍为原任务的 2026-06，查询快照记录实际月份；遇到历史周期检查，应在页面改用“任务周期月份”。

配置管理仅限本地调试页，生产配置下不提供写入口。当前只允许将检查项关联到已实现的两项业务处理器；新增新的检查逻辑仍需开发。真实 OceanBase 表列名、租户兼容模式及资料周期标签须在联调时核对。
