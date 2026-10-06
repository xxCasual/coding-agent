# V02 精简评测原始结果（2026-09-10）

本目录是 [评测报告](../../docs/eval-report.md) 中 n=16 的原始逐条记录，从当时 gitignored 的 `eval/results/{baseline,full}.jsonl` 原样复制，未改动内容。

| 项 | 记录 |
| --- | --- |
| 任务 | [heldout manifest](../manifests/heldout.json) 中 8 个任务，各组各跑 1 次 |
| 配置 | baseline：Skill、上下文优化、Reviewer 均关闭；full：三者打开，并通过 `REVIEW_AGENT_MCP_SERVERS` 拉起 `api_contract` MCP |
| 计分 | 只认隐藏验收（`eval/hidden/<task_id>/`）退出码，即 `outcome=success` 且 `hidden_passed=true`；不看模型 final 文本，也不把 `run_status=succeeded` 当成完成 |
| 结果 | 隐藏验收 16/16（baseline 8/8，full 8/8）；`run_status`：baseline succeeded 5 / failed 3，full succeeded 5 / failed 1 / interrupted 2 |
| 模型 | `deepseek-v4-flash`（profile `deepseek`）；当天 API 别名指向的具体版本未记录 |
| 代码版本 | JSONL 中 `code_commit=b08045b`，属于本地开发历史，公开仓库是之后整理的单次提交，不含该 SHA；评测进程工作树另有未提交的 D04 改动 |
| 与公开仓库的对应 | 公开仓库中的 `eval/manifests/`、`eval/hidden/`、`eval/samples/` 下三个样例与 `b08045b` 逐文件一致；`src/review_agent/eval/` 的 runner 之后在 R4 有改动 |
| 重试与人工介入 | runner 无重试参数；报告与状态记录中没有重跑或人工介入。runner 以覆盖方式写文件，文件本身不能排除更早被覆盖的尝试 |
| 费用 | 16 行 `estimated_cost` 均为 `null`，记为未知；预算预留与记账是之后 R4 才接入的 |

## 状态与验收为什么不同

隐藏验收检查的是 Agent 隔离副本中最终代码产物；`run_status` 是 Agent 运行本身的结束状态。4 次 `failed` 的 `step_count` 都等于上限 24，应是步数用尽，但 JSONL 未记录失败原因；2 次 `interrupted` 的原因同样未记录。这些运行在结束前已写入的代码仍通过了隐藏验收。所以 16/16 只能表述为“代码产物通过隐藏验收”，不能表述为“16 次运行均无故障完成”。

baseline 与 full 同时切换了多个因素，这组结果只是配置对比，不能归因到 Reviewer 或任何单一开关；样本为 8 个任务各 1 次，不外推到其余 heldout 任务。

## 复算

```bash
python3 - <<'EOF'
import json
from collections import Counter
rows = [json.loads(l) for v in ("baseline", "full") for l in open(f"eval/v02-20260910/{v}.jsonl") if l.strip()]
print(len(rows), Counter((r["variant"], r["outcome"] == "success" and r["hidden_passed"]) for r in rows))
print(Counter((r["variant"], r["run_status"]) for r in rows))
print("input", sum(r["usage"]["input_tokens"] for r in rows), "output", sum(r["usage"]["output_tokens"] for r in rows))
EOF
```

2026-10-06 复算输出：16 行，baseline/full 各 8 行通过；`run_status` 计数与上表一致；输入 4,390,502、输出 179,690 token，与报告一致。这是对保存结果的复算，不是重新调用模型复现。
