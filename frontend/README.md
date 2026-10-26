# 本地检查工作台

Vue 3、TypeScript、Vite 独立前端。页面按路由懒加载，接口继续由现有 FastAPI 提供，默认地址 `/debug/`，路由使用 hash，避免额外部署 SPA 路由规则。

```powershell
cd frontend
npm install
npm run dev
```

开发页面为 `http://127.0.0.1:5173/debug/`，将 `/v1` 请求代理到 `127.0.0.1:8091`。日常使用：

```powershell
npm run build
```

构建输出到 `compliance_service/web/`，由 FastAPI 托管；`scripts/start-local-debug.ps1` 会在构建产物缺失或前端源码更新时自动构建，用户日常只需要启动 PowerShell 脚本。

三个入口：任务记录、检查项配置、手动 JSON 触发。任务摘要、报告、附件、证据和进度使用独立的轻量接口；证据和附件按需分页加载。完整文档快照不进入列表页。
