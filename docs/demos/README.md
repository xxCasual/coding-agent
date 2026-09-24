# 三段演示脚本（待真模型录屏）

**未录屏。** 2026-09-10 Compose 段 1（`demo-fastapi`）已用真实 DeepSeek 跑通自动化脚本，见文末；精简正式评测 JSONL **n = 16**（V02），不要把 Compose demo 写成评测成绩。录屏请标注任务、模型、代码版本、日期，并展示真实 diff、检查与用量（未知则显示未知）。

确定性替代证据（FakeModel，不是演示成绩）：

| 场景 | 已有夹具 | 提交 |
| --- | --- | --- |
| 修复 / 实现 / 环境失败统计 | `tests/test_eval_runner.py` **8 passed** | M11 `b9e8fcc` |
| Reviewer 遗漏与 disposition | `tests/test_local_reviewer.py` | M09 `d7f1500` |
| worker 中断、未知副作用、取消 | `tests/test_recovery.py` | M08 `7f7624a` |

前置：[deploy.md](../deploy.md)。需要密钥、已登记 workspace、以及（第三段）Celery worker。

## 1. 失败服务修复

样例：`eval/samples/fastapi-service`（tree hash `50acd3c7371c675c725e1d3a`）。开发任务参考 `dev-fs-fix-quantity`：`POST /items` 的 `quantity` 必须是 JSON 整数。

**Compose 路径（推荐）**

1. `cp .env.example .env` 并填入 `DEEPSEEK_API_KEY`
2. `docker compose up -d --build`，等待 api healthy 且 worker 就绪（首次冷启动可能 5–15 分钟）
3. **先停掉占用 8000 的本机 conda uvicorn**，再 `bash scripts/compose-smoke.sh` 确认 `demo-fastapi` 可见
4. 浏览器打开 `http://127.0.0.1:8000/`，选 `demo-fastapi`，`reviewer=off`，提交需求；写操作在 Web 点「允许」

**conda 路径**

1. 按 [deploy.md](../deploy.md) 启动 Postgres/Redis、迁移、API、worker
2. 将样例绝对路径写入 `$REVIEW_AGENT_DATA_ROOT/workspace_registry.json`，重启 API
3. `review-agent agent --workspace <id>` 或打开 `/`，提交该需求，`reviewer=off` 即可
4. 展示隔离副本 diff（原仓库不变）、隐藏验收或任务内检查、用量字段
5. 不要把静态 `examples/demo-report.md` 当成这次运行结果

## 2. Skill + MCP + Reviewer 遗漏修复

样例：`eval/samples/fastapi-service` 或 `examples/fastapi-items`，契约 `examples/contracts`。Skill：`implement-fastapi-endpoint`。MCP：见 deploy 中的 `REVIEW_AGENT_MCP_SERVERS`。创建 run 时 `reviewer=default`。

1. 打开 Skill 与 MCP；完整方案开关与 eval `full` 一致。
2. 提交带契约约束的接口需求（开发集 `dev-fs-contract-post-v1` 可作口播脚本；契约文件在 workspace 内可读，即使 MCP 未连）。
3. 展示 `delegate_review` / `Run.review` findings；主 Agent 修复或记录不采纳理由。
4. Reviewer 失败或 `unavailable` 时明确说出「不是无问题」。
5. 真实模型路径未验证（V02）；stdio MCP 本身已在 M06 用 `tests/mcp_stdio_driver.py` 验证。

## 3. Worker 中断恢复与未知命令核对

1. 启动 API + `celery worker --pool=prefork`（broker 指向本机 Redis）。
2. 提交一个会跑命令的任务；在命令执行中停止 worker 进程（不要 `docker compose down -v`）。
3. 重启 worker：应领取同一 `run_id`，租约/文件锁阻止双写。
4. 对无法确认退出码的命令，界面应停在 `needs_attention`，提供 `accept_and_continue` / `end_task`，绑定 `workspace_revision`。
5. 取消中看 `cancel_requested` 直到服务确认。未知副作用不要重放来「证明已取消」。

Compose 全栈路径见 [deploy.md#compose-一键-demo](../deploy.md#compose-一键-demo)。自动化脚本 `scripts/compose-demo-run.py` 会自动 allow 写操作；遇到 patch 核对类 `needs_attention` 会打印 run JSON 并非 0 退出（避免空转 15 分钟）。2026-09-10 Compose 真模型路径已跑通：`demo-fastapi` run `164240f1-adfa-4b4a-98a0-123de163d84f` status `succeeded`（隔离副本 `quantity: int`），这不是评测 JSONL。[eval-report.md](../eval-report.md) 精简 n=16。Web 手动审批路径仍可用。
