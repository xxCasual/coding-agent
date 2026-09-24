# 模块实际进度

最后更新：2026-09-24。升级专用仓：`Code_review_Agent_upgrade`（无 remote）。上游对照基线：`0d9e602`。本仓 init：`4095af7`。

最新补验（2026-09-24）：**R2-VDB 已关闭**；专用 PostgreSQL 与 API/worker/CLI/Web 链路通过，实际接口开发及同会话规划完成。见 [完整链路报告](../fullstack-validation.md)。下文旧记录保留历史时点。

本轮仓库：物理独立本地仓；每模块本地 commit，默认不 push。详见 HANDOFF 版本控制。

收口提交：D02 `6277a92`；D01 `b4bbfea`；D03 `35a5f70`；交接 `44009d3`。V01 在 conda `review-agent`（Python 3.11.15）复跑 **60 passed**。V03 在拉取 `python:3.12-slim` 后 **1 passed**。

| 模块 | 状态（沿用交接记录） | 完成提交 | 实际交接记录 | 实现缺口／待验证 |
| --- | --- | --- | --- | --- |
| M01 | 完成 | `ba97ccc` + D02 `6277a92` | handoffs/M01.md | D02/V01 已关 |
| M02 | 完成 | `c611afd` + D02 `6277a92` | handoffs/M02.md | D02/V01 已关；V04 仍开放（不宣称双模型） |
| M03 | 完成 | `e0f0b7a` + D01 `b4bbfea` | handoffs/M03.md | D01/V01 已关；**V02** 精简真实评测 16/16（见 eval-report）；144 次与录屏仍开放 |
| M04 | 完成 | `c709722` + D03 `35a5f70` | handoffs/M04.md | D03/V01/V03 已关 |
| M05 | 完成 | `8d01f8b` | handoffs/M05.md | V01 已关；确定性 Skill 路径完成；V02 full 打开 Skill（精简 8 任务） |
| M06 | 完成 | `d622888` | handoffs/M06.md | V01 已关；stdio 已验证；V02 full 拉起 `api_contract` MCP；默认仍需 `REVIEW_AGENT_MCP_SERVERS` |
| M07 | 完成 | `06b83f0` + D01 `b4bbfea` | handoffs/M07.md | V01 已关；resume/审批调度已由 M08 接手 |
| M08 | 完成 | `7f7624a` + 交接 `5db5fc9` | handoffs/M08.md | 六类 fixture 已验；Celery 派发夹具见 M11；V02 精简已关；V04 仍开放 |
| M09 | 完成 | `d7f1500` + 交接 `429637b` | handoffs/M09.md | V02 full `reviewer=default` 已跑（精简）；Compose 库 `0003` 已由 M12 补齐 |
| M10 | 完成 | `0853b47` | handoffs/M10.md | 客户端 FakeModel HTTP 已验；Celery 真后台未运行；V02 精简评测走 eval runner 而非 Web |
| M11 | 完成 | `b9e8fcc` | handoffs/M11.md | FakeModel runner 已验；Celery/Redis 派发夹具已验；V02 精简 JSONL n=16；Compose 全栈现含 worker/beat |
| M12 | 完成（材料与部署复现；精简评测已跑） | `42ee47d` | handoffs/M12.md | 精简 JSONL n=16；24×3 与真模型录屏未做；V04 仍开放 |
| O01 | 未排期 | — | — | — |

## 桌面聊天 MVP（2026-09-10）

已实现，完成提交 `99ffa39`。固定桌面两栏、统一首次发送/续聊/追加、模型正文展示、审批与取消/恢复、折叠任务详情；修复会话切换、事件续读去重、发送幂等与产物迟到响应。仅前端，无新增依赖或后端接口变更。电脑端为本轮交付范围。

验证：前端 **15 passed**；前端 src 类型检查与生产构建通过；独立 Chrome 模拟数据桌面视觉检查通过，真实模型联调未运行。长历史分页、逐 token 输出仍受后端现状限制。详情及既有 Vite 配置类型检查限制见 M10 增量记录。

## 当前未解决问题

本表于 2026-09-09 收口后更新。已关闭项保留一行并写关闭提交，避免下游把旧差异当现状。

| 编号／性质 | 当前实际情况与来源 | 影响模块 | 原责任／关闭条件 |
| --- | --- | --- | --- |
| D01 已关闭 | 官方 `StateGraph` + `InMemorySaver` / `AsyncPostgresSaver.setup()`；CONFIRM 先写审批意图。M08 已接 `interrupt()` / `Command(resume=...)`。[当前入口](CONTRACTS.md#当前实现入口与已知差异) | 不得把 saver `setup()` 当恢复完成 | 关闭提交 `b4bbfea`；interrupt/resume 实现 `7f7624a` |
| D02 已关闭 | 共享边界为 frozen Pydantic（`extra=forbid`）；参数模型仍独立；JSONB 形状未改 | 消费者用 `model_dump` / `model_copy` | 关闭提交 `6277a92` |
| D03 已关闭 | 配置默认 `docker`；`REVIEW_AGENT_EXECUTOR_BACKEND=host` 为显式本机开发；pytest `conftest` 强制 host；无参 `CommandRunner()` 仍 host | M08 容器恢复用默认 docker | 关闭提交 `35a5f70` |
| V01 已关闭 | conda `review-agent` Python 3.11.15 安装项目 + pytest（未装 ruff）。复跑 60 passed（含 Postgres 用例） | 后续模块以该环境为准 | pydantic 2.13.5、pytest 9.1.1、langgraph 1.2.11、mcp 2.2.0、psycopg 3.3.5 |
| V02 精简正式评测已关闭 | 2026-09-10 精简 heldout 8 任务 × baseline+full = 16 次；隐藏验收 **16/16**。型号 `deepseek-v4-flash`，`--code-commit b08045b`，执行后端显式 `host`。full 打开 Skill/上下文优化/Reviewer 并拉起 MCP。数字见 [eval-report.md](../eval-report.md)。**未跑** 24×3=144、Reviewer 6×3 对照。full 相对 baseline 总 token **+1.18%**，不是 20% 节省。不要把 75% 目标写成 144 次成绩 | M03 闭环；M05/M06/M09 真模型路径；M11/M12 评测 | 关闭条件：精简 JSONL 与报告一致。剩余：144 次、三段录屏 |
| V03 已关闭 | `docker info` 成功；拉取 `python:3.12-slim` 后 `test_docker_executor_run_and_cancel` **passed** | M08 容器取消 | 在 review-agent 记录于 2026-09-09 |
| V04 模型能力待认证 | 第二个 `alt` profile 本轮不测 | 宣称双模型支持及 M12 材料 | 未知项不写已支持 |
| M08 Celery/Redis 派发夹具已关闭 | conda `review-agent` 已装 celery 5.6.3、redis 6.4.0。本机 Compose Redis ping 成功；`tests/test_celery_dispatch.py` 6 passed。M12 已走通 postgres/redis + API workspace 列表。Compose `docker compose up` 现会启动 worker/beat（与 conda 手动起 worker 不同）。V02 精简 JSONL 已跑；144 次与录屏分开记录 | 真后台 / 第三段演示 | 关闭提交 `b9e8fcc`；Compose 一键 Demo 与正式 JSONL 评测分开记录 |
| D04 耦合边界加固 | `AgentRuntime` 隔离工作区尊重 `tool_registry_factory`/注入工具与 memory；`TaskStore`/`ModelClient` Protocol 对齐实际调用；恢复按持久化 `replay_category`（Alembic `0004`）；`run_delegated_review` 共用审查入口；`build_task_service` + `run_attempt` + `run_lock`/`review_state`；前端 `useCodingSession` 拆分。实现已落地，提交 SHA 待本轮 commit。 | 加工具、接入存储、改恢复/审批 | 提交后填 SHA；V02 精简已另记，不外推 144 次 |

## 下一模块

主线材料已交：M12 交接。**V02 精简评测已写入** [eval-report.md](../eval-report.md)（n=16，隐藏验收 16/16；小样本）。仍开放：24×3 规模、三段真模型录屏、**V04**。可选增强见 [O01](modules/O01-optional.md)（未排期）。不把精简 16 次外推成 144 次成绩。

允许状态：待开始、进行中、实现完成待验证、完成、受阻。只有必需实现、验收与交接齐备才标记完成；缺口和未运行检查分开登记。历史测试结果仅证明当时提交与环境下的行为。

目标契约仍为 [CONTRACTS v1](CONTRACTS.md)。


## 求职成果核验（2026-09-18）

新增恢复边界实验脚本与 [证据说明](../resume-evidence.md)。专用 PostgreSQL + 真实磁盘 + SIGKILL 已验证补丁补记、再次恢复复用及未知副作用 needs_attention。它不是完整 Celery/真实模型故障恢复演示。

已有工作树定向检查：恢复/执行/评测 36 passed，前端 15 passed；服务与 Web 预检通过。本轮无新真实模型调用、无扩大评测、无录屏、无 push。旧公开仓库仍为 PR 版，发布当前升级版待处理。未改动生产接口。


## Coding Agent 增量改造方案（2026-09-21）

已整理 Coding Agent 改造方案 v1。**R1 已完成；R2 实现完成，数据库验证待补；R3 已完成；R4 已完成本轮预算内小样本验收**；不改变上面的历史实现与验证状态。

用户确认方向为主 Coding Agent + Reviewer 子代理，已取消本轮沙箱建设和简历修改。后续入口以该方案为准：R1 审查目标与复用 → R2 任务模式 → R3 协作展示 → R4 预算内真实场景和 Reviewer 单变量对照。沿用现有架构，不新建多代理平台。

真实模型累计预算 5 元，与此前 RAG 试跑共用账本；录屏可选，旧计划的 144 次评测与三段录屏不作为本版必做门槛。初始方案整理仅做文档核对；R1 实施证据见下方及交接，不改变旧真实评测口径。


### R1：审查目标与复用（2026-09-21）

完成：`ReviewTarget`、原仓 diff/匹配快照、任务未跟踪文件 diff、paths 源码审查、完整复用键及调用 ID 恢复约束、覆盖/未检查记录和累计 usage。只做现有链路增量，没有新增子任务服务、队列或依赖。

验证：conda `review-agent`，显式 `REVIEW_AGENT_DATABASE_URL=''` 使用现有内存检查器；Reviewer/执行/运行时/恢复/模型客户端定向检查 **56 passed，1 skipped**。磁盘、Git、host 进程为真实执行；模型为确定性 FakeModel。Docker 检查 skipped，PostgreSQL 集成及付费模型未运行；无依赖安装、容器启动或 push。

本地阶段提交：`bc5e82d`；起始工作树快照提交 `96a5233`；由于起始工作树包含未提交依赖，使用独立本地分支 `rework/r1-review-targets`，先保存起始快照，再提交仅 R1 差异；用户当前 HEAD 与原暂存区保留。详情见 R1 交接。下一阶段按方案进入 R2，本轮不启动。


### R2：开发 / 审查 / 规划模式（2026-09-22）

状态：**实现完成，数据库验证待补**。接口、两种存储、只读工具权限、完成语义、来源版本、CLI/HTTP 客户端已接入；创建 run 时固定输入副本，恢复复用。沿用单一运行时和工具链，无新增服务、队列或依赖。R2 阶段 Web 仍默认开发；模式与协作展示现由下方 R3 接入。

定向结果：**88 passed，8 skipped**（8 项 PostgreSQL 检查未配置实库）。覆盖模型主动请求写入/命令/Skill/扩展仍被拒绝，报告有 findings 可完成，规划不自动开发，幂等模式冲突，恢复/审批及 worker 重新装配，CLI 明确切换，排队源码固定及复制竞态失败。FakeModel 控制决策；磁盘、Git、host 进程真实；不作为真实模型任务成功证据。`0005_run_task_mode` 离线 SQL 已核对，实库迁移/容器/付费模型未运行。

阶段提交：`c2135a6af4d9b8e83f37cff8f2fef46dc84dfed3`，位于独立本地分支 `rework/r2-task-modes`（基于 R1 分支）。当前工作目录已更新，原 HEAD/暂存区及其他用户修改保留，未 push。详见 R2 交接。补验项 **R2-VDB**：在专用 PostgreSQL 测试库运行迁移及持久化检查；未完成前不宣称数据库认证。


### R3：协作展示与交付（2026-09-22）

状态：**完成**。在现有 React 运行台加入开发 / 只审查 / 只规划选择及审查范围输入；活动任务锁定模式，重新打开会话保留上一轮模式。常显 Reviewer 状态和验证结果；详情分别列出审查版本、目标范围、实际读取、未检查项、findings、Agent 处理理由与历史轮次用量。`fixed` 明示为 Agent 记录，`not_adopted` 显示不采纳；旧审查/验证不会冒充当前版本通过。CLI 同步展示范围、证据、处理理由和未知用量。

验证：前端 **24 passed**；完整 TypeScript 检查及生产构建通过；CLI/TaskService/运行时定向检查 **43 passed**（1 条既有 Starlette/AnyIO 弃用警告）。真实本地 HTTP/SSE + 内存 TaskService + 磁盘/Git 输入已在浏览器核对报告、范围、读取记录及模式恢复，模型为 FakeModel。未运行付费模型、PostgreSQL/Redis/Celery 全栈或 Docker；R2-VDB 仍待补。没有新增依赖、UI 框架、队列或通用子任务服务。

阶段提交：`6f90bb035302bd1b67619b11685438fdbbb427b4`，分支 `rework/r3-collaboration-ui`，承接 R2 最终记录 `9246db2`。原工作区 HEAD 与暂存区保留；构建产物已同步，未 push。详见 R3 交接。下一阶段为方案 R4；实际调用前仍须在共用模型调用边界落实原累计 5 元账本约束，不能拿本阶段确定性检查代替真实模型证据。


### R4 首轮：真实调用与预算保护（2026-09-23，补验前）

状态：**实现完成，真实验证部分完成**。共用原 5 元账本约束覆盖主 Agent/Reviewer/两种异步协议；修复当前版本传递、快照 status 越界、局部补丁应用和 Reviewer off 仍主动委派。没有新增代理、框架、服务或依赖。

真实模型完成过一次 FastAPI `/sum` 开发闭环及一次只审查 health 回归，原仓不变。续聊隐藏验收通过但 run 失败；后续开发复跑仍有审查耗尽。配对两侧隐藏验收通过，但 off 侧也运行了 Reviewer，**对照无效**；off 已本地修复，尚未真实重跑，不得声称 Reviewer 增益。只规划真实调用未运行。全部失败、有效/无效 findings 核对与限制见 [R4 报告](../r4-evidence.md)、R4 交接。

定向检查 **89 passed，1 Docker skipped**；off 修复后的直接消费者检查 **67 passed**。累计预算占用 **4.507638 元**（包含历史未知预留 1.50 元），余额 0.492362 元不足单次 0.50 元预留，已停止真实调用。不是供应商实际账单。原 12 条历史保留，无新账本。

开放项：**R4-REVIEW** 审查结束稳定性/非缺陷反馈与验收时机；**R4-PAIR** 修复开关后的有效配对；**R4-FOLLOWUP / PLAN** 真实续聊重验与规划场景；**R2-VDB** 保持开放。本轮不以新增能力掩盖这些问题。阶段实现提交 `13bc4a0ebbf1dd68d1717cef35400db4ddca6410`；本地阶段分支 `rework/r4-real-evidence`，保留当前 HEAD/暂存区，未 push。


### R4 补验完成（2026-09-23）

状态：**完成（本方案的预算内小样本范围）**。默认交付审查等待当前版本验收；Reviewer 报告约束仅列有依据的缺陷，非法 JSON 最多纠正一次，计入现有步数/用量。无新代理、依赖或服务。确定性检查 **84 passed**，1 条既有弃用警告；修复前的两个失败复现及全部真实失败记录保留。

真实模型：开发、只审查、同会话续聊、只规划 **4 个场景全部 succeeded**；开发/续聊隐藏验收通过，原仓与只读副本不变；新冻结 `/clamp` 的 Reviewer 单变量配对 **2/2 succeeded、隐藏验收 2/2**，off 无任何委派，on 完成且无 findings。只作 n=1 个案，不能宣称 Reviewer 普遍收益。[补验报告](../r4-followup-evidence.md) / [可提交摘要](../../eval/r4-followup/results.json)。

用户明确追加 5 元，原账本 cap=10 元；此次新增 0.899442 元、累计占用 **5.407080 元**（历史未知预留 1.50 元保留），余 4.592920 元；已停止付费调用。**R4-REVIEW / PAIR / FOLLOWUP / PLAN** 在上述范围关闭。**R2-VDB 仍开放**：只读连接预检为本机 PostgreSQL 不可用，未安装依赖或启动容器；没有全栈或第二模型认证。

补验完成提交 `6ffd38e001cd87c768f8b361799e8d78109b6ccc`。沿用本地分支 `rework/r4-real-evidence`，承接 `9e623f5`；原 HEAD/暂存区及无关文件保持不变，未 push。


### README 收口（2026-09-24）

README 已同步三种任务模式、审查范围、R3/R4 实际证据、预算保护及下一步顺序：先补专用 PostgreSQL 实库验证，再完成入口验收并用于实际小任务。仅文档更新，核对 CLI 参数、源码与现有报告；未调用模型、启动服务或重复测试。R2-VDB 等未验证项状态不变。

README 完成提交 `9845f6ab51c88210bd0d3d936132e57d905c5243`；沿用本地分支 `rework/r4-real-evidence`，原 HEAD/暂存区保持不变，未 push。

### R2 实库与完整链路收口（2026-09-24）

复用现有镜像与 conda，不安装依赖；专用测试库 **10 passed**，TaskService/runtime/恢复/Reviewer **75 passed**，Celery/Redis **6 passed**。真实进程验证排队、审批、重启恢复、运行中取消、SSE 续读及产物下载，CLI 与 Web 已核对。R2-VDB 关闭；不等同 Compose 镜像、prefork 或执行隔离认证。

修复成功开发缺少交付补丁、待审批及批次后续命令被误判未知副作用、Reviewer ID 超长入库失败、worker 异常残留 running/租约。新增回归均有修复前失败，未知副作用阻断保持。详细事实与失败保留在 [报告](../fullstack-validation.md) / [摘要](../../eval/fullstack-20260924/results.json)。

真实 `/greet` 开发经修复恢复后 succeeded，8 项测试及 9 条独立断言通过，Reviewer completed / 无 findings；同会话只规划 succeeded / not_applicable，原仓及规划副本未改。19 次新增模型请求，共用新增占用 0.278620 元，累计 **5.685700 / 10 元**，余 **4.314300 元**；含历史未知预留 1.50 元，非实际账单。付费任务已停止。后续进入实际小任务使用，按阻塞修复，不扩功能。

本轮完成提交 `36b8d49bb21804efeb716591b325205b89ce5c39`；本地分支 `rework/r4-real-evidence`，原 HEAD/暂存区与无关改动保留，未 push。验证进程与本次独立容器已停止，证据保留。
