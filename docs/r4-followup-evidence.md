# R4 补验完成记录（2026-09-23）

**R4 本轮约定的小样本验收已补齐。** 开发、只审查、同会话续聊、只规划均成功完成；新冻结的 Reviewer 开关配对两侧成功且隐藏验收通过，off 侧没有调用 Reviewer。一次配对只证明该案例，不代表普遍收益或生产稳定性。[此前失败与无效对照](r4-evidence.md) 全部保留。

用户本轮明确追加 5 元，原账本累计上限由 5 元提高到 **10 元**。此次实际新增预算占用 **0.899442 元**，累计 **5.407080 元**，余额 **4.592920 元**；没有用完预算才停止的要求，场景完成后已停止付费调用。累计中仍包含历史未知预留 1.50 元；这些数值是保守预算估算，不是实际账单。

## 局部修复及验证

默认交付审查在 commands 模式下等待当前版本的正式验收 passed。模型过早调用会得到 `verification_required`，提示返回 final 交由现有运行时完成验收与 Reviewer；不会提前消耗 Reviewer 模型额度。独立的指定范围/重点审查仍可使用。保留完整复用键、当前版本检查和 findings 处理要求，不把旧报告直接当当前通过。

最小回归使用真实 Git/磁盘/验收命令和仅 1 步 Reviewer 配额：修复前在相同代码版本重复审查并 interrupted，修复后先验收再审查并 succeeded。原轨迹也确认代码版本没变、验收记录变化导致 key 改变；不是增大步数掩盖重复审查。

Reviewer 提示改用合法 JSON 示例，明确只报告有具体条件、错误行为和证据的缺陷，排除风格偏好与假设的依赖版本。第一次真实复跑因非法 JSON 中断，记录未删除；随后在现有循环内加入**最多一次格式纠正**，占用原 Reviewer 步数、父 run 用量和同一金额预算。第二次仍非法或无剩余步数则 unavailable。`review.format_error` 事件保存原因、是否纠正及最多 1000 字符响应摘录，不把非法响应视为干净审查。

预算保护保留请求前 0.5 元预留、原子写入/文件锁、失败保留额度、SDK 零重试、输出 4096 token 和请求大小限制；代码硬上限改为 10 元，实际限额仍取代码与账本的较小值。原 167 条请求没有修改，账本另记额度变更原因。

定向检查 **84 passed**，1 条既有 Starlette/AnyIO 弃用警告：模型客户端、Reviewer、coding runtime、TaskService、恢复。新增检查覆盖当前验收前不花费交付审查配额、一次格式纠正计入原用量/步数、无剩余配额不纠正、账本写更高额度仍不能突破授权 10 元。两个新增问题均有修复前失败记录；没有重复无关全量检查。

## 全部真实调用记录

与首轮相同：`deepseek-flash`、native tools、thinking disabled、max_tokens=4096、SDK retries=0；host、InMemoryTaskStore、真实 Git/pytest/TestClient，Skills/上下文优化开，memory/MCP 关；主循环 24 步、Reviewer 累计 6 步。配置与源码 hash 随每次 manifest 保存。2026-09-23 再核对[官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/?article_id=article_1779470751466_8)，仍按输入 2 元/百万、输出 8 元/百万的峰值 cache-miss 口径，不扣缓存/错峰优惠。

| 尝试 / 场景 | run 状态 | 隐藏验收 | 秒 | 输入 / 输出 token | 预算估算元 |
| --- | --- | --- | ---: | ---: | ---: |
| 第 1 次 /sum（保留失败） | interrupted，Reviewer JSON 非法 | 通过 | 22.63 | 76689 / 3980 | 0.185218 |
| 第 2 次 /sum 开发 | succeeded | 通过 | 15.56 | 79186 / 2062 | 0.174868 |
| health 原仓改动只审查 | succeeded | 不适用 | 8.28 | 18013 / 1339 | 0.046738 |
| 同会话追加 maximum | succeeded | 通过 | 9.89 | 76705 / 1325 | 0.164010 |
| 同会话 /stats 只规划 | succeeded | 不适用 | 6.77 | 22596 / 1137 | 0.054288 |
| 新 /clamp 配对，Reviewer on | succeeded | 通过 | 14.50 | 55160 / 2792 | 0.132656 |
| 新 /clamp 配对，Reviewer off | succeeded | 通过 | 17.33 | 59720 / 2778 | 0.141664 |

此次 54 次实际模型请求，所有新增请求 usage 已知；没有新增未知预留。耗时包含 run 与外部隐藏验收；run 费用已包含 Reviewer，不二次相加。所有原仓均不变，review/plan 工作副本源码哈希也不变。

完整输入、run_id、版本、事件/工具轨迹、patch 和证据 SHA256 见 [可提交摘要](../eval/r4-followup/results.json)。本机原始 [开发组记录](../eval/results/r4-20260923-development-02/results.json)、[配对记录](../eval/results/r4-20260923-pair/results.json) 保留在 gitignored 的 eval/results，摘要及冻结输入进入阶段提交。

## 使用场景事实

- 开发 `5ee2421b-9ac3-45f4-996d-e4abec094046`：实现严格整数 `/sum`，当前版本测试与 Reviewer 完成，空 findings。模型先做过一次带 focus 的专项审查，之后再做默认交付审查；两轮语义不同，费用已计入，不当作默认审查复用。保留该事实，不声称所有额外审查均已消除。[交付 patch](../eval/results/r4-20260923-development-02/development/data/workspaces/development/runs/5ee2421b-9ac3-45f4-996d-e4abec094046/artifacts/delivery.patch)。
- 只审查 `8c345736-0971-4bf6-b083-1d34f4734502`：对独立构造的原仓 health `ok → broken` 改动报告 1 个有效回归；不自动修复。它不是主开发题发现 bug 的证据。
- 续聊 `f9983084-a4b4-4e9c-9154-76c08f9e6864`：与开发同 session；source_run_id 指向成功开发，追加 maximum（含全负数）后正式测试、Reviewer、隐藏验收均通过，[增量 patch](../eval/results/r4-20260923-development-02/development/data/workspaces/development/runs/f9983084-a4b4-4e9c-9154-76c08f9e6864/artifacts/delivery.patch) 可查看。
- 规划 `594f8ade-b81c-468a-95ea-618d0907cf07`：source_run_id 指向续聊成功版本；输出文件、步骤、拟议验证与不确定项。源码不变，没有写入/通用命令执行；verification 为 not_applicable，没有把拟议测试说成已运行。

## Reviewer 单变量配对（n=1）

原 `/normalize` 题已参与缺陷排查，继续保留为无效历史对照。本次预先冻结未使用的新 `/clamp` 题及独立隐藏验收，模型两侧需求与初始源码相同，唯一实验开关为 Reviewer；模型/Skills/上下文/内存/MCP/执行后端/步数与金额规则不变。初始版本均为 `b239e4b1d112bae31302cd1d`，独立工作副本、无共享模型历史。

on run `9762fd61-1713-41e8-9426-8d990b03df74` 只有 1 次 Reviewer 委派，completed、findings=[]；off run `1e4f6bb8-650e-4598-8972-d75eebe146d3` 委派事件为 0，review.enabled=false。两侧正式验收和隐藏验收均通过，完整任务状态 succeeded。因此开关此次真正形成对照。

本例有效问题、误报、采纳数均为 0；Reviewer 没有揭示可量化的质量改进。表中 token/耗时/费用仅描述本次结果；生成路径不同，n=1 不能将差异外推成 token 节省或普遍收益。on 的 seed_context 曾因限额省略 test_app.py，后续 read_file 已返回该文件；报告仍保留 seed 阶段限制与实际读取记录，不能据此称为无限制全仓审查。

## 收口范围

R4-REVIEW 的已知流程错误、R4-PAIR、R4-FOLLOWUP、R4-PLAN 已有上述代码/确定性/真实证据，可以在本轮小样本范围内关闭。模型仍可能误报或输出非法格式；明确失败、有限纠正和预算上限继续生效，不承诺任意任务都能完成。

**R2-VDB 仍待补**：只读检查确认既有本机 PostgreSQL 无法连接（OperationalError），未安装数据库、启动 Docker 或修改业务库；所以本轮不是 PostgreSQL/Redis/Celery 全栈认证。旧 144 次评测、第二模型、沙箱建设等未扩入本轮。未 push，原 HEAD/暂存区与无关修改保留。
