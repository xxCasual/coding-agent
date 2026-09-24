# 实库与完整链路补验（2026-09-24）

R2-VDB 已补齐。PostgreSQL 迁移、持久化及 API → Redis → Celery worker → PostgreSQL checkpoint → CLI/Web 链路已在本机验证。按验收发现的问题修复，没有增加代理、框架或依赖。小型 FastAPI 开发和同会话规划已完成；开发经历过修复后恢复，不能描述为首次运行无故障。

## 环境与边界

复用已安装 Docker Desktop 和已有 `postgres:16` / `redis:7` 镜像，创建本次独立容器与专用数据库；未触及原业务库。API 与 worker 使用既有 conda `review-agent`（Python 3.11.15），运行于不同进程；Celery 使用 solo，项目命令使用 host。不是 Compose 应用镜像、prefork、多机或 Docker 执行隔离认证。验证后停止本次 API、worker 和数据库容器，保留证据。

## 发现与修复

1. 成功开发未自动产生 `delivery.patch`，旧产物测试手工放入文件掩盖了遗漏。现在开发验收与审查完成后，先生成、登记补丁，再发出 `run.succeeded`；规划不产生补丁，原仓不回写。回归同时检查完成事件到达时 API 已可下载。
2. 审批恢复把尚未执行的普通命令（含同批后续命令）误判成未知副作用。沿用工具账本的 `execution_status`，明确记录 `pending` / `awaiting_approval`，执行前持久化 `executing`。仅有明确待执行标记且无进程句柄/开始时间时允许继续；旧未知记录、已执行但无结果的命令仍需人工核对。
3. 自动 Reviewer 的调用 ID 拼接 run、版本与缓存键，超过 PostgreSQL `VARCHAR(64)`。改为三者的 SHA-256 确定性 ID；不扩大数据库字段，也不削弱复用范围。
4. worker 内部异常可能遗留 `running` 状态及租约。现在记录 `interrupted` 并释放本次租约；恢复仍先核对副作用，不直接重放未知命令。

以上均有修复前失败与修复后通过的回归。真实开发第一次因待执行批次恢复受阻而结束；第二次在 Reviewer ID 入库失败后停滞。确认旧 worker 停止后，显式清理该旧租约并标记中断，再通过 API 恢复同一 PostgreSQL checkpoint，完成 Reviewer 与交付。保留这些失败，不将人工恢复说成原版本自动恢复。

## 验证结果

| 范围 | 结果 |
| --- | --- |
| TaskService、runtime、恢复、Reviewer | **75 passed**，1 条既有 Starlette/AnyIO 弃用警告 |
| 专用 PostgreSQL | **10 passed**：从 0004 旧数据升级 0005、默认 develop、模式约束、跨连接、幂等竞争、租约、Reviewer ID 与工具执行状态；3 条 Alembic 配置弃用警告 |
| Celery / Redis | **6 passed**，使用独立 Redis 测试 DB |
| 实际多进程，无付费模型 | 无 worker 时排队；Web 审批；重启 worker 后继续补丁；真实睡眠命令启动后取消；注入模型超时、重启后恢复规划；SSE 从中间序号续读；产物下载 SHA-256 一致 |
| CLI / Web | CLI 经真实 HTTP 提交 plan 并显示 succeeded；浏览器核对活动模式不可改、审批、规划恢复、真实开发与规划会话 |
| 实际模型开发 | `/greet` 支持默认称呼、去首尾空白、Unicode、空白/超长 422；8 项项目测试通过，外部 9 条独立验收断言通过，Reviewer completed / findings=[]，补丁可下载 |
| 同会话只规划 | 使用成功开发版本，succeeded / not_applicable；仅只读工具，源码不变，拟议验证未运行 |

定向测试命令：

```bash
PYTHONPATH=src REVIEW_AGENT_DATABASE_URL='' REVIEW_AGENT_CELERY_BROKER='' \
  conda run -n review-agent python -m pytest -q \
  tests/test_task_service.py tests/test_coding_runtime.py tests/test_recovery.py tests/test_local_reviewer.py

# 必须使用专用测试库：fixture 会删除/重建业务表。
PYTHONPATH=src REVIEW_AGENT_DATABASE_URL="$TEST_DATABASE_URL" \
  conda run -n review-agent python -m pytest -q tests/test_postgres_task_store.py

PYTHONPATH=src REVIEW_AGENT_DATABASE_URL='' REVIEW_AGENT_CELERY_BROKER="$TEST_REDIS_URL" \
  conda run -n review-agent python -m pytest -q tests/test_celery_dispatch.py
```

初次把 Redis 地址同时传给“无 broker”测试导致其预期不成立；分开运行后通过。初次真实任务启动器没有把 conda 放在 PATH 首位，也已修正启动配置；没有安装依赖。

## 费用与证据

本轮 **19 次**付费请求，新增预算占用 **0.278620 元**，共用累计 **5.685700 / 10 元**，余 **4.314300 元**。原 221 条账本记录保持不变，没有新增未知用量。累计仍含历史未知预留 1.50 元；按[官方中文价格页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/?article_id=article_1779470751466_8)的 Flash 峰值输入 2 / 输出 8 元每百万 token 保守估算，不是实际账单。完成后停止新增付费任务。

可提交的 [结果摘要](../eval/fullstack-20260924/results.json) 含 run ID、事件序号、验证、预算与源码摘要；[交付补丁](../eval/fullstack-20260924/delivery.patch) 可查看。原始进程日志、启动器、复验脚本及账本快照保存在本机 `.git/fullstack-before/`（不进入版本库）。真实规划曾尝试旧绝对路径，被权限检查拒绝后改用相对路径；保留此现象，不宣称模型规划总是简洁或无误。

下一步直接用于有明确验收标准的小任务，按真实阻塞修复。大规模评测、第二模型、额外代理与沙箱建设不在本次范围内。
