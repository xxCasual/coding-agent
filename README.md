# Coding Agent

把本地 Git 仓库里的需求做成可验证的代码改动：连续对话、隔离工作副本、工具调用、Skills/MCP、独立 Reviewer、异步 worker 与步骤恢复。CLI 与浏览器运行台共用同一套任务 API。旧的 GitHub PR 审查仍可用，但是兼容功能。

正式任务范围是 Python / FastAPI 与小型 AI 应用。默认单人本机；仓库必须先在服务端登记。

## 当前状态与使用方式

截至 2026-09-24，R1–R4 的 Coding Agent 增量改造已交付：审查目标、任务模式、协作展示和预算内真实场景已接入现有架构。**R2 PostgreSQL 实库补验和本机 API/worker/CLI/Web 链路已通过**，并完成小型 FastAPI 真实开发与规划；过程中的缺陷及修复见 [完整链路报告](docs/fullstack-validation.md)。

2026-10-06 补齐 [无 Key 完整链路冒烟](docs/validation/fullstack-smoke.md)：空数据库迁移、真实 PostgreSQL/Redis、HTTP API、Celery prefork、审批期间重启、checkpoint 续跑、真实 pytest 验收、补丁下载及 SSE 续读均通过。CI 增加 PostgreSQL/Redis service job；这条冒烟的模型决策是脚本化的，不代表真实模型效果。

| 模式 | 适合做什么 | 交付与权限 |
| --- | --- | --- |
| 开发 `develop`（默认） | 新功能、修复、同会话追加需求 | 在独立副本修改，执行验收，需要时委派 Reviewer，交付 patch；不自动合并原仓 |
| 只审查 `review` | 检查原仓未提交改动或指定文件 | 只读报告，保留问题位置、证据、实际读取与未检查项；不自动修复 |
| 只规划 `plan` | 理解现有实现、拆解需求 | 输出文件、步骤、拟议验证和不确定项；不写代码或运行项目脚本 |

Web 输入区可选择模式及审查范围，CLI 使用下方参数。任务开始后模式固定；下一轮明确切换为开发才开始实施。Reviewer 有独立上下文与只读工具；运行台展示审查轮次、主 Agent 的处理理由及当前版本验证，旧版本结果不冒充当前通过。

审查范围区分 `workspace_changes`（原仓输入时已有改动）、`run_changes`（本轮产生的改动）与 `paths`（指定文件/目录）。原仓输入与 Agent 新改动分别保留，未跟踪文本文件也纳入相应范围。

## 快速开始

### 无 Key 冒烟（推荐先跑）

```bash
uv sync --frozen --extra dev
uv run --extra dev python scripts/fake-model-smoke.py
```

脚本在临时目录建一个带失败测试的 Git 仓库并登记，经 HTTP API 提交开发任务，验收命令为 `pytest -q`；脚本化的假模型读取文件并提交补丁。预期输出 `run_status: succeeded`、`verification: passed`、可下载的 `delivery.patch`，且原仓库未被修改，最后一行为 `SMOKE OK`。它只验证平台流程（登记、隔离副本、工具、验收、产物、API），不代表模型编码能力，也不经过 PostgreSQL / Celery。2026-10-06 已在 macOS（Apple Silicon）全新 clone 上验证。

### Compose 一键 Demo

```bash
cp .env.example .env          # 填入 DEEPSEEK_API_KEY
docker compose up -d --build  # 首次冷启动可能 5–15 分钟（pip）；需 DEEPSEEK_API_KEY
# 若本机 conda uvicorn 占用 8000，先停掉，否则会打到 ~/.review-agent 而不是容器
bash scripts/compose-smoke.sh # 无模型；最多等约 10 分钟直到 api healthy
open http://127.0.0.1:8000/   # 选择 demo-fastapi，提交需求；写操作点「允许」
```

该路径用于启动完整服务栈；R4 使用 host + 内存 TaskService，后续已补 PostgreSQL/Redis 与本机 API/worker 验收；尚未重跑 Compose 应用镜像全栈。

启动脚本负责：迁移、LangGraph checkpoint、workspace 登记（容器内 `/app/eval/samples/...`）、API + Celery worker + beat。详见 [docs/deploy.md#compose-一键-demo](docs/deploy.md#compose-一键-demo)。

### conda 开发路径

已确认环境：conda `review-agent`（Python 3.11.15）。不要另建解释器顶替它。完整顺序、迁移和停止见 [docs/deploy.md](docs/deploy.md)。

```bash
cp .env.example .env   # 填入密钥；.env 不提交
docker compose up -d postgres redis
PYTHONPATH=src REVIEW_AGENT_DATABASE_URL=postgresql+psycopg://review_agent:review_agent@localhost:5432/review_agent \
  conda run -n review-agent alembic upgrade head
# checkpoint 表：见 docs/deploy.md 中的 setup_postgres_checkpointer
```

在 `$REVIEW_AGENT_DATA_ROOT/workspace_registry.json`（默认 `~/.review-agent`）登记本地 Git 路径，然后：

```bash
PYTHONPATH=src \
REVIEW_AGENT_DATABASE_URL=postgresql+psycopg://review_agent:review_agent@localhost:5432/review_agent \
REVIEW_AGENT_CELERY_BROKER=redis://localhost:6379/0 \
  conda run -n review-agent uvicorn review_agent.api.app:app --host 127.0.0.1 --port 8000
```

另开终端：

```bash
export REVIEW_AGENT_API_URL=http://127.0.0.1:8000
conda run -n review-agent review-agent agent --workspace <registered-id>
```

任务模式可通过 CLI 选择：

```bash
review-agent agent --workspace <registered-id> --mode plan
review-agent agent --workspace <registered-id> --mode review
review-agent agent --workspace <registered-id> --mode review --review-path app.py --review-focus "接口边界"
```

只规划会输出涉及文件、步骤、拟执行验证和不确定项；只审查返回报告，两者不改代码或运行项目脚本。任务结束后可用 `/mode develop` 为下一条需求开启开发；活动任务不能切换模式。部署此版本前运行 `alembic upgrade head` 到 `0005_run_task_mode`。专用测试库迁移、跨连接持久化及后台链路已补验，见 [完整链路报告](docs/fullstack-validation.md)；此前真实模式使用证据见 [R4 补验报告](docs/r4-followup-evidence.md)。

或打开 `http://127.0.0.1:8000/`。无 Celery worker 时，创建 run 会排队（`queued` + `dispatch_pending`），不会自动跑模型。

## 一条任务路径

1. 登记 workspace（只提交 id，不把任意绝对路径 POST 给 API）。
2. 创建 session，提交需求（必需 `Idempotency-Key`）。
3. 可选启动 Celery worker；否则开发检查可在有 TaskService 的进程内 `run_once`（测试路径，不是生产 CLI）。
4. 在隔离副本上看 diff 与检查；产物经 `GET /api/runs/{id}/artifacts`。原仓库不被 commit/reset。
5. 审批、取消中、`needs_attention` 的操作见 [docs/deploy.md](docs/deploy.md)。

无模型密钥时不要声称编码任务已完成。确定性 FakeModel 夹具不是正式评测成绩。

## 能力、验证与限制

已实现：Coding API 与 SSE 续读、CLI/Web、LangGraph 编码循环、PostgreSQL 存储与 Celery 派发、租约/文件锁、审批/取消/恢复、三个内置 Skill、可选 MCP stdio，以及独立只读 Reviewer。默认执行后端仍为 Docker；host 需显式配置。未知副作用进入 `needs_attention`，不自动重放。

**隔离边界：**“隔离副本”指每次运行把仓库复制到独立工作目录，原仓不被修改；这是文件层面的隔离。下表所有真实模型运行和链路验证都使用 `host` 后端，命令以当前用户权限直接在本机（Compose Demo 中为容器内）执行，没有进程、网络或资源隔离，只应用于可信仓库和需求。Docker 后端（默认镜像 `python:3.12-slim`、网络 `none`）只有自动化测试覆盖，尚未用于真实任务。

**验证记录按各自范围使用：**

| 记录 | 实际结果 | 不能推断什么 |
| --- | --- | --- |
| 实库与完整链路（2026-09-24） | 定向 75 + PostgreSQL 10 + Celery/Redis 6 项通过；真实接口 8 项测试及 9 条独立断言通过；规划完成 | 本机 host、独立进程、Celery solo；开发经修复后恢复，不能称首次无故障或生产认证 |
| R3 客户端（2026-09-22） | 前端 24 项检查、类型检查、构建及本机 HTTP/SSE 核对通过 | HTTP/SSE 使用 FakeModel，并非真模型全栈演示 |
| R4 最新定向检查（2026-09-23） | 84 passed，覆盖 Reviewer、运行时、TaskService、恢复与预算保护 | 不代表全仓测试或 PostgreSQL 实库补验 |
| R4 真实使用 | 开发、只审查、续聊、只规划四场景成功；新 `/clamp` 配对两侧成功并通过隐藏验收，Reviewer off 无委派 | 仅 host + 内存 TaskService，n=1 配对；不证明 Reviewer 普遍收益或任意任务稳定完成 |
| 历史 V02（2026-09-10） | heldout 8 任务 × 两组，n=16，代码产物隐藏验收 16/16；运行状态两组各 5/8 succeeded，见 [原始 JSONL](eval/v02-20260910/) | baseline/full 同时切换多个因素，不能当 Reviewer 单变量对照；24×3 未跑 |

[R4 最新报告](docs/r4-followup-evidence.md) 保留首轮失败、修复与费用记录；历史成绩见 [eval-report](docs/eval-report.md)。不要把 75% 完成率或 20% token 节省等原目标写成实测结果。

真实调用可通过 `REVIEW_AGENT_BUDGET_PATH` 接入**已有累计 CNY 账本**。当前保护仅针对已核价的官方 DeepSeek Flash：请求前预留、限制输入/输出、禁用 SDK 隐式重试，缺失用量保留额度；不自动新建或重置账本。默认未开启，具体契约见 [R4 预算说明](docs/upgrade/CONTRACTS.md#r4-增量契约共用预算与真实调用)。

仍待验证：Compose 应用镜像/prefork 路径及第二模型 profile。MCP Streamable HTTP、Explorer 和自动路由未排期；无多人权限、自动发布或自动回写原仓能力。模型仍可能误报或返回非法格式；格式纠正最多一次，不能把未完成审查当作干净报告。

## 下一步

1. **用于实际小任务**：选择 Python/FastAPI 的短需求，先写清验收标准，再核对 patch、测试与 Reviewer 结果。
2. **按实际阻塞修复**：本轮迁移和完整入口已补验，当前不扩展代理种类或架构。需要使用 Compose 应用镜像时，再验证对应部署路径。

数据库测试始终使用专用测试库；`tests/test_postgres_task_store.py` 会删除/重建业务表。真实调用继续沿用同一累计预算账本，本轮剩余约 4.314 元（保守估算）。

## 兼容：PR review

```bash
conda run -n review-agent review-agent review <github-pr-url>
```

拉取 GitHub PR diff、本地 Python 上下文、可选 LLM findings、SQLite job。不会自动向 GitHub 发布 review comments。静态示例（非正式编码成绩）：

```bash
conda run -n review-agent python -c "from review_agent.demo import write_demo_report; write_demo_report('examples/demo-report.md')"
```

`review-agent memory --cwd .` 只读该目录下旧 `.memory`，不是 TaskService 会话记忆。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/deploy.md](docs/deploy.md) | conda / Compose、登记、迁移、取消与停止 |
| [docs/architecture.md](docs/architecture.md) | 系统关系、Skill/MCP/Reviewer、恢复核对 |
| [docs/fullstack-validation.md](docs/fullstack-validation.md) | 最新实库/完整链路、局部修复、真实接口与规划、费用 |
| [docs/r4-followup-evidence.md](docs/r4-followup-evidence.md) | 最新四场景与 Reviewer 单变量小样本、费用及限制 |
| [docs/eval-report.md](docs/eval-report.md) | 正式评测口径与精简 n=16（24×3 未跑） |
| [docs/demos/README.md](docs/demos/README.md) | 三段演示脚本（待真模型录屏） |
| [docs/upgrade/STATUS.md](docs/upgrade/STATUS.md) | 模块进度与未解决问题 |

前端源码在 `frontend/`。改 UI 后 `npm --prefix frontend run build`，产物在 `src/review_agent/web`，由 FastAPI 托管。

## 检查

CI（`.github/workflows/ci.yml`）与本地使用相同命令：

```bash
uv sync --frozen --extra dev
REVIEW_AGENT_EXECUTOR_BACKEND=host uv run --extra dev pytest -q -rs
npm --prefix frontend ci
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend run build
```

2026-10-06 基础 CI（GitHub Actions `ubuntu-latest`，自带 Docker）与本机启用 Docker 后结果一致：pytest 214 passed、12 skipped，含 Docker 执行与取消测试；基础 job 跳过未配置 PostgreSQL（11）、Redis（1）的集成测试，新增 integration job 提供服务容器并要求这 12 项全部执行，随后运行完整链路 smoke。无 Docker 守护进程时基础 Docker 测试会跳过（213 passed、13 skipped）。前端类型检查、24 项测试与构建通过。[远端 run 37419790628](https://github.com/xxCasual/coding-agent/actions/runs/37419790628)（提交 `895cdc7`）三个 job 全部通过，包含 12 项集成测试和 `FULLSTACK SMOKE OK`。

日常只运行改动及其直接消费者的定向检查。例如复跑 R4 范围（不调用真实模型）：

```bash
PYTHONPATH=src REVIEW_AGENT_DATABASE_URL='' conda run -n review-agent python -m pytest -q \
  tests/test_model_client.py tests/test_local_reviewer.py tests/test_coding_runtime.py \
  tests/test_task_service.py tests/test_recovery.py
```

实库测试另用专用测试数据库配置。前端改动运行现有测试、类型检查和构建；`make check` 使用 uv 路径，已验证的日常解释器以 conda `review-agent` 为准。
