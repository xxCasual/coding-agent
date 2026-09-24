# 共享接口与工程决策 v1

§1—§7 保留目标契约，并非所有能力均已实现。接入时先看下方当前入口及 [STATUS 未解决问题](STATUS.md#当前未解决问题)，再核对相关源码；历史 handoff 的临时方案不自动修改目标。普通实现细节无需新增架构文档。

## 0. 公共不变量与读取索引

每个模块先读本节，再按模块“开工读取”选择专题；不要求全文加载后续模块契约。

- 复用现有注册表、模型客户端和运行时；接口有明确首建责任，不复制平行业务实现。
- `session_id` 表示连续会话，`run_id` 表示一次需求，内部 `call_id` 与 provider ID 各司其职；原生工具调用与结果必须配对。
- 编码完成与验收通过分开；验证绑定当前文件版本，未运行/跳过不算 passed，未知用量不记零。
- 参数校验先于权限判断与执行；Skill/MCP 内容不自行授予权限。主 Agent 写入，Reviewer 只读。
- 任务源码、可信运行数据与原工作区分开；取消须确认实际执行停止，未知副作用不得盲目重放。
- 下表列出当前接入入口，不替代专题约束；文件内旧路径或历史限制以当前代码和明确差异记录核对。

### 当前实现入口与已知差异

核对基点：升级仓 M01–M12（见 STATUS 完成提交）；2026-09-10。以后公开接口变化时同步维护受影响行及 STATUS，不复制历史日志。

| 边界／目标章节 | 当前实现入口 | 当前限制／差异 |
| --- | --- | --- |
| ToolRegistry／§1、§2 | [tools.py](../../src/review_agent/harness/tools.py)、[tool_params.py](../../src/review_agent/harness/tool_params.py)、[mcp/schema.py](../../src/review_agent/harness/mcp/schema.py) | `validate_arguments` / `execute` / `execute_async`；本地工具仍用 Pydantic，MCP 动态 Schema 用 jsonschema；重名拒绝覆盖 |
| 共享消息与结果／§2 | [models.py](../../src/review_agent/harness/models.py) | 边界类型为 frozen Pydantic（`extra=forbid`）。`RegisteredTool` 等含 Callable 的内部类型仍为 dataclass |
| ModelClient／§1、§2 | [model_client.py](../../src/review_agent/harness/model_client.py) | Protocol 为异步 `complete` / `stream`；`OpenAICompatibleModelClient` 实现该契约。旧 `complete_json` 仅留在 `DeepSeekModelClient`（PR 兼容）。V02 精简评测用 `deepseek-v4-flash`；第二 profile 认证见 V04 |
| AgentRuntime／§3 | [runtime.py](../../src/review_agent/harness/runtime.py)、[coding_graph.py](../../src/review_agent/harness/coding_graph.py)、[graph_checkpointer.py](../../src/review_agent/services/graph_checkpointer.py) | 官方 `StateGraph`，`thread_id=run_id`。隔离工作区用 `tool_registry_factory`（默认 `build_default_tool_registry`）按副本重建，再合并构造时注入的额外工具；注入的 `memory` 实例保留。测试 `InMemorySaver`；有 `REVIEW_AGENT_DATABASE_URL` 时 `AsyncPostgresSaver.setup()`。CONFIRM 先写审批意图再 `interrupt()`；已有 checkpoint 用 `Command(resume=...)`，不覆盖 `step_count=0` 的新 initial |
| TaskStore／§3、§5 | [task_store.py](../../src/review_agent/harness/task_store.py)、[task_store_postgres.py](../../src/review_agent/services/task_store_postgres.py) | Protocol 含 session/run/事件/工具执行/审批/产物（含 `create_run_bundle`、`list_sessions`、`list_approvals`、`set_approval_decision`、`register_artifact`）。`ToolExecutionRecord.replay_category` 供恢复使用。内存实现（检查）+ PostgreSQL（权威） |
| TaskService/API／§5—§6 | [task_service.py](../../src/review_agent/services/task_service.py)、[run_attempt.py](../../src/review_agent/services/run_attempt.py)、[factory.py](../../src/review_agent/services/factory.py)、[routes.py](../../src/review_agent/api/routes.py)、[clients/task_api.py](../../src/review_agent/clients/task_api.py) | coding API、SSE、产物。一次执行尝试在 `execute_run_attempt`；API 与 Celery 经 `build_task_service()` 装配，worker 不 import `api`。`GET /runs/{id}` 含 `workspace_revision` 与 `pending_approval`。CLI/React 只经 HTTP 客户端；R2 新增冻结的 `task_mode/review_target` 与输入版本信息，见下方 R2 契约 |
| CLI / Web 协作展示／R3 | [cli.py](../../src/review_agent/cli.py)、[useCodingSession.ts](../../frontend/src/hooks/useCodingSession.ts)、[ReviewDetails.tsx](../../frontend/src/components/ReviewDetails.tsx) | 沿用 R2 创建/查询和既有 SSE；模式冻结、任务间隔离、范围/读取分列、版本与用量口径见下方 R3 契约 |
| 恢复／§5 | [recovery.py](../../src/review_agent/services/recovery.py)、[run_lock.py](../../src/review_agent/services/run_lock.py)、[lease.py](../../src/review_agent/services/lease.py)、[worker/tasks.py](../../src/review_agent/worker/tasks.py) | 按持久化 `replay_category` 分发（不按工具名名单）。`repeatable_read` 可跳过；`patch_checkable` 按磁盘核对；进程句柄未知或 `non_replayable` 不完整结果 → `needs_attention`。文件锁在 `services/run_lock.py`（`worker.locks` 再导出）。Alembic `0004_tool_replay_category`。官方 saver `setup()` 仍不等于恢复完成 |
| Reviewer／§7 | [reviewer.py](../../src/review_agent/harness/reviewer.py) `run_delegated_review`、[review_state.py](../../src/review_agent/harness/review_state.py)、[tools.py](../../src/review_agent/harness/tools.py) `attach_review_tools` | 主动委派与交付门禁共用 `run_delegated_review`。store 只依赖 `review_state.initial_review_state`。`LocalReviewService` 只读；R1 新增 [ReviewTarget](../../src/review_agent/harness/review_target.py)、原仓输入 diff 和目标/证据复用键，细节见下文 R1 增量契约；V02 full `reviewer=default` 已跑（精简 n=16） |
| 工作副本／执行／§4 | [workspace_manager.py](../../src/review_agent/services/workspace_manager.py)、[executor.py](../../src/review_agent/services/executor.py)、[config.py](../../src/review_agent/config.py) | 默认 `REVIEW_AGENT_EXECUTOR_BACKEND=docker`；`host` 为显式本机开发。pytest 强制 host。无参 `CommandRunner()` 仍 host（旧 PR 验证）。workspace 登记在 `DATA_ROOT/workspace_registry.json` |
| Skills／§7 | [loader.py](../../src/review_agent/harness/skills/loader.py)、M05 接手摘要 | 元数据/正文按需加载，脚本经执行器；`REVIEW_AGENT_SKILLS_ENABLED=false` 时不构造 loader、不注入 hints、不注册 skill 工具。契约文件仍可普通读取。V02 full 打开 Skill（精简） |
| MCP／§7 | [session.py](../../src/review_agent/harness/mcp/session.py)、[api_contract](../../src/review_agent/mcp_servers/api_contract/)、M06 接手摘要 | 首发 stdio；`mcp__{server_id}__{tool_name}`；run 内 Client 生命周期；未配置 `REVIEW_AGENT_MCP_SERVERS` 则不拉起进程。Streamable HTTP 属 O01 |
| Eval／§2、§7 | [eval/runner.py](../../src/review_agent/eval/runner.py)、M11 接手摘要、[eval-report.md](../eval-report.md) | `review-agent eval`；baseline 关 Skill/上下文优化/Reviewer，full 打开。隐藏验收在 `eval/hidden/`，不复制进 Agent 可写目录。计分不依赖 final 文本。V02 精简 n=16（隐藏验收 16/16）；24×3 未跑 |
| 投递材料／§7 | [README.md](../../README.md)、[docs/deploy.md](../deploy.md)、[docs/architecture.md](../architecture.md)、M12 接手摘要 | coding 为主入口；PR 为兼容。三段真模型录屏未做。不自动投递或 push |

D01—D03 已在 STATUS 关闭。conda 已装 celery；本机 Redis 派发夹具已跑。业务 Alembic head 为 `0005_run_task_mode`。Compose worker/beat 全栈未常驻。V02 精简 JSONL n=16；24×3 未跑。

## 1. 代码边界与调用方式

保留 `src/review_agent`。新 coding 能力放在现有 harness、services、api 层；新增 coding graph 与旧 PR graph 分开。命名以职责为准，不先创建空模块或抽象基类树。

| 边界 | 目标能力 | 首建/接入模块 |
| --- | --- | --- |
| ToolRegistry | tools 描述、参数校验、按名查找；统一调用结果 | M01；M05/M06 扩充 |
| ModelClient | 异步流式返回文本、完整工具调用和用量；协议兼容由适配器承担 | M02 |
| AgentRuntime | 用可序列化状态执行一次 run，输出事件并在边界接收消息/取消 | M03 |
| TaskStore | 读写会话、消息、调用记录、事件、run 状态和产物元数据 | M03 最小内存实现；M07 PostgreSQL |
| WorkspaceManager / Executor | 创建输入副本、文件版本、命令生命周期、取消和输出产物 | M04 |
| TaskService | 用上述组件创建、控制、查询任务；客户端不直接操作运行时 | M07 |
| LocalReviewService | 本地需求/diff/源码/验证证据 → ReviewResult | M09 |

模型、工具和 coding graph 采用 async 接口，以便统一流式输出与 MCP 生命周期。纯同步的旧 PR 能力通过明确适配保留；取消长命令必须交给真实进程句柄，不能仅取消 `to_thread` 的等待。Celery prefork 任务以一次 `asyncio.run()` 驱动一次执行尝试，异步连接在该生命周期内建立/关闭。

只为真实替换点定义小 Protocol。M03 的内存 TaskStore 和 M07 的 PostgreSQL TaskStore 服务于同一运行时；不新增通用 ORM Repository 框架、插件市场或跨项目 SDK。

## 2. 消息、工具与验证

目标：共享边界类型为可序列化的 Pydantic 模型（已落成），内部纯计算 dataclass 可以保留。仅有一次规范化，不在每层反复转换同一对象。

| 类型 | 最少信息与约定 |
| --- | --- |
| Message | message_id、session_id、run_id、role、content；assistant 可有 tool_calls；tool 消息含 provider_call_id |
| ToolCall | 内部 call_id、provider_call_id、name、arguments；call_id 在首次完整模型响应时产生，恢复时复用 |
| ToolSpec | name、description、JSON Schema、执行风险与重放类别；本地参数由 Pydantic 模型生成 Schema |
| ToolResult | call_id、success、summary、error_code、artifact_refs、changed_files、verification；可带有界 stdout/stderr 预览 |
| VerificationRecord | passed/failed/unverified/not_applicable、实际检查命令、退出码、执行时 workspace_revision、证据引用 |
| Usage | 输入/输出 token、model/tool 耗时、模型配置 ID；未知用量为 null，费用估算携带价格配置和日期 |

模型的 provider_call_id 原样用于原生 assistant/tool 消息配对；内部 call_id 用于记录和恢复，避免不同模型响应重复 ID 的歧义。M01 的旧 JSON 适配调用可以先生成本地 ID。

所有工具先做服务端参数校验，再判断权限，再调用 handler。未知工具、非法参数、超时和执行失败都是可定位结果；不得把不完整 JSON、无效工具参数或模型文本静默当命令执行。

模型回复 final 是完成建议。编码任务只有在本轮最终文件版本的必需验收通过后，才可称为“验证通过”；没有可执行验收则记录 unverified。纯问答可使用 not_applicable。跳过、无测试、检查工具不可用都不等于 passed。独立评测验收位于 Agent 不可写的位置，由受信任 eval runner 执行（`eval/hidden/`），防止通过修改验收本身获得成功。成功判定不依赖模型 final 文本。

工具风险和重放类别是两个维度：只读、验证、工作区写入、外部/未知副作用；对应可重复读取、受控可重复验证、可核对补丁、不可自动重放。命令含有 pytest 字样不代表安全或可重放。

默认模型决策上限 24 步、运行执行时间上限 20 分钟、验证失败后最多两轮修复；部署配置可调。等待审批时间不计入执行时间。恢复沿用累计消耗，不能重置预算。上下文上限由经过能力核实的模型配置给出，并预留输出空间；未知实际 token 量要标注估算。

## 3. 会话、run 和 graph 状态

session 关联一个登记的 workspace 和一段连续对话；run 是一次用户需求的执行。同一 session 同时只允许一个未结束的 run，包括 queued、running、waiting_approval、interrupted、needs_attention。

目标：LangGraph `thread_id = run_id`，checkpoint 记录该 run 的执行游标；跨 run 历史由 TaskStore 的 session 消息提供。不要把 session_id/run_id 混用，导致下一需求覆盖旧执行点。CONFIRM 路径使用官方 `interrupt()`；等待审批释放 worker，决定后再领取。

新图的边界为：摄取待处理消息 → 组装上下文 → 模型决策 → 参数/权限检查 → 单个工具调用 → 验收/预算判定 → 下一轮或结束。可合并纯计算节点，但模型调用与有副作用工具不可塞进同一个不可见大节点。一次模型响应多个工具时按顺序逐个执行、逐个记录。

checkpoint 保存 ID、游标、待执行调用、预算、摘要、workspace_revision 等 JSON 数据；客户端、连接、锁、进程和 Executor 从运行依赖注入。完整对话只有 TaskStore 一份权威日志；graph 用消息 ID/摄取游标引用，必要上下文是可重建投影。

工具调用前持久化完整 assistant 消息和 call_id，调用后先持久化结果和 tool 消息，再前进 checkpoint。重放同一步使用相同 ID，消息追加必须幂等。执行中补充消息按持久化顺序在下一工具边界摄取，恢复也不会重复摄取。

原生协议的一批 assistant tool_calls 必须都有对应 tool 结果，才能进入下一次模型决策。若补充需求或取消使该批尚未执行的调用失效，记录明确的 skipped/cancelled 工具结果，随后再追加新用户消息；不能留下未配对调用，或在参数仍是半截时执行。

新 coding 的持久 Markdown 记忆位于按 workspace_id 分配的可信数据目录，与 run 源码分开，跨 run 共享；M03 注入 memory_root，M04 配置实际位置。旧本地 memory CLI 仍可读取原 `.memory`，无需迁移/覆盖原文件。共享经验仍须检查相关文件 hash，不能因来自同一 workspace 就自动视为有效。

| run 状态 | 含义与后续动作 |
| --- | --- |
| queued | 已创建或获准继续，等待领取 |
| running | 有执行所有者，正在模型/工具/验收阶段 |
| waiting_approval | 等待具体操作决定；释放 worker 槽位，决定后再派发 |
| interrupted | 已停在可恢复边界，如模型重试耗尽；resume 检查后排队 |
| needs_attention | 文件版本/未知副作用待核对；提供证据，核对前不执行 |
| succeeded | 流程正常结束；同时展示 verification，不能只凭此宣称编码验收通过 |
| failed | 明确无法完成或预算耗尽；后续需求在同 session 创建新 run |
| cancelled | 活动命令已结束并保存状态；后续需求新建 run |

取消请求单独持久化时间/标记，在进程真正停止前保留当前状态并展示“取消中”。任何非终态都可请求取消。已终态重复取消返回当前状态。resume 用于 interrupted 和完成核对的 needs_attention；审批使用审批决定接口；终态 run 不被改回 running。

取消时若副作用未知，先停止执行，保留 needs_attention 与 cancel_requested，人工核对后才标记 cancelled。此时的 resume 核对只完成状态结算，不再运行模型。无需重新执行未知命令来“证明已取消”。

## 4. 工作目录、文件与执行

第一版选择独立目录快照，不先兼容多种 worktree 策略。workspace 由用户在服务端配置登记，客户端提交 workspace_id，不接受任意磁盘路径或任意 clone URL。

首个 run 从登记 Git 仓库建立副本：包含已跟踪文件的当前内容、删除状态和未被忽略的未跟踪文件；跳过原 `.git`、缓存和明确排除的密钥文件，`.env.example` 可保留。记录原 HEAD、是否脏、输入文件 manifest。目录外符号链接拒绝并报告。若包含子模块或 LFS 内容但不能物化，明确报出不支持，不能生成缺文件的“成功”副本。

每个 run 有固定持久路径。副本建立自己的 Git 基线，以便生成包含新增、删除、二进制变动的交付差异，不在用户仓库提交、重置或自动 apply。所有任务状态、输入 manifest、产物和命令日志在任务源码目录之外。

同 session 后续 run 默认从前一 run 已核对的最终副本创建新副本，继承未通过验证的代码时明确保留该状态；不悄悄重新从原仓库开始。unknown/needs_attention 未解决时，先恢复核对或取消核对，不能直接用不明文件状态继续。

workspace_revision 是任务源码文件及模式的内容摘要，排除声明的缓存、环境目录和运行元数据。补丁记录 patch_hash、目标文件前后 hash 和新增/删除信息。命令结束后重新观测变更，不能假定只有 apply_patch 会改文件。

目标：本地文件读写由可信执行层约束路径；任意项目命令默认在容器运行。仅有面向自有样例的显式本机开发模式（`REVIEW_AGENT_EXECUTOR_BACKEND=host`）。命令使用 argv；shell 解释器属于需明确允许的能力，字符串黑名单不能代替隔离。

可信 worker 管理 Docker；每条命令创建有 run/call 标识的容器，挂载该任务源码和必要只读验收，使用登记镜像。取消时停止/强杀对应容器并等待退出。项目容器没有 Docker socket、模型密钥或其他工作区挂载。资源限制和超时固定在 Executor 配置；依赖安装阶段网络显式允许，任务阶段默认关闭。

预构建镜像或一次准备阶段提供依赖，不在每次命令里重新安装。进程/容器操作句柄写入执行记录，便于 worker 重启后核对。M04 负责这些原语，M08 负责何时使用它们恢复任务。

## 5. 持久化、派发与事件

新任务使用 SQLAlchemy 2 + psycopg + Alembic + PostgreSQL；框架 checkpoint 使用官方 PostgreSQL saver。旧 ReviewStore SQLite 留给旧 PR 功能，不搬迁旧数据。

业务表限定为 sessions、runs、messages、events、tool_executions、approvals、artifacts；workspace 登记保留在本地配置，run 存其配置快照；checkpoint 表独立。消息/调用的重放去重通过稳定 ID 完成，事件使用 `(run_id, seq)` 唯一约束。

创建 run 与首条消息、幂等键、待派发标记在同一数据库事务提交。`Idempotency-Key` 按 session 唯一，同 key 同请求返回原 run，同 key 不同内容返回 409。session 行锁/唯一约束共同防止两个活动 run。

Celery + Redis 消息只带 run_id，领取任务后从 PostgreSQL 读取最新状态。使用 run 所有者/租约防止重复执行；一旦失去所有权就不能开始新副作用。接管前确认旧命令/容器已结束，不能仅因租约过期就并发写文件。

单机共享工作目录持有进程级文件锁，随执行进程生命周期释放；旧进程还持锁时新 worker 不启动执行。该约束只适用于本项目单机部署，不扩展为跨主机锁服务。

使用 runs 上的待派发字段和有限重试，不新增通用 outbox 框架。Celery beat 定时触发补偿扫描，worker 启动也扫描待派发任务；扫描幂等，Redis 恢复后可以重新派发。审批、取消和恢复状态保留在数据库，队列结果后端不作为业务真相。

事件最少包含 run_id、seq、timestamp、type、公开 payload；关联工具时带 call_id，关联子任务时带 subtask_id。委派事件为 `delegate.started` / `delegate.completed`，采纳记录为 `review.disposition`。类型覆盖 run 状态、message 收到/摄取、model 文本增量/完成、tool 开始/完成、审批、验证、Skill/MCP、委派和产物。具体 payload 与对应功能一起落成，不预建庞大事件类型体系。

事件先提交再推送，seq 由数据库事务分配；单调递增但客户端不能假设无间隙。模型文本按小块合并持久化，避免每 token 一次事务。SSE 直接续读 PostgreSQL 事件，第一版无需 Redis PubSub；心跳无业务 seq。

客户端使用 Last-Event-ID 或 after 序号续读，只取更大 seq，按 ID 去重。不展示凭据、完整系统提示或不必要的内部推理。完整工具输出写产物，事件带摘要；未知用量不能显示为零。

## 6. 公共 API 的最小补齐

以下均由 M07/M08 提供，M10 消费。错误使用稳定 code/message，可附必要的 validation details；不把堆栈和密钥返回给用户。

| 方法与路径 | 约定 |
| --- | --- |
| GET `/api/workspaces` | 列出登记 workspace 的 ID、显示名和客户端所需的本地映射信息 |
| POST `/api/sessions` | 输入 workspace_id，返回 session；不接受任意服务器路径 |
| GET `/api/sessions` | 简单 limit/cursor 列表，支持运行台刷新 |
| GET `/api/sessions/{id}` | 会话信息及消息分页；返回是否存在活动 run |
| GET `/api/sessions/{id}/runs` | 此会话历史 runs 的分页摘要 |
| POST `/api/sessions/{id}/runs` | 需求、模型配置 ID、可选验收、可选 `reviewer`=`default`\|`off`；必需幂等 header；正常返回 202 + run_id |
| GET `/api/runs/{id}` | 状态、阶段、verification、预算/用量、`workspace_revision`、`review`（findings、status、dispositions、subtask_id）、`pending_approval`（intent/param_summary/workspace_revision/call_id）或 `pending_approval_id`、`needs_attention`/`attention`、取消标记 |
| GET `/api/runs/{id}/events` | SSE，支持 Last-Event-ID/after；无事件时心跳 |
| POST `/api/runs/{id}/messages` | 客户端 message_id 去重，追加需求；终态拒绝并提示创建新 run |
| POST `/api/runs/{id}/cancel` | 幂等请求停止，响应不能谎称命令已退出 |
| POST `/api/runs/{id}/resume` | body：`action`=`continue`\|`accept_and_continue`\|`end_task`，可选 `call_id`、`workspace_revision`。仅 `interrupted` 或已核对的 `needs_attention`；核对不完备返回 `task.reconciliation_required` 及差异/受影响文件 |
| POST `/api/approvals/{id}/decision` | allow/deny；同决定重复请求幂等，不同决定冲突；绑定参数与文件版本 |
| GET `/api/runs/{id}/artifacts` | 产物 ID、种类、摘要、大小、摘要 hash |
| GET `/api/runs/{id}/artifacts/{artifact_id}` | 按受控 ID 读取内容；不接受客户端文件路径 |

运行台列表和产物内容接口是对原文的最小补充，否则新对话刷新与 diff 展示无法落地。保留旧 `/api/reviews` 系列接口。

## 7. 扩展接入与交付边界

Skills 的目录元数据和加载按 Agent Skills 兼容子集；权限和验收选择在受信任配置中，不相信正文自称的权限。MCP 首发 stdio，服务器使用明确配置的命令；仅实现工具发现/调用，不扩展完整资源浏览器、OAuth 管理台或插件市场。

MCP Client 使用官方 Python SDK v2，经 M06 验证后锁版本；同一 run 内管理连接并负责取消/关闭。工具命名使用 `mcp__{server_id}__{tool_name}`，发现重名拒绝覆盖。本地契约 Server 只通过登记 contract_id 访问样例文件。

Reviewer 读取需求、当前 patch、必要源码和验证证据，返回 findings、证据和未覆盖项。实现入口为 `LocalReviewService`：与主 Agent 使用独立消息上下文及预算，且没有写工具、通用命令或递归委派权限。Reviewer 失败/`unavailable`/过期不是“无问题”；主 Agent 经 `submit_review_response` 对每个 finding 记录修复或不采纳理由。编码任务默认 `reviewer=default`，可 `off`；纯问答不强制。评测对照是「最小单 Agent」（关 Skill 注入、上下文优化与 Reviewer）与「完整方案」，不是与协议不同的旧 V1。`REVIEW_AGENT_CONTEXT_OPTIMIZATION=false` 时不做历史摘要与 changed-file snippet，仍保留原生 tool 配对与会话历史。

旧 review_pr 工具只留在兼容入口，M04 起不进入默认 coding 工具集，防止它通过旧 PR 验证流程绕过 Executor。新编码审查使用 M09 的本地只读 Reviewer。

## 官方参考

- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)：checkpointer 与长期存储职责不同。
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)：恢复会重新进入发生 interrupt 的节点，故审批前不得产生未保护的副作用。
- [Agent Skills 规范](https://agentskills.io/specification)：name/description、SKILL.md 与按需资源加载。
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)：当前 v2 主线及客户端/服务端接口，以实际锁定版本的文档为准。
- [MCP 客户端概念](https://modelcontextprotocol.io/docs/2026-07-28/learn/client-concepts)：工具发现和调用的协议边界。
- [Celery tasks](https://docs.celeryq.dev/en/stable/userguide/tasks.html)：任务确认、重投与幂等处理。

上述框架机制不替代本项目的文件版本核对、进程取消和业务状态约束。


### R1 审查目标增量契约（2026-09-21）

- `delegate_review` 保留原有 `run_id/subtask_id/workspace_revision/focus`；新增可选 `target: ReviewTarget`，默认 `run_changes`。`target.kind` 为 `run_changes/workspace_changes/paths`，`paths` 是规范化相对路径列表，`focus` 为审查重点，解析后的 `baseline/workspace_revision` 固定目标版本；调用方传入的基线或目标版本不匹配时拒绝执行。旧的外层 `workspace_revision` 仍指当前任务源码，原仓审查的目标版本放在 `target.workspace_revision`。
- `run_changes` 比较任务输入基线与当前源码，包括未跟踪文件；`workspace_changes` 比较登记原仓 HEAD 与准备时的允许输入树，含 staged/unstaged 的最终合并差异及未跟踪文本。续接 run 的开发基线来自父 run，原仓审查仍来自登记原仓。`paths` 直接读取所选当前源码，不伪造 diff。首版不提供 staged-only 审查。
- `WorkspaceManager` 在可信 `meta/review_input/` 保存原仓匹配源码，`meta/workspace_review.json` 保存原始 diff、基线、版本及未检查原因。复制前后及副本内容签名不一致则中止；diff 从副本内容与原仓不可变 commit 生成。原仓不 stage/commit/reset。旧 run 没有该元数据时 `workspace_changes` 明确不可用。
- 单文件超过 128,000 bytes、二进制/非 UTF-8、排除路径以及总 diff 超过 48,000 字符的部分记入 `unchecked`；不把截断部分列为完整审查。额外 hunk 上下文上限 12 个，序列化上下文/paths 内容各受 12,000 字符预算约束。Git 文件模式变化不在首版文本 diff 覆盖内。
- 复用键涵盖目标类型、基线、目标版本、路径、focus、需求、验收、验证结果、契约证据全文 hash、模型 profile 和 Reviewer 配置。工具主动委派及交付门禁使用同一计算入口。只有当前任务的完整默认目标可满足交付门禁；paths/原仓/不同 focus 结果不能冒充默认任务改动审查。
- 同一 `call_id` 的相同参数继续复用账本；更改该调用参数会返回 `invalid_arguments`。交付门禁调用 ID 包含复用键，恢复时从匹配账本恢复结果，不借用最新的其他范围投影。
- `ReviewResult` 与 `run.review` 增加 `target/cache_key/selected_paths/read_records`。`selected_paths` 为目标文件集合，`coverage` 仅列已提供 diff/源码/静态摘要的文件；`read_records` 区分 patch、源码行段和工具读取，不保证整文件语义覆盖。`unchecked` 保存未检查和工具失败原因。新轮次不继承其他目标的 finding 处理理由；事件仍保留原轮次。
- 子审查 usage 为各轮累加，父 run 同步累计。缺失和超时用量保持未知；已知 token、延迟和估算成本相加。`completed` 表示结构化审查完成，不等于无问题；无效空结果为 `unavailable`，预算/超时为 `interrupted`，版本过期为 `failed`。
- 本阶段没有新增数据表、任务模式、队列或前端入口；R2/R3 消费既有 JSONB review 投影、工具账本及委派事件。


### R2 任务模式增量契约（2026-09-22）

- `RunCreateRequest`、TaskService/TaskStore 创建参数、HTTP 客户端与 runtime 增加 `task_mode: develop|review|plan`，缺省 `develop`。`review_target` 使用 R1 的 `ReviewTarget`，仅 review 模式可指定；缺省 `workspace_changes`。模式与请求目标存入 `runs.task_mode/review_target`，更新/恢复/审批接口不提供修改入口。Alembic `0005_run_task_mode` 为旧 run 补 `develop`，新增类型约束；部署前执行迁移。
- 幂等比较包括需求、profile、验收、Reviewer 配置、模式及规范化目标；同 key 改模式/路径/focus 返回冲突。plan 的 Reviewer 固定关闭；review 不接受 `reviewer=off`。只读模式拒绝命令型验收，持久化验收为 `not_applicable`。
- TaskService 在创建新 run 时持有现有 worker 文件锁、准备输入副本，再派发；幂等重试复用副本。源文件采集变化或失败时 run 失败且不投递。底层直接使用 AgentRuntime 时仍在首次准备阶段固定副本。`workspace_snapshot` 保存 `source_run_id/source_revision/baseline_commit/prepared_at`，查询 API 公开该记录；已固定的副本丢失时拒绝从较新原仓重建。
- review/plan 使用现有 Reviewer 的内置只读工具注册表；执行前再核对允许工具及工具对象身份。只读模式不合并注入工具、不启动 MCP、不注入 Skill 工具/脚本；文本不能扩大权限。review 额外允许绑定当前 run 及冻结目标的 `delegate_review`，不开放 `submit_review_response`；plan 不委派。拒绝行为仍产生普通工具失败结果并保持原生消息配对。
- 同一 coding graph 内按模式完成：develop 沿用代码验收和 findings 处理；review 要求冻结目标的结构化 Reviewer 报告，findings 不阻止“审查完成”，unavailable/版本错误不转为成功；plan 的最终模型输出为 `PlanResult`（`files/steps/validation/uncertainties` 字符串列表，steps/validation 非空），不执行所列验证。两种只读模式均标记代码验收 `not_applicable`，完成前核对输入源码版本，报告写入会话和事件。
- 后续开发只继承当前 run 之前、已成功且通过代码验收的 develop run（保留原有 passed/not_applicable 规则）；不把只读 run 当新的代码基线。计划与报告留在带 run_id 和来源版本的会话历史里；用户明确请求开发时创建新 develop run，不扩大旧 checkpoint 权限。
- CLI：`--mode develop|review|plan`（别名 `--task-mode`），review 可加 `--review-target`、重复 `--review-path` 和 `--review-focus`。`/mode` 只影响下一个 run；当前有未结束任务时拒绝切换。现有 Web 请求缺省仍开发；R3 尚未新增 Web 模式选择与协作展示。
- 本阶段沿用既有执行器、队列和模型客户端，不新增子代理、服务或依赖。确定性测试已覆盖真实磁盘/Git/host 行为；PostgreSQL 实库迁移、跨连接持久化和真实模型仍待验证。


## R3 增量契约：客户端模式与协作展示

- Web 创建 run 提交已有 `task_mode` 与 `review_target`。新会话默认 develop；打开已有会话先读取最新/活动 run 的权威详情，恢复模式及范围。活动 run 只能补充需求，模式和范围不能编辑；下一轮显式选择 develop 才将只读会话切回开发。plan 请求 Reviewer off，review 请求 default。幂等重试比较模式及目标，改变范围不复用旧请求键。
- `run.review` 是最新报告；`delegate.started/completed` 与 `review.disposition` 事件按 `run_id + subtask_id` 展示历史轮次。SSE 按 run/seq 去重、续读，拒绝不属于订阅 run 的事件；delegate/disposition 触发权威详情刷新。任务/会话切换中止旧监视并丢弃迟到响应。
- 常显模式、Reviewer 待开始/执行中/完成/失败或未完成、最新验证。报告展示目标类型、规范化 paths、focus、baseline 和目标版本；`workspace_changes` 明确指原仓输入快照，不能当作 Agent 新改动已审。其他目标与当前版本不符时显示过期。验证 passed/failed 只有携带与当前源码一致的版本才按原状态展示；缺版本、旧版本不能声称当前通过。
- `selected_paths` 为选中目标；`coverage/read_records` 为实际提供给 Reviewer 的内容，不能推断全文件/全仓审查。`unchecked/warnings` 保留。有效 completed 空 findings 与 unavailable/failed/interrupted 区分。
- findings 按最新报告轮次展示位置/证据。`fixed` 文案为“Agent 标记已修复”，单独展示理由与验证状态；`not_adopted` 为“不采纳”。review 模式只报告问题，不要求自动修复。历史事件保留轮次、目标、版本、问题数、用量与对应处理理由；旧轮次不替代最新报告。
- 本轮 Reviewer usage 为模型各轮累计；历史按 subtask_id 合并防止重复累计，已加载轮次的 token 累计只在每轮该字段已知时相加，任一未知保留 null；不同估价口径不盲目合计费用，各轮记录单独展示。任务总用量已包含 Reviewer，不能把子任务再加一次。
- 只读 `run.succeeded` 的交付文本作为独立报告保留在聊天记录，不被之前的 `model.text_delta` 投影覆盖。报告范围采用可读文字；JSON API 字段未变。产物继续使用既有 HTTP 下载接口，不自动合并原仓。

本阶段没有数据库迁移、新 API 或新增依赖。Vite 配置用现有 Vite 的 UserConfig 与 Vitest InlineConfig 描述测试字段，移除仅为 setup 文件定位引入的 Node 类型依赖；完整 `tsc -p frontend --noEmit` 可运行，无需安装包。实际验证和限制见 R3 交接。


## R4 增量契约：共用预算与真实调用

- `REVIEW_AGENT_BUDGET_PATH` 为可选的已有 CNY 账本路径；默认不开启。启用后，主 Agent、Reviewer、native/legacy async 协议共用 `model_client._provider_stream` 的请求前预留。只接受已核价的官方 `deepseek-flash`，账本与 profile 价格必须一致；其他模型/端点拒绝发送。旧同步 PR 客户端在此配置下拒绝调用。
- 每请求先在文件锁下原子预留 0.5 元；累计限额取账本值与 10 元中较小者（2026-09-23 用户明确追加 5 元），包括全部历史记录。禁止隐式 SDK 重试；新请求/恢复/新客户端仍查同一账本。输出上限 4096，禁用 thinking，请求 JSON 不超过 100000 字节，并按文本/模板保守界限检查预留是否足够。此限制是本轮 DeepSeek 小额证据运行保护，不是通用计费服务。
- 正常结束且输入/输出 token 都已知时，按峰值 cache-miss 价格登记保守费用估算；错误、取消、缺失或非法 usage 保留整笔预留。账本无凭据或提示词，不等同供应商账单；历史未知请求不得改成零。账本丢失/无效即拒绝，不自动新建或重置。旧 RAG 程序没有同步改造，禁止与本轮脚本并发写账本。
- `apply_patch` 对 Begin Patch 格式先验证旧文本唯一匹配，再允许 Git 接受缺少周边上下文的局部 hunk；普通 unified diff 行为和补丁前后 hash/恢复校验不变。
- `run_attempt` 在事件循环关闭前释放它自己创建的异步模型客户端；外部注入客户端由调用方管理。
- Reviewer off 在运行时不开放委派/反馈工具，委派 handler 也检查开关；即使模型强行请求仍拒绝。review 任务保留其必需的 Reviewer。真实对照中发现旧实现只跳过最终 gate，没有阻止主动调用；修复后的新任务真实配对已完成（n=1，off 无委派）；历史无效配对保留。
- 每次主模型决策提供 active run_id 和现算 workspace_revision，明确 input snapshot 与修改后版本不同。默认交付审查为 run_changes，不以带其他 focus/目标的审查替代；版本/复用键检查不放宽。Reviewer 每轮知道剩余步数。Reviewer 的 git_status 返回选中目标文件清单并注明快照语义，避免无 `.git` 的原仓快照向上发现无关仓库；git_diff 仍返回已解析目标 diff。

实际记录及局限见 R4 交接 与 [R4 报告](../r4-evidence.md)。


### R4 补验：交付顺序和格式纠正（2026-09-23）

- develop + commands 的默认 run_changes 交付审查只有在当前源码版本的正式验收 passed 后才启动。过早工具请求返回 `verification_required` 并提示先 final，由既有 acceptance/review gate 继续；模型调用预算不因这次拒绝减少。指定 paths/focus 的专业审查保持独立，不冒充交付审查；复用键仍包含验收证据。
- Reviewer 报告仅要求有证据的可执行缺陷；风格偏好、假设版本和缺验收记录本身不作代码 bug。JSON 示例改为合法对象；格式失败最多纠正一次，仍使用同一 remaining steps/父 run 用量/金额账本。第二次失败或无剩余步数为 unavailable，不降级成 findings=[] 的成功报告。
- `review.format_error` 是既有事件流上的诊断事件，payload 含 subtask_id、retry 和至多 1000 字符 response_excerpt；不写入父会话模型历史，也不授予新权限。
- 新增 budget LIMIT=10 只是用户本轮授权的硬上限；原账本 cap 更低时仍按较低值执行。此次账本保留全部历史并另记 5→10 额度变更，未重置请求。

当前真实结果与限制见 [补验报告](../r4-followup-evidence.md)：四场景及一组有效配对完成；不外推稳定性/Reviewer 收益。R2-VDB 仍是独立环境补验项。

## 实库与交付补验增量（2026-09-24）

- 开发成功前在原有工作副本生成并登记 `delivery.patch`，完成事件发出时即可按 artifact ID 下载；不回写原仓，review/plan 不生成交付补丁。
- 工具账本沿用 `execution_status`：`pending` 为模型已提出但尚未开始，`awaiting_approval` 为等待审批，`executing` 在真正调用工具前持久化。`complete_tool_execution` 接受空 result 以仅更新执行元数据；不能把空结果视为完成。仅明确待执行、没有 started_at/进程句柄的记录可继续；旧未知状态及已开始但结果未知仍需核对。
- 自动 Reviewer 的确定性 call_id 使用 run ID、workspace_revision、完整 cache_key 的 JSON 序列 SHA-256，满足既有 64 字符数据库字段；审查复用仍使用完整目标/版本键。
- 运行时内部异常使当前 running run 转为 interrupted，记录不含异常详情的类型提示并释放本次租约；恢复继续经过既有副作用核对。
- 实际结果见 [完整链路报告](../fullstack-validation.md)。没有新增表、状态机、依赖或代理。
