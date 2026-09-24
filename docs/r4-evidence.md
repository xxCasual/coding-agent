# R4 首轮真实使用证据（2026-09-23）

> 本页保留首轮失败及无效对照。用户随后追加预算，补验已完成；当前结论见 [R4 补验完成记录](r4-followup-evidence.md)。

**部分完成，不能宣称主流程稳定或 Reviewer 有效提升。** 真实模型完成过一次 FastAPI 开发闭环和一次只审查任务；续聊、后续复跑和对照出现失败。Reviewer off 实测未关闭主动委派，因此本次配对无效；开关已本地修复并通过定向检查，但预算不足以重跑。只规划真实场景未运行。

## 环境与冻结口径

复用 `TaskService + InMemoryTaskStore + WorkspaceManager`，conda `review-agent`，显式 host 后端，真实 Git/磁盘/pytest/FastAPI TestClient。未走 PostgreSQL/Redis/Celery/Web 全栈，不是容器隔离证据。Skills 和上下文优化开启，memory/MCP 关闭；主/子代理共用 DeepSeek 官方 `deepseek-flash`、native tools、thinking disabled、max_tokens=4096、SDK retries=0。输出目录禁止复用。

开发任务与对照任务分别为 `/sum`、`/normalize`；[输入与隐藏验收](../eval/r4/tasks.json) 在首次调用前固定，隐藏验收从 Agent 工作副本之外执行，未加入模型提示词。初始源码只有 health 接口及测试，源码版本 `b239e4b1d112bae31302cd1d`。最初开发试跑为 16/4 步；第 5 次开发与配对恢复项目默认的主循环 24 步、Reviewer 6 步。每次 manifest 保存当次配置与源码 hash；调试题未移入正式对照。

价格于 2026-09-22 核对：按峰值 cache-miss 输入 2 元/百万 token、输出 8 元/百万 token 保守登记，未减去缓存/错峰优惠，因此**不是实际账单**。[DeepSeek 官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/?article_id=article_1779470751466_8)、[请求参数](https://api-docs.deepseek.com/api/create-chat-completion/)。历史 `deepseek-v4-flash` 名称没有用于新调用。

## 全部尝试

耗时含 run 执行和外部隐藏验收；费用为整个 run（含 Reviewer）的峰值估算。表内“隐藏通过”与 run 成功分开；多次尝试同一开发题，不当成独立完成率样本。

| 尝试 | 场景 / Reviewer | run 状态 | 隐藏验收 | 秒 | 输入 / 输出 token | 元 |
| --- | --- | --- | --- | ---: | ---: | ---: |
| development | /sum，第 1 次 | interrupted | 通过 | 24.10 | 139026 / 3967 | 0.309788 |
| development-02 | /sum，第 2 次 | interrupted | 通过 | 22.40 | 119729 / 3332 | 0.266114 |
| development-03 | /sum，第 3 次 | **succeeded** | 通过 | 15.53 | 82623 / 1873 | 0.180230 |
| development-03 | 原仓 health 改动只审查 | **succeeded** | 不适用 | 9.14 | 24448 / 1558 | 0.061360 |
| development-03 | 同会话追加 maximum | failed | 通过 | 23.67 | 199519 / 3591 | 0.427766 |
| development-04 | /sum，第 4 次 | interrupted | 通过 | 15.15 | 75856 / 2112 | 0.168608 |
| development-05 | /sum，第 5 次 | interrupted | 通过 | 32.99 | 171787 / 5380 | 0.386614 |
| pair / reviewer-on | /normalize，default | failed | 通过 | 43.54 | 299625 / 5813 | 0.645754 |
| pair / reviewer-off | /normalize，off（实际仍调用 Reviewer） | failed | 通过 | 38.93 | 234694 / 5941 | 0.516916 |

完整 run_id、session_id、输入版本、逐次结果和证据 SHA256 见 [可提交摘要](../eval/r4/results.json)。原始 [开发记录](../eval/results/r4-20260922-development-03/results.json)、[配对记录](../eval/results/r4-20260922-pair/results.json) 保存在本机 gitignored 目录；摘要保留历史路径与 hash，未丢弃失败。

成功开发 run 为 `b480e4f2-5369-4853-b45b-cea50ea21eb5`：当前版本 `eda2533a67f454133a4bb010` 验证 passed，Reviewer completed、findings 为空，原仓未变化；[交付 patch](../eval/results/r4-20260922-development-03/development/data/workspaces/development/runs/b480e4f2-5369-4853-b45b-cea50ea21eb5/artifacts/delivery.patch) 由既有 WorkspaceManager 导出，未手工补写实现。早期运行未在 driver 自动导出 patch，收口时从保留的工作副本统一导出并记录 hash；不是模型额外完成的操作。

只审查 run 为 `f76531c7-c911-4d95-a442-66eb394d1a5a`：输入预先将 health 的 `ok` 改成 `broken`，Reviewer 在 app.py:7–8 报告 1 个有效回归，报告完成，原仓和只读工作副本哈希均不变。它是明确构造的独立审查输入，不是主开发流程的“发现并修复”演示。

续聊确实沿用同一 session，输入 `source_run_id` 为上述成功开发 run；Agent 已实现 maximum，隐藏验收通过，但反复应用局部补丁失败后耗尽 16 步，未完成当前版本的正式验收/审查。不能写作续聊完整成功。补丁问题已本地修复；真实续聊重验待补。

## 配对有效性与 findings 核对

配对两侧初始源码、任务、模型、配置和预算规则相同，拟仅切换 Reviewer。然而 off 一侧仍主动调用了 2 轮 Reviewer，开关没有真正隔离变量；末尾又被累计金额保护终止。**这不是有效 Reviewer 消融，不计算成功率差、token 节省或增益。** 最后代码修复发生在该对照之后，配对 manifest 中的旧源码 hash 保留；没有把修复后的代码冒充已实测版本。

on 一侧共 3 轮、3 条 finding：未确认实际功能缺陷；一条要求显式 StrictStr，Agent 记录 fixed；一条报告自行承认“不是缺陷”，Agent 记录 not_adopted；最后一条声称字符串 `abc` 被列表字段转换为字符列表，尚未处理即耗尽主循环。真实运行环境为 Pydantic 2.13.5，后置 HTTP 核对 `abc`、字典以及多类非字符串输入均为 422，反驳最后一条的可复现断言。显式 StrictStr 的建议可以提高表达清晰度，但在本环境 JSON 请求中未确认违规输入通过，因此不计有效 bug。

off 一侧实际产生 7 条 finding，包含无根据的 Pydantic 版本疑虑、风格/测试建议，以及 Reviewer 未拿到正式验收记录的流程提示；未确认新增功能缺陷，没有正式 disposition 记录。不能将这些数目称为“发现 7 个 bug”。本地核对结果及源码 hash 在 [adjudication.json](../eval/results/r4-20260922-pair/adjudication.json)，同时嵌入可提交摘要。核对没有修改被测源码，也没有追加付费调用。

## 本轮修复与仍开放问题

已修复并本地验证：

- 每轮给主模型当前 run_id/源码版本，避免修改后仍用初始快照去委派；保留过期拒绝。
- 明确 run_changes / workspace_changes；Reviewer 知道剩余步数；快照的 git_status 只列所选目标，避免向上读取无关仓库。
- Begin Patch 在旧文本唯一匹配后允许无周边上下文的局部 hunk；歧义仍拒绝，hash 和恢复校验保留。
- Reviewer off 不注册委派/反馈工具；残留 handler 也拒绝 off 调用；上下文明确关闭。恶意/错误模型仍请求委派时不会启动 Reviewer。
- 共用账本保护及 attempt 自建客户端释放。

仍开放：

- **R4-REVIEW**：Reviewer 会把风格或缺少验收证据写成 finding；验收 gate 更新证据后，旧 review 的复用键失效，可能需要再次审查而耗尽累计 Reviewer 步数。没有放宽当前证据检查或把 interrupted 改成 succeeded。需要在后续预算内解决审查时机与非缺陷反馈，不能继续宣称稳定闭环。
- **R4-PAIR**：off 修复后的真正单变量重跑；目前没有有效配对结论。
- **R4-FOLLOWUP / PLAN**：补丁修复后的续聊闭环，以及只规划真实调用尚待补验。只读/规划已有确定性检查，不能替代真实模型证据。
- **R2-VDB** 等历史缺口不变；未运行数据库迁移/持久化、容器或第二模型认证。

最终修改相关检查：89 passed、1 Docker skipped；off 修复后再检查直接消费者，67 passed。均为 conda `review-agent`；1 条既有 Starlette/AnyIO 弃用警告。未重新跑无关全量测试，未安装依赖、未做沙箱建设、未 push。

## 预算收口

沿用原 `真实调用_5元预算/budget.json`；原 12 条 RAG 历史完全不变。本轮 155 次实际请求，已知用量峰值估算 **2.963150 元**；累计占用 **4.507638 / 5 元**，其中历史未知请求保留 **1.50 元**。余额 **0.492362 元** 小于每请求必要预留 **0.50 元**，最后请求在发送前被拒绝。没有重置账本，也没有为凑齐场景继续调用。
