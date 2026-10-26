# 生产适配器配置与联调说明

2026-10-25。当前新增上游任务、OceanBase 只读查询、附件取证和 HTTP 回调适配器，并复用现有 SQLite 账本、fence 和 outbox。代码可在本机注入环境下验证；真实 OceanBase、正式上游和正式回调尚未联调，本文不代表生产验收通过。

## 配置与启动

从 `config/production.example.toml` 复制一份部署专用配置，替换正式来源、环境、`check_url`、授权矿井及附件/回调目标白名单。示例域名只说明格式，不能直接用于部署。API 与 worker 使用同一配置文件和账本。

```powershell
# 由部署环境提供 COMPLIANCE_TASK_TOKEN 与 COMPLIANCE_OB_* 环境变量
# 不在脚本、命令历史、配置、日志或快照中填入真实凭据
python -m compliance_service --db .local/production.sqlite3 init
python -m compliance_service --production-config config/production.local.toml --db .local/production.sqlite3 serve
# 另一个终端
python -m compliance_service --production-config config/production.local.toml --db .local/production.sqlite3 worker
```

`--production-config`、`--db` 放在子命令之前。配置加载不连接数据库；worker 执行任务时才通过选定驱动建立连接。`init` 负责创建账本或执行内置 additive schema 2 迁移，重复执行安全；迁移代码在 `ledger.py`，无需另一个迁移脚本。已有部署在迁移前备份账本及关联输入/证据目录。

配置只保存环境变量名。数据库使用 `COMPLIANCE_OB_HOST/PORT/DATABASE/USER/PASSWORD`；任务认证使用 `COMPLIANCE_TASK_TOKEN`，至少 16 个非空白字符。现阶段恰好配置一个 `[[principals]]`，令牌对应其 `allowed_mines`，不是面向多租户的完整 IAM 系统。令牌通过 `X-Inspection-Token` 请求头提交。

`oceanbase.compatibility_mode` 必须显式为 `mysql`。`driver_module` 指向部署中已安装、经验证的 MySQL 兼容 DB-API 模块；模板中的 `pymysql` 不是安装动作或驱动兼容性承诺。本次没有新增依赖。部署需验证该驱动支持 `%s` 占位符和 `connect(host, port, database, user, password, connect_timeout)` 关键字，并使用只读账号。

## 任务入口与查询

默认入口为 `POST /inspection/tasks`；实际使用 `service.check_url`，与上游基础表保持一致，不要求上游换成另一个路径。提交报文示例：

```json
{
  "mineId": "610401015959",
  "status": "running",
  "checkCycle": "2026-10",
  "items": [
    {"itemCode": "stop_work_unload", "itemName": "冲击危险停工撤人与卸压"},
    {"itemCode": "relief_effects", "itemName": "冲击地压解危效果检验"}
  ],
  "resultCallbackUrl": "https://callback.example.com/result"
}
```

成功返回 HTTP 200 和下列 JSON，表示控制请求持久化生效；检查由 worker 异步执行：

```json
{"code":200,"success":true,"msg":"任务控制请求已生效"}
```

响应头 `X-Inspection-Task` 是本服务任务键。带同一令牌调用 `GET {check_url}/{task_key}?mine_id=...` 可查看任务及每项 run/delivery 状态。运行与进度接口继续为 `GET /v1/runs/{run_id}` 和 `GET /v1/runs/{run_id}/progress`，需带 `source_system`、`mine_code`、`environment`，且通过令牌和配置归属验证。未授权矿井、错来源/环境或不可读运行返回 404。

配置适配器后禁用 debug、manual、旧 upstream/runs 和文档调试入口，防止绕过上游取证和身份链。`/health` 只说明进程存活及配置情况，`real_connectivity=false` 不会因为配置存在而变成真实连通性证明。

## 幂等与暂停恢复

只默认接受 `running`。正式暂停/恢复字符串必须经双方确认后写入 `[status_map]`，例如把已约定的 wire 字符串映射为 `pause`/`resume`，不能把示例注释当成已支持协议。

`executionId` 存在时与 `taskId` 组成执行身份；只有 `taskId` 时用该任务身份；二者都没有时按矿井、周期、排序后的检查项计算指纹。重复控制定位同一任务，包括终态任务，不另建结果。需要重新检查时使用新的明确 `executionId`；不能靠改变回调 URL 或再次发送同一 running 请求创建新一轮。回调 URL、矿井、周期和检查项在任务创建后不能静默替换。

暂停在文件、页面或 provider 检查点协作生效；已开始的单次模型请求允许收束。恢复继续原任务、查询/附件范围和有效 fence，不把已成功解析或 provider 候选整体重算。暂停和恢复也必须携带能定位原执行的同一身份、范围和回调 URL。

## OceanBase 查询和输入快照

`oceanbase.py` 只执行参数化 SELECT，资料 ID 来自服务端两项白名单。当前适配器假定目标表或只读视图提供：

- `mine_code`、`file_name_id`、`attachment_id`；
- 可比较的 `report_date` 和 `report_time_year`；
- 附件名称、原始 URL，优先列名为 `attachment_name`、`attachment_url`。

真实表列名、类型、时区、更新/替换/作废字段和查询权限须在联调中核对。不要对未经核验的表直接宣称支持。若源端不是这些列，应由确认后的适配器/只读视图映射，不能把错误字段猜测当成正式协议。

日报/原始记录按检查月查询；年度设计、阈值和评价资料保留用于匹配有效期，不用月度过滤误删。保留年度记录不表示它必然适用于本轮，上传频率也不证明有效期。`2026/01/00` 等周期标签不能伪造为事件日期；跨月事件和周验收仍须以正文事件/施工日期核对。

每项保存查询 SQL/参数、版本、查询时间、原始行和规范化行。`attachment_id` 去重；同名不同 ID 保留；同 ID 元数据冲突拒绝。查询快照先进入账本，恢复复用该快照，避免重取清单悄悄改变输入范围。

附件按 ID 获取并保存收据，验证文件魔数、SHA256、原始名称和来源 URL；同字节重复不制造新事件，不同哈希的同日报版本保留冲突。成功文件收据可复用，失败附件按各自 ID 重试，不能把成功子集按数组顺序重新绑定原清单。附件下载失败、无返回行、解析失败、OCR 不可读和预算跳过分别形成缺口。

查询目录只能证明被登记的资料存在。当前 OB 路径明确保持 `source_complete=false`；必要覆盖未独立证明时不输出 `pass`，通常以 `pending`/`unverifiable` 收束，有确定反证仍依规则产生 `no_pass`。

## 网络目标和回调

若正式回调要求认证，可在 `[callback]` 配置 `header_envs = { Authorization = "COMPLIANCE_CALLBACK_AUTHORIZATION" }`。环境变量值是完整请求头值（包含认证方案）；密钥不会写入任务、payload 或交付记录。关键传输头和幂等头禁止覆盖。

附件与回调分别配置 `allowed_hosts` 或 `allowed_origins`，默认 HTTPS，不接受 URL 内凭据、片段或重定向；回调发送前校验解析地址，拒绝元数据/链路本地地址。确有内网附件目标时仅对已核验白名单显式开启 `allow_private`。`allow_http`、`allow_loopback` 仅用于受控本机测试，不能作为生产模板默认值。部署网络出口还应落实固定目标/代理策略；当前主机白名单不等于完整网络边界。

任务保存的 `resultCallbackUrl` 只是结果目标，不是本服务接收任务的 `check_url`。每项每次执行只写一条 outbox DTO；多日期问题合并在同条 `detail`。DTO 包含五类状态、带时区定版时间和非空摘要/详情；只使用原始附件 URL，附件自身没有具体问题时省略 `attachmentContent`。

投递复用现有持久 outbox。HTTP 2xx 且响应 JSON 的 `code=200`、`success=true` 才算 delivered；HTTP 200 但业务失败不算成功。超时/连接异常/5xx 使用同一 payload、定版时间、URL 和 `Idempotency-Key` 退避重试，默认最多 3 次，达到预算或出现鉴权/报文/业务失败进入可见 `failed`。回调失败不重新执行检查，不改写业务结果。远端是否采用该幂等头仍需联调，不能承诺 exactly-once。

## 识别失败恢复与历史结果

稳定原生解析和成功 provider 候选分别缓存；临时识别失败允许只补失败 provider，模型、提示、token 预算和质量门变化会改变缓存身份。转录一致仍须经过事件和规则前件，候选不能越级为业务问题。运行终态之后结果与快照不覆盖；补证或重试后需要更新业务结论时，创建新执行并保留历史。

## 本地证据和正式放行

`test_upstream_integration.py` 覆盖任务到查询/附件/结果/outbox 的本地闭环；`test_task_ledger.py` 覆盖任务、幂等、暂停恢复和 fence；`test_oceanbase_acquisition.py` 覆盖注入数据库/下载；`test_callbacks.py` 含真实本机 HTTP 成功与重定向拒绝；`test_evidence_boundaries.py`、`test_recognition_recovery.py` 覆盖判定边界及识别恢复。测试不连接真实 OceanBase、正式上游或正式回调。

正式放行仍需：确认 itemCode/check_url/status/执行标识和身份认证；核对只读数据库视图、驱动及资料适用政策；保存五类结果正式实收回执；建立真实专家双标 gold、仲裁和独立 M6 盲测。准确率阈值与分母由业务方和独立专家确定，不能以单元测试数量、completed 或模型置信度替代。
