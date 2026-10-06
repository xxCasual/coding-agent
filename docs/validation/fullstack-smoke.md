# 可复现的无 Key 完整链路验证

2026-10-06，macOS Apple Silicon，公开代码基线 `7f112a8`，加本轮验证脚本。模型决策为脚本化 FakeModel，PostgreSQL、Redis、HTTP API、Celery prefork、文件和验收命令均真实执行。结果见 [JSON 记录](fullstack-smoke-20261006.json)。

## 覆盖范围

从空数据库运行 Alembic 到 0005，建立 PostgreSQL checkpoint；登记一个临时 FastAPI Git 仓库；HTTP 提交任务并核对同一幂等请求获得同一 run；无 worker 时状态 queued；启动 prefork worker 后在补丁写入前暂停审批。

停止并重启 API/worker，核对审批 ID 和源文件仍一致，批准后从 PostgreSQL checkpoint 继续，执行真实 pytest，最终 `succeeded` / `verification=passed`；补丁下载内容与登记 SHA-256 一致，原仓不变。SSE 带 Last-Event-ID 只取得其后的事件；再次投递相同 run 不重复改变已完成结果；API 重启后仍读取最终状态。Web HTML 及入口已检查，浏览器交互沿用既有前端测试。

## 重现

必须使用空的专用数据库；脚本在发现已有表时拒绝运行，不删除表。

```bash
uv sync --frozen --extra dev
docker compose -p smoke-coding up -d postgres redis
docker compose -p smoke-coding exec postgres createdb -U review_agent review_agent_smoke
REVIEW_AGENT_DATABASE_URL=postgresql+psycopg://review_agent:review_agent@127.0.0.1:5432/review_agent_smoke \
REVIEW_AGENT_CELERY_BROKER=redis://127.0.0.1:6379/14 \
  uv run --extra dev python scripts/fullstack-smoke.py --report /tmp/fullstack-smoke.json
docker compose -p smoke-coding down
```

这条路径启动容器数据库/Redis，本机运行 API 与 prefork worker，Web 使用仓库已构建资源；不等于已重跑 Compose 应用镜像全栈。使用 host 执行器，不代表 Docker 执行隔离认证或真实模型编码质量。

## CI 与回归

integration job 启动 PostgreSQL 16、Redis 7 service 容器，先探测服务，运行 12 项 PostgreSQL、checkpoint、Redis 集成测试，并断言 JUnit 没有 skipped；另建空数据库运行上述 smoke。原 python/frontend job 保留。

远端确认：[run 37419790628](https://github.com/xxCasual/coding-agent/actions/runs/37419790628)，提交 `895cdc7`，python / integration / frontend 全绿。

本轮另外核对 TaskService、编码 runtime、恢复、Reviewer、Celery 相关回归：80 passed、1 skipped（该次未配置 Redis）；Redis 的独立服务测试已在上述 12 项中执行。worker 重启、取消和模型超时的历史多进程证据仍见 [原报告](../fullstack-validation.md)，当前确定性回归不会替代历史模型实验。
