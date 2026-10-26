# M0 M1 开发与验收基线

2026-10-23 第二轮独立评审放行 M0/M1。两路复评均未发现需要等待真实源端、OCR 或最终法规组合才能解决的开工阻塞。用户最新范围优先于原 DOCX 中人工复核改判等旧设计。

## 采用的边界

不设人工改判、审批或 review_state。仅 execution_state、不可变 result、delivery_state；后续证据查看和反馈也不能修改历史业务结论。优化规则或解析后创建新运行。

本轮建设独立安装项目、受理用例、SQLite 账本、双入口、租约恢复、合成事实和 mock 交付，不接真实 OceanBase、OCR、MinIO 或上游网络。旧 document-parser 保持只读参考，不导入其代码。

## 本地技术选择

Python 3.12，SQLite（标准库）用于单机事务/恢复，FastAPI/Uvicorn 使用本机已有安装。测试使用标准库 unittest，无须安装测试工具。无消息中间件：本轮持久运行账本直接供工作进程轮询，避免数据库与队列双写；后续队列仅作唤醒，不更改受理和恢复语义。此选择不代表生产数据库、多机并发或外部系统已验证。

## 身份与状态

活动范围键由 environment、source_system、mine_code、item_code、check_cycle、显式 scope、definition_version 的规范化 JSON 计算 SHA256。callback_mode 不影响范围，同范围回调模式不同返回冲突。最终文件清单和字节哈希不参与受理键；取数后单独保存 input_snapshot。当前运行完成后再次触发创建新 run_id。

本地 sourceSystem/mineCode/environment 是归属条件，并非生产鉴权。所有查询带这三个条件；正式接入时必须来自可信身份，不能直接信任客户端声明。

执行为 queued → running → completed/failed；租约过期由新工作器接管，fence 单调增加，所有工作器写入须检查 fence、持有人和有效期。普通处理异常持久化为技术 failed，不阻塞其他项；进程崩溃后最多执行3次，下一次接管以 failed 收束而不再执行。pending/unverifiable 是 completed 的业务结果，不是等待人工介入。

交付为 none 或 waiting → sending → delivered；失败回到 waiting 重试同一已保存 payload。交付租约与执行租约分开，不能因为交付失败重新检查。mock 接收器按 delivery_id 去重，仅证明本地交付语义，不承诺远端 exactly-once。

## 模拟判断政策

两项定义标记 engineering-mock-v1，仅用合成命题验证工程机制，不代表真实法规规则。模拟聚合顺序为：技术错误 failed；已登记证据证明任一必要命题为假 no_pass（同时保留覆盖缺口）；必要内容不可读 unverifiable；材料缺口或观察不完整 pending；所有必要命题有登记证据且为真才 pass。未定义适用条件不自动输出 N/A。

## 最小验收

- 同范围20并发一个活动运行；失败事务不遗留占位或待办。
- 五类终态后再次触发新运行；旧快照和结果不变。
- 多检查项独立执行、独立交付；单项技术失败不丢另一项。
- 中断后恢复阶段；新 fence 生效后旧工作器所有写入被拒绝。
- mock 超时/错误重投同一定版结果，不创建新运行，不重算。
- 矿井、来源、环境、运行、输入快照与证据引用不匹配时拒绝读取/完成。
- 全部测试可在禁止 socket 网络调用的环境执行；real 回调与未配置 provider 均拒绝。

## 后续放行

M1 通过只表示内部运行机制成立。M2/M3 接真实文件、源端与解析；M4 验证两项实际合规规则与准确性；M6 固定真实验收集并验证质量。盲测和分层评测不阻塞 M0/M1，外部网络安全在对应真实接入时完成。
