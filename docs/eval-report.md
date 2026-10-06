# 评测报告

**实际正式样本：n = 16（精简口径）。** 2026-09-10 按 heldout 中 8 个任务、baseline + full 各一次跑完；隐藏验收 **16/16**。这是小样本，**不是** 24×3=144 的默认正式规模，禁止外推或把 75% 完成率、约 20% token 节省写成项目成绩。

冻结实现记录：`--code-commit b08045b`（`docs: record desktop chat MVP completion`）。评测进程工作树另有未提交的 D04 耦合加固改动。`b08045b` 属于本地开发历史，公开仓库是之后整理的单次提交；公开的 manifest、隐藏验收脚本和三个样例与该版本逐文件一致。Runner 与任务集见 [eval/README.md](../eval/README.md)。原始 JSONL 已原样公开在 [eval/v02-20260910/](../eval/v02-20260910/)，附复算命令。

## 已执行的命令

以下为历史评测当时的原样命令（conda 环境 `review-agent`）；当前推荐的 uv 安装与检查方式见 [README](../README.md#检查)。

最小单 Agent（关 Skill / 上下文优化 / Reviewer）：

```bash
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export PYTHONPATH=src
export REVIEW_AGENT_EXECUTOR_BACKEND=host
PYTHONPATH=src conda run --no-capture-output -n review-agent review-agent eval \
  --manifest eval/manifests/heldout.json \
  --variant baseline \
  --out eval/results \
  --code-commit b08045b \
  --task-id hold-fs-fix-health \
  --task-id hold-fs-fix-unique-ids \
  --task-id hold-llm-fix-invalid-code \
  --task-id hold-pb-fix-list-scope \
  --task-id hold-fs-feat-patch-item \
  --task-id hold-llm-feat-failover \
  --task-id hold-pb-feat-search \
  --task-id hold-fs-contract-get-item
```

完整方案（Skill + 上下文优化 + `reviewer=default` + MCP `api_contract`）：

```bash
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export PYTHONPATH=src
export REVIEW_AGENT_EXECUTOR_BACKEND=host
export REVIEW_AGENT_CONTRACT_ROOT="$(pwd)/examples/contracts"
export REVIEW_AGENT_MCP_SERVERS='[{"server_id":"api_contract","command":["<conda-python>","-m","review_agent.mcp_servers.api_contract"],"env_allowlist":["PYTHONPATH","REVIEW_AGENT_CONTRACT_ROOT"],"timeout_seconds":30}]'
PYTHONPATH=src conda run --no-capture-output -n review-agent review-agent eval \
  --manifest eval/manifests/heldout.json \
  --variant full \
  --out eval/results \
  --code-commit b08045b \
  --task-id hold-fs-fix-health \
  --task-id hold-fs-fix-unique-ids \
  --task-id hold-llm-fix-invalid-code \
  --task-id hold-pb-fix-list-scope \
  --task-id hold-fs-feat-patch-item \
  --task-id hold-llm-feat-failover \
  --task-id hold-pb-feat-search \
  --task-id hold-fs-contract-get-item
```

`eval/results/` 已 gitignore。本轮写入 `baseline.jsonl` / `full.jsonl` 及对应 `*.summary.json`。

Runner 以写模式覆盖 `{variant}.jsonl`，**没有** `--repeats`。若日后跑 24×3，应对每次重复使用不同 `--out` 子目录或扩展 runner，再合并；不能把不同配置无标记拼在一起。

精简 8 任务覆盖三份样例、4 修复 / 3 功能 / 1 契约，并含 4 个 `reviewer_hard=true`：`hold-fs-feat-patch-item`、`hold-llm-feat-failover`、`hold-pb-feat-search`、`hold-fs-contract-get-item`。

本机 `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` 指向 `127.0.0.1:54658` 且该端口未监听。评测前已 `unset`，否则会 `Connection error`。不要把这类环境失败写成实现失败。

`.env` 中 `REVIEW_AGENT_EXECUTOR_BACKEND=docker`，但评测显式设为 `host`：默认 Docker 网络为 `none`，`python:3.12-slim` 无 FastAPI/pytest，隐藏验收与 Agent 命令共用 conda 环境。这是本轮评测环境选择，不是把 host 改成产品默认。

## 规模口径

| 方案 | 任务 | 重复 | 组 | 次数 | 本轮 |
| --- | --- | --- | --- | --- | --- |
| 默认正式 | heldout 24 | 3 | baseline + full | 144 | **未跑** |
| 精简 | heldout 中 8 个 | 1 | baseline + full | 16 | **已跑**（2026-09-10） |
| Reviewer 对照 | `reviewer_hard=true` 的 6 个 | 各 3（仅当模型/开关/预算与主实验相同） | 探索性 | 最多 36 | **未跑**独立对照；精简 8 个里含 4 个 hard，不得声称统计显著 |

heldout `reviewer_hard` 任务：`hold-fs-feat-patch-item`、`hold-fs-contract-v2-priority`、`hold-fs-contract-get-item`、`hold-llm-feat-failover`、`hold-llm-contract-fixture`、`hold-pb-feat-search`。

开发集 8 个（`eval/manifests/dev.json`）不得用于冻结调参。M11 看过结果的仅 FakeModel 夹具 `demo-fix` / `demo-impl` / `demo-env`（测试临时树，不是 manifest 条目）。

## 实际结果

计分只认隐藏验收退出码，不依赖模型 final 文本，也不把 `run_status=succeeded` 当成完成。完成率分母含平台失败；本轮无平台失败。

| 指标 | 值 | 来源 |
| --- | --- | --- |
| 正式尝试次数 | 16 | `eval/results/baseline.jsonl` + `full.jsonl` |
| 隐藏验收成功 | **16/16**（baseline 8/8，full 8/8） | `outcome=success` 且 `hidden_passed=true` |
| 失败分布（按 `outcome`） | 无 | 16 行均为 `success` |
| Agent `run_status`（非计分） | baseline：succeeded 5 / failed 3；full：succeeded 5 / failed 1 / interrupted 2 | 同 JSONL；`failed`/`interrupted` 仍可能隐藏验收通过 |
| token | 输入 **4,390,502**；输出 **179,690**；16 行均有用量 | `usage.input_tokens` / `output_tokens` |
| 费用 | 未知 | `estimated_cost` 16 行均为 `null`，**不记零** |
| 端到端延迟 | P50 **65.8 s**；P95 **159.4 s** | 16 行 `elapsed_ms` |
| 模型延迟 | P50 **59.3 s**；P95 **157.4 s** | 16 行 `model_latency_ms` |
| 墙钟 | baseline 约 9 min 50 s（14:05:06Z–14:14:56Z）；full 约 9 min 21 s（14:15:09Z–14:24:30Z） | 评测进程日志 |
| 模型 / profile | `deepseek-v4-flash` / `deepseek` | `.env` `DEEPSEEK_MODEL`；JSONL `profile_id` |
| 执行后端 | `host`（显式） | 评测环境变量 |
| 并发 | 1；eval runner 顺序调用 `TaskService.run_once`；InMemoryTaskStore；无 Celery | runner 实现 |

full 相对 baseline（同一 8 个任务、各一次）：总 token 2,271,692 → 2,298,500（**+1.18%**），不是节省。不得把「约 20% token 节省」写成实测。full 打开了 Skill、上下文优化、Reviewer，并拉起 MCP；token 对比不能单独归因某一开关。

75% 完成率、约 20% token 节省是原文待验证目标，**不是**本项目在 144 次规模上的成绩。本轮精简隐藏验收 16/16 不能外推到其余 16 个 heldout 任务或 3 次重复。

### 任务级（隐藏验收均通过）

| task_id | 类别 | 样例 | baseline `run_status` / steps | full `run_status` / steps |
| --- | --- | --- | --- | --- |
| hold-fs-fix-health | fix | fastapi-service | succeeded / 14 | succeeded / 23 |
| hold-fs-fix-unique-ids | fix | fastapi-service | failed / 24 | succeeded / 19 |
| hold-llm-fix-invalid-code | fix | llm-adapter | succeeded / 16 | succeeded / 16 |
| hold-pb-fix-list-scope | fix | python-backend | failed / 24 | succeeded / 13 |
| hold-fs-feat-patch-item | feature（hard） | fastapi-service | succeeded / 9 | interrupted / 16 |
| hold-llm-feat-failover | feature（hard） | llm-adapter | failed / 24 | failed / 24 |
| hold-pb-feat-search | feature（hard） | python-backend | succeeded / 24 | interrupted / 24 |
| hold-fs-contract-get-item | contract（hard） | fastapi-service | succeeded / 14 | succeeded / 14 |

full 日志中 MCP `get_endpoint_contract` / `validate_response_sample` 对错误 `contract_id` 或未知 path 返回工具错误（例如 `health`、`notes`、`/items/{id}` 不在 `items-v1`/`items-v2`）。这是模型探测，不是平台失败；隐藏验收仍通过。

冒烟（不计入 n=16）：同日 `hold-fs-fix-health` baseline 另写入 `eval/results/v02-smoke/`，约 27 s，`outcome=success`。

## 确定性证据（不是正式评测）

| 行为 | 命令 | 结果 |
| --- | --- | --- |
| eval runner 成功/实现失败/环境失败；隐藏脚本不进副本 | `PYTHONPATH=src conda run -n review-agent python -m pytest -q tests/test_eval_runner.py` | **8 passed**（FakeModel；2026-09-10 复跑） |
| Celery/Redis 派发夹具 | `tests/test_celery_dispatch.py` | **6 passed**（M11 `b9e8fcc`；本机 Redis） |
| 六类恢复 fixture | `tests/test_recovery.py` | M08 `7f7624a`；同版本不重跑全量 |

未跑：heldout 其余 16 个任务、3 次重复、Reviewer 6 任务×3 对照、第二 `alt` profile（V04）、三段真模型录屏。
