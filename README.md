# 冲击地压合规检查服务

本服务面向两项冲击地压跨文档合规检查，保留原文、文件字节哈希、页/行/单元格定位和跨文件证据链。当前代码已实现本地持久账本、任务幂等与暂停检查点、PDF/DOCX/XLSX 解析、候选质量门、两项确定性规则、OceanBase 只读适配器、附件冻结和 HTTP 回调 outbox。真实 OceanBase、正式上游、正式回调、生产身份授权和专家 M6 盲测仍需外部联调与验收。

规则结果只能是 `pass`、`no_pass`、`pending`、`unverifiable`、`failed`。`completed` 只代表执行流程收束；目录存在、模型置信度或 mock 回执都不能证明业务正确。候选、否定句和资料未找到证据不会越级形成 `no_pass`，历史结果不可覆盖，修订通过新 executionId 创建新运行。

本地离线检查仍见 [本地文件检查](docs/local-documents.md)。生产适配器、环境变量、任务入口、OceanBase 查询假设、附件与回调安全约束见 [生产适配器配置与联调说明](docs/production-integration.md)。开发待办与验收矩阵见 [开发待办](docs/development-tasks.md) 和 [验收矩阵](docs/acceptance-matrix.md)。
调试页面已经拆成独立的 Vue 3 + TypeScript + Vite 前端工程，源码在 [frontend](frontend/README.md)，由 FastAPI 同源托管构建产物。`start-local-debug.ps1` 会在源码更新或构建产物缺失时自动执行构建；旧版附件调试器保留在 `/debug/legacy`。

## 本地安装

Python 3.12+。运行时使用 FastAPI、Uvicorn、pypdf、python-docx、openpyxl；测试使用标准库 unittest 和项目已声明的测试依赖，不在代码中安装数据库/OCR/回调驱动。新机器可在独立虚拟环境安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install ".[test]"
```

## 本地运行

需要快速模拟上层任务时，运行 `.\scripts\start-local-debug.ps1` 并打开 `http://127.0.0.1:8091/debug`。页面填写任务参数后自动执行，显示结果、涉及文件及原文证据，固定不发回调。页面还可查看和编辑检查项 code、清单 ID 与上报周期；见 [检查项与清单配置](docs/inspection-catalog-config.md)。配置 OceanBase 的本地调试方式见 [本地模拟任务与证据查看](docs/local-task-debug.md)。

```powershell
python -m compliance_service init
python -m compliance_service demo --scenario clean
python -m compliance_service demo --scenario violation
python -m compliance_service serve
```

服务默认绑定 `127.0.0.1:8080`，本地 `/debug` 仅用于离线调试，不提供人工改判入口。工作进程另行运行：

```powershell
python -m compliance_service worker
python -m compliance_service worker --once
```

数据库默认 `.local/service.sqlite3`，可在子命令之前指定 `--db <path>`。进程重启后，租约过期的任务可恢复；历史快照与结果不可覆盖。

## 生产配置启动

生产模式必须显式提供配置和授权令牌，不能依赖客户端自报矿井身份：

```powershell
$env:COMPLIANCE_TASK_TOKEN = "部署环境注入的令牌"
python -m compliance_service --db .local/production.sqlite3 init
python -m compliance_service --production-config config/production.local.toml --db .local/production.sqlite3 serve
python -m compliance_service --production-config config/production.local.toml --db .local/production.sqlite3 worker
```

配置模板见 `config/production.example.toml`。它只保存环境变量名；数据库、附件和回调目标必须显式白名单。配置不会在加载时连接数据库，worker 执行任务时才使用部署中已验证的 MySQL 兼容 DB-API 驱动。init 会执行内置的 additive schema 迁移，已有账本迁移前应先备份。

生产入口使用配置的 `check_url`（默认 `POST /inspection/tasks`）和 `X-Inspection-Token`。成功只表示控制请求已持久化，响应为：

```json
{"code":200,"success":true,"msg":"任务控制请求已生效"}
```

响应头 `X-Inspection-Task` 提供任务键；随后用 `GET {check_url}/{task_key}?mine_id=...` 查看任务和每项运行状态。生产配置下 debug/manual 路由不可用；`GET /v1/runs/{run_id}/progress` 受令牌和矿井归属保护。

上游默认只接受 `running`。暂停/恢复必须在 `[status_map]` 中写入双方确认的 wire 字符串；暂停在文件、页面或 provider 检查点协作生效。存在 `executionId` 时它参与执行身份；没有时使用 taskId 或稳定请求指纹。重复终态请求复用原任务；需重新执行时使用新的 executionId，不承诺远端 exactly-once。

## 本地任务接口

本地 fixture 可使用 `POST /v1/manual/runs`，例如：

```json
{
  "sourceSystem": "demo",
  "mineCode": "synthetic-mine",
  "checkCycle": "2026-10",
  "scope": {"fixture": "clean"},
  "callbackMode": "mock",
  "items": [{"itemCode":"stop_work_unload"},{"itemCode":"relief_effects"}]
}
```

各检查项独立受理、执行和交付；同范围活动运行合并，终态后新请求创建新运行。`GET /v1/runs/{run_id}` 返回快照、结果、证据和投递状态。fixture 是合成事实集选择，不是人工指定业务结果的生产接口。

## 测试和已知限制

```powershell
python -m unittest discover -s tests -v
python -m compileall -q compliance_service tests
```

关键覆盖包括 `test_upstream_integration.py`、`test_task_ledger.py`、`test_oceanbase_acquisition.py`、`test_callbacks.py`、`test_evidence_boundaries.py` 和 `test_recognition_recovery.py`。这些测试使用注入的数据库、文件和本机回调，不连接真实 OceanBase 或正式上游。真实资料版式、OCR/视觉业务准确率、跨月适用、正式回调实收、生产身份授权和 M6 专家盲测仍待确认。
## 相关文档

- [生产适配器配置与联调说明](docs/production-integration.md)
- [开发待办](docs/development-tasks.md)
- [验收矩阵](docs/acceptance-matrix.md)

前端重构验证记录见 [frontend-verification-2026-10-26.md](docs/frontend-verification-2026-10-26.md)。
