# 简历声明与证据入口

2026-10-06，公开项目说明与面试准备共用本表。实现存在、测试通过和真实模型效果分别判断。

| 声明 | 实现 | 验证 |
| --- | --- | --- |
| 编码、验收和失败修复 | `harness/coding_graph.py`、`acceptance.py`、`workspace_revision.py` | `tests/test_coding_runtime.py`、无 Key smoke |
| 审批、checkpoint、文件哈希 | `services/recovery.py`、`patch_apply.py`、`graph_checkpointer.py` | [恢复注入证据](resume-evidence.md)、`tests/test_recovery.py`、[完整链路 smoke](validation/fullstack-smoke.md) |
| 租约、文件锁、异步派发 | `task_store_postgres.py`、`run_attempt.py`、`run_lock.py`、`worker/tasks.py` | `tests/test_postgres_task_store.py`、`test_celery_dispatch.py`、fullstack smoke |
| 三类 Skill、MCP | `harness/skills/builtin/`、`harness/mcp/`、`mcp_servers/api_contract/` | `test_skills.py`、`test_mcp_registry.py`、`test_mcp_skill_flow.py` |
| 只读 Reviewer 和版本复用 | `harness/reviewer.py`、`review_target.py` | `tests/test_local_reviewer.py` |
| 上下文选择、摘要与裁剪 | `harness/context.py::ContextAssembler` | `tests/test_coding_runtime.py`，保留工具调用与结果配对 |
| 四项联调故障修复 | 产物生成、工具执行状态、Reviewer ID、异常释放租约 | [完整链路报告](fullstack-validation.md)，包含修复前后回归及人工恢复边界 |
| 8 任务、两配置、16 次真实运行 | `eval/manifests/heldout.json`、`eval/hidden/` | [原始记录](../eval/v02-20260910/README.md)、[报告](eval-report.md) |

实现路径相对 `src/review_agent/`，测试路径相对仓库根目录。

## 面试解释

模型 final 之后必须运行冻结的验收条件；验收记录绑定文件内容版本。checkpoint 存图状态，工具账本和文件哈希用于对账外部副作用。未知命令执行结果不能自动重放。

Redis 负责派发，PostgreSQL 是业务状态来源。租约和文件锁共同防并发，仍有单机/共享文件系统及多机 fencing 边界。SSE 的事件序号用于断线续读。

Reviewer 使用独立上下文和只读工具，缓存依据版本及审查输入失效。不能把它的实现存在写成已经证明质量提升。

16/16 是最终代码产物通过隐藏验收，两组 run_status 各 5/8 成功；失败与中断仍存在。baseline/full 同时切换多个因素，不是单变量消融。历史 `b08045b` 不在公开历史，模型别名版本未冻结；小样本不能外推。

上下文实现为按预算选择消息、保留工具配对、近似 token 估算、摘要/截断和文件片段缓存。checkpoint 持久化不等于长期记忆，也不能证明彻底消除窗口限制。
