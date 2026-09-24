# 架构说明

主产品入口是 **coding TaskService**。CLI 与 React 运行台只走 HTTP，不直连 runtime。旧 PR review 仍走独立的 ReviewService / SQLite，不参与编码 run。

```mermaid
flowchart TB
  cli[CLI review-agent agent]
  web[React console at slash]
  api[FastAPI TaskService API]
  pg[(PostgreSQL sessions runs events)]
  redis[(Redis queue only)]
  worker[Celery worker prefork]
  graph[coding StateGraph]
  tools[ToolRegistry]
  exec[Executor and workspace copy]
  skill[Skills on demand]
  mcp[MCP stdio client]
  reviewer[LocalReviewService read-only]

  cli --> api
  web --> api
  api --> pg
  api -->|"publish run_id"| redis
  redis --> worker
  worker -->|"run_once"| graph
  graph --> tools
  tools --> exec
  tools --> skill
  tools --> mcp
  graph --> reviewer
  graph -->|"thread_id equals run_id"| pg
```

## 职责

| 部分 | 做什么 | 不做什么 |
| --- | --- | --- |
| CLI / Web | 选已登记 workspace、提交需求、SSE 续读、审批/取消/核对 | 不把本机任意路径 POST 给 API；关页不等于取消 |
| TaskService | 创建 session/run、幂等、事件、产物、派发 | 队列不是业务真相 |
| Celery | 只传 `run_id`；领取后从 Postgres 读状态 | 不用 Redis 存 run 结果 |
| coding graph | 模型决策 → 单工具执行 → 验收/预算；CONFIRM 先写审批意图再 `interrupt()` | 不把模型调用和有副作用工具塞进同一不可见节点 |
| ToolRegistry | 参数校验先于权限与执行；本地 Pydantic，MCP 用 JSON Schema | 重名拒绝覆盖 |
| Skills | `load_skill` / 资源 / 脚本；脚本走同一 Executor | 正文不授予权限；`REVIEW_AGENT_SKILLS_ENABLED=false` 时不注入 |
| MCP | 首发 stdio；工具名 `mcp__{server_id}__{tool_name}` | 未配置则不拉起进程；HTTP transport 属 O01 |
| Reviewer | 只读需求/diff/源码/验证证据；主 Agent 经 `delegate_review` 委派 | 无写工具、无通用命令、不递归委派。`unavailable` 不是「无问题」 |
| Executor / WorkspaceManager | 独立副本、文件版本、`workspace_revision`、命令取消 | 不提交/reset 原仓库；目录外符号链接拒绝 |

对照评测：baseline 关 Skill 注入、上下文优化与 Reviewer；full 打开。两者不是与旧 V1 PR 协议混比。

## 恢复时核对什么

接管或 resume 前：

1. **文件**：当前副本的 `workspace_revision` / patch hash，与审批或核对请求绑定。已生效 patch 按 `call_id` 补记，不重复 `git apply`。
2. **调用记录**：完整 `ToolResult` 按 `call_id` 复用；MCP/外部无完整结果、部分 patch、无句柄孤儿命令 → `needs_attention`，不自动重放。
3. **所有权**：租约（`owner_id` / `lease_until`）+ `$DATA_ROOT/locks/{run_id}.lock`。有效租约或仍持锁的旧进程会拒绝第二执行者。
4. **预算与用量**：恢复沿用累计消耗，不重置。未知 token/费用为 null，不记零。

官方 `AsyncPostgresSaver.setup()` 只保证 checkpoint 表存在。
