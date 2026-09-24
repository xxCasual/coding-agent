# 部署与复现

本说明覆盖本机 conda 开发路径和 Docker Compose 服务。凭据只用占位符；不要把真实密钥写入仓库。

冻结实现：`b9e8fcc`（M11）。日常开发用已确认的 conda 环境 `review-agent`（Python 3.11.15），不要为了「换解释器」另装一套依赖。

## 两条路径

| 路径 | 用途 | 说明 |
| --- | --- | --- |
| Compose 全栈 | 一键 Demo | `docker compose up -d --build`；见下文 [Compose 一键 Demo](#compose-一键-demo) |
| conda `review-agent` | 日常开发、CLI、本机 API | `PYTHONPATH=src`；Postgres/Redis 仍建议用 Compose 提供 |
| Compose 仅 `postgres`/`redis` | 数据层 | 与 conda API/worker 共用 |

默认执行后端是 Docker：`REVIEW_AGENT_EXECUTOR_BACKEND=docker`。显式本机开发或旧 PR 验证工具才设 `host`。pytest 会强制 host。

## 环境变量

复制 [.env.example](../.env.example) 为 gitignored 的 `.env`。最少需要：

```bash
export DEEPSEEK_API_KEY="sk-your-key-here"          # 正式编码任务；无密钥则 run 无法完成模型步骤
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
export DEEPSEEK_MODEL="deepseek-v4-pro"
export REVIEW_AGENT_DATABASE_URL="postgresql+psycopg://review_agent:review_agent@localhost:5432/review_agent"
export REVIEW_AGENT_DATA_ROOT="$HOME/.review-agent"
export REVIEW_AGENT_CELERY_BROKER="redis://localhost:6379/0"
export REVIEW_AGENT_API_URL="http://127.0.0.1:8000"
export PYTHONPATH=src
```

可选：

- `REVIEW_AGENT_EXECUTOR_BACKEND=host` — 仅本机开发
- `REVIEW_AGENT_APPROVAL_MODE=confirm|auto` — 默认 `confirm`：写操作需 Web「允许」或 `POST /api/approvals/{id}/decision`。`auto` 仅用于脚本化实验，Compose 一键 Demo 保持 confirm
- `REVIEW_AGENT_SKILLS_ENABLED=true|false` — baseline 评测为 false
- `REVIEW_AGENT_CONTEXT_OPTIMIZATION=true|false` — baseline 评测为 false
- `REVIEW_AGENT_MCP_SERVERS` — JSON 数组；空则不拉起 MCP 进程
- `REVIEW_AGENT_CONTRACT_ROOT` — 契约样例目录，例如 `examples/contracts`

MCP 配置示例（一行 JSON）：

```bash
export REVIEW_AGENT_MCP_SERVERS='[{"server_id":"api_contract","command":["python","-m","review_agent.mcp_servers.api_contract"],"env_allowlist":["PYTHONPATH","REVIEW_AGENT_CONTRACT_ROOT"],"timeout_seconds":30}]'
```

子进程 env 不得包含模型密钥。

## 数据目录与 workspace 登记

`REVIEW_AGENT_DATA_ROOT` 默认 `~/.review-agent`。其中：

| 路径 | 内容 |
| --- | --- |
| `workspace_registry.json` | 已登记仓库；客户端只提交 `workspace_id`，不传任意磁盘路径 |
| `locks/{run_id}.lock` | 单机文件锁 |
| 每个 run 的副本、产物、命令日志 | 与原工作区分离；不回写、不 reset 用户仓库 |

登记示例：

```json
{
  "workspaces": [
    {
      "id": "eval-fastapi-service",
      "display_name": "Eval FastAPI service",
      "path": "/absolute/path/to/eval/samples/fastapi-service"
    }
  ]
}
```

改 registry 后重启 API。未登记路径调用 `review-agent agent /some/path` 会退出 1，并提示上述文件位置。

## 启动顺序（conda + Compose 数据层）

1. 启动 Postgres 与 Redis（不启动 Compose 里的 api/worker 也可）：

```bash
docker compose up -d postgres redis
```

等到两个容器 `healthy`。本机 2026-09-10 复现：`code_review_agent_upgrade-postgres-1`、`code_review_agent_upgrade-redis-1` 均为 healthy；Redis `PING` → `PONG`。

2. 业务迁移（Alembic 只管业务表，head 为 `0005_run_task_mode`）：

```bash
REVIEW_AGENT_DATABASE_URL=postgresql+psycopg://review_agent:review_agent@localhost:5432/review_agent \
  conda run -n review-agent alembic upgrade head
```

**已有库的版本漂移**：若 `alembic current` 仍是 `0001_task_entities`，但 `runs.owner_id` 已存在（本环境 2026-09-10 即如此），不要反复 `upgrade`。先确认列后：

```bash
conda run -n review-agent alembic stamp 0002_run_lease
conda run -n review-agent alembic upgrade head
```

缺少 `ix_runs_dispatch_pending` 时：`CREATE INDEX IF NOT EXISTS ix_runs_dispatch_pending ON runs (dispatch_pending)`。升级代码前应 `upgrade head` 到 `0005_run_task_mode`（含 `tool_executions.replay_category`、`runs.task_mode/review_target`；旧任务默认为 develop）。R2 本轮仅核对离线迁移 SQL，实库迁移未运行；下方历史记录不代表此次迁移已经应用。

新库直接 `upgrade head` 即可，不必 stamp。

3. LangGraph checkpoint 表由官方 saver 创建，**不是** Alembic：

```bash
PYTHONPATH=src REVIEW_AGENT_DATABASE_URL=postgresql+psycopg://review_agent:review_agent@localhost:5432/review_agent \
  conda run -n review-agent python -c "import asyncio, os; from review_agent.services.graph_checkpointer import setup_postgres_checkpointer; asyncio.run(setup_postgres_checkpointer(os.environ['REVIEW_AGENT_DATABASE_URL']))"
```

`setup()` 只建表，不等于任务恢复完成。

4. 写好 `workspace_registry.json` 后启动 API：

```bash
PYTHONPATH=src \
REVIEW_AGENT_DATABASE_URL=postgresql+psycopg://review_agent:review_agent@localhost:5432/review_agent \
REVIEW_AGENT_DATA_ROOT="$HOME/.review-agent" \
REVIEW_AGENT_CELERY_BROKER=redis://localhost:6379/0 \
  conda run -n review-agent uvicorn review_agent.api.app:app --host 127.0.0.1 --port 8000
```

无 broker 时 `POST /api/sessions/{id}/runs` 仍返回 202，run 保持 `queued` + `dispatch_pending`，不会自己执行。

5. 可选 Celery worker / beat（prefork；任务只传 `run_id`）：

```bash
PYTHONPATH=src \
REVIEW_AGENT_DATABASE_URL=postgresql+psycopg://review_agent:review_agent@localhost:5432/review_agent \
REVIEW_AGENT_DATA_ROOT="$HOME/.review-agent" \
REVIEW_AGENT_CELERY_BROKER=redis://localhost:6379/0 \
  conda run -n review-agent celery -A review_agent.worker.celery_app worker --loglevel=info --pool=prefork

PYTHONPATH=src REVIEW_AGENT_CELERY_BROKER=redis://localhost:6379/0 \
  conda run -n review-agent celery -A review_agent.worker.celery_app beat --loglevel=info
```

## Compose 一键 Demo

容器内路径与宿主机不同：Compose 使用 `REVIEW_AGENT_DATA_ROOT=/var/lib/review-agent`（named volume），workspace 登记为容器内 `/app/eval/samples/...`，由 `scripts/compose-seed-registry.py` 在 API 启动时写入。执行器为 `host`（容器内直接跑命令，不嵌套 Docker）。

1. 准备密钥：

```bash
cp .env.example .env
# 编辑 .env，至少设置 DEEPSEEK_API_KEY
```

2. 启动全栈（postgres、redis、api、worker、beat）：

```bash
docker compose up -d --build
```

首次启动 api 会 `pip install -e .`（含 dev 依赖与 eval 样例 requirements）、跑 Alembic、创建 checkpoint 表、种子 registry。`api` healthcheck 通过后再起 worker/beat。首次冷启动在慢网络下可能需 **5–15 分钟**；Compose 默认使用清华 PyPI 镜像与共享 `pip_cache` volume，worker/beat 复用缓存会快很多。

3. 验证：

```bash
bash scripts/compose-smoke.sh
# 默认最多等 600s（与 api healthcheck start_period 一致）；可设 COMPOSE_SMOKE_MAX_WAIT
# 或手动：
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8000/api/workspaces
```

应看到 `demo-fastapi`、`demo-python-backend`、`demo-llm-adapter`（样例目录存在时）。**确保 8000 端口未被本机 conda API 占用**，否则 smoke 会打到错误实例。

4. 浏览器打开 `http://127.0.0.1:8000/`，选择 `demo-fastapi`，创建 session 并提交需求（需要 `Idempotency-Key`）。写操作默认需审批，在 Web 点「允许」。worker 会从 Redis 领取 `run_id` 并执行。

可选 CLI 自动化（需 `DEEPSEEK_API_KEY`，自动 allow 写操作）。`run.reconciliation` / patch 核对类 `needs_attention` **不会**自动 resume，脚本打印 run JSON 后非 0 退出，避免空转：

```bash
python3 scripts/compose-demo-run.py
```

5. 仅数据层（与 conda 开发共用）：

```bash
docker compose up -d postgres redis
```

| Compose 变量 | 值 | 说明 |
| --- | --- | --- |
| `REVIEW_AGENT_EXECUTOR_BACKEND` | `host` | 容器内不能嵌套 `docker run` |
| `REVIEW_AGENT_DATA_ROOT` | `/var/lib/review-agent` | 与 conda 的 `~/.review-agent` 分离 |
| `REVIEW_AGENT_MCP_SERVERS` | `${REVIEW_AGENT_MCP_SERVERS:-[]}` | 段 1 默认关闭；段 2 在 `.env` 设置 JSON 即可覆盖 `compose.yaml` |
| `REVIEW_AGENT_APPROVAL_MODE` | 默认 `confirm`（不在 compose 里改） | Web 点「允许」；`compose-demo-run.py` 经 API 自动 allow |
| `PIP_INDEX_URL` | 清华镜像（可覆盖） | 加速容器内 pip |

Compose 的 `environment` 块优先于 `.env` 的同名项，但 `REVIEW_AGENT_MCP_SERVERS` 已写成 `${...:-[]}`，因此 `.env` 可以启用 MCP。其它写死在 `compose.yaml` 的项（如 `REVIEW_AGENT_EXECUTOR_BACKEND=host`）仍以 compose 为准。

无 `DEEPSEEK_API_KEY` 时 run 会在模型步骤失败；不要声称编码任务已完成。

停止：`docker compose stop` 或 `docker compose down`（`-v` 会删库）。

## 一条已验证路径（2026-09-10，无付费模型，conda）

在升级仓根目录、conda `review-agent`：

1. `docker compose up -d postgres redis` → 两容器 healthy。
2. Alembic 经 stamp `0002_run_lease` 后 `upgrade head` → `0004_tool_replay_category`。
3. `setup_postgres_checkpointer` → `checkpointer_ok`。
4. `REVIEW_AGENT_DATA_ROOT=/tmp/review-agent-m12-repro` 启动 uvicorn。
5. `GET /api/health` → `{"status":"ok"}`。
6. `GET /api/workspaces` → 列出已登记的 `eval-fastapi-service`。
7. `GET /` → 200，FastAPI 托管的 React 运行台。
8. `review-agent agent /tmp/not-registered-m12` → 退出 1，提示 registry，未向 API POST 绝对路径。

无 `DEEPSEEK_API_KEY` 时不要创建编码 run 并声称完成。产物与隐藏验收复用 FakeModel 夹具：`PYTHONPATH=src conda run -n review-agent python -m pytest -q tests/test_eval_runner.py`（本环境 **8 passed**，非正式评测）。

## 客户端

```bash
export REVIEW_AGENT_API_URL=http://127.0.0.1:8000
review-agent agent --workspace eval-fastapi-service
# 或：review-agent agent /absolute/path/to/registered/repo
```

浏览器打开 `http://127.0.0.1:8000/`。选择登记 workspace，创建 session，提交需求（需要 `Idempotency-Key`）。产物：`GET /api/runs/{id}/artifacts`。

兼容入口：`review-agent review <github-pr-url>`（旧 PR 流程，SQLite，不是 coding TaskService）。

## 取消、审批、恢复、停止

| 状态 | 操作 |
| --- | --- |
| 任意非终态 | `POST /api/runs/{id}/cancel`。响应不能当成「命令已退出」；UI/CLI 看 `cancel_requested` 直到服务确认 |
| `waiting_approval` | `POST /api/approvals/{id}/decision`（allow/deny）。不要用 resume |
| `interrupted` | `POST /api/runs/{id}/resume`，`action=continue` |
| `needs_attention` | `accept_and_continue` 或 `end_task`，带当前 `workspace_revision`。未知退出码不得改成成功 |
| 终态 | resume 返回 `task.terminal_run` |
| 取消中且副作用未知 | 保持 `needs_attention` + `cancel_requested`；核对后 `end_task` → `cancelled`。不要重放未知命令 |

停止：

```bash
# 停本机 API / worker / beat：Ctrl+C 或结束对应进程
docker compose stop worker beat api    # 若用了 Compose 应用服务
docker compose stop postgres redis     # 数据层；卷会保留
docker compose down                    # 停容器，默认保留 named volume
```

数据卷：`review_agent_pg`、`review_agent_data`。`docker compose down -v` 会删库，确认后再用。

## 评测命令（有密钥后再跑）

见 [eval-report.md](eval-report.md)。无密钥或无预算时不要制造结果文件。
