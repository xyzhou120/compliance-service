# 本地技术版本与来源

2026-10-23 实测运行环境为 Windows、Python 3.12.4、FastAPI 0.115.12、Uvicorn 0.34.1、HTTPX 0.28.1；后者只用于HTTP测试。pyproject.toml 记录直接依赖版本，运行无需旧 document-parser 目录、配置或数据库。未安装新的第三方依赖。

本轮未抽取或复制 document-parser 组件，因此没有旧 app 包初始化、副作用或整套旧依赖引入。包导入不会建立数据库或连接源端；CLI 创建服务时才初始化指定 SQLite 路径。

Python 使用 PSF 许可，SQLite 属公有领域，FastAPI 使用 MIT 许可，Uvicorn/HTTPX 使用 BSD 许可；正式交付时应随锁定的完整依赖清单复核各个实际发行包的 LICENSE，不以此简表替代完整许可审查。

本地 schema 版本记录在 SQLite PRAGMA user_version，当前为1。重复初始化同版本幂等；高于当前支持版本时拒绝启动，避免以旧代码打开新结构。当前不承诺生产库迁移/降级兼容性。

项目使用标准库 unittest 验证，不依赖 pytest。当前未安装专门的 lint/typecheck 工具，验证报告分别列出 compileall 和实际测试结果，不将语法编译当作类型检查。

0.2 原生解析使用已安装的 pypdf 5.4.0、python-docx 1.1.2、openpyxl 3.1.5，测试用 Pillow 11.2.1；已声明独立依赖，未引入旧项目或额外安装包。未使用 PyMuPDF 作为运行依赖。
