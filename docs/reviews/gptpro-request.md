# GPT Pro design review request

The following architecture request was submitted through the local GPTPro bridge on 2026-09-28, using requested effort -1. Both attempts failed before a model answer was obtained. No GPT Pro recommendations are claimed or used as acceptance evidence.

```
No reasoning-tier menu found on the composer after waiting 30000ms for hydration
```

Attempts: `319cc5abe8ec`, `ebf91faa887e`. There is no verified responding model slug or response transcript. The final design is the project owner's implementation and independent local review, supported by the tests in this repository.

## Request

请用中文对一个即将实现的本地 agent 执行服务做充分的架构与失效模式审阅，给出明确取舍和可执行的第一版范围。你是外部设计顾问，不要只列理想化平台功能。

用户要：把已有 Claude 相关目录中的 agent-exec 抽离成独立 Git/GitHub 项目；完善 agent 划分/调用；有 CLI、HTTP API、MCP、调试能力；作为常驻本机服务供其他 PC 和其他项目调用；部分替代 Claude Code / Codex / Grok 内置 agent 调度；特别用于 brainstorm，或修改 Codex 接入。保留已有工作和正在运行的旧 jobs。

已确认的旧实现：一个 2638 行 Python CLI，在 ~/.local/bin/agent-exec，codex-exec 别名。~/.config/agent-exec/roles.yaml 10 个角色，backends.yaml 指向 codex luna/terra/sol。命令有 start/hook-agent/dispatch/finish/wait/digest/result/diff/adopt/drop/verdict/report/gc/log/list/kill/roles/render/check/resolve。目前仅 codex 引擎，调用 codex exec --json，~/.codex-worker 保存 jobs 状态。另有旧开发源与测试、Claude agents generated drivers、协作 contract。约 3200 旧 job 记录且有 running 标记，不能迁移/覆盖它们。只复制源代码/模板/测试，不复制凭证/运行数据/私人上下文。

宿主实情：brainstorm 已有 append-only events，api.py 是唯一产品写入口；research 有自己可恢复队列，Council 是独立发言→互评→主席。Provider.complete(prompt, timeout, cwd=None) 和 SearchProvider.search(query, timeout) 是明确接口。TODO 暂无自动执行。brainstorm 主目录大量 dirty，不能大范围更改或双写业务状态。scaffold 也有自己的任务文件权威状态，不能和新服务争权。Codex 本机有 headless CLI，MCP 可接入；原生 child thread/context 与外部进程并非同一事物。

初拟设计：
1. 包名 agent-exec，Python >=3.10，保留原 runner 的兼容入口和迁移 provenance；新服务独立 XDG state/config，不改旧 jobs。
2. skill/AGENTS/CLAUDE 只负责方法和路由；一个服务负责执行，SQLite 为执行记录唯一权威。宿主任务 ID → service task/run ID → provider thread/session ID → workspace/base commit → events/artifacts → acceptance evidence 的可追踪链。
3. 把任务用途（research/implement/debug/review）与权限（read-only/worktree）、provider/model/effort、预算分开。保留 codex-scout/terra/sol 等 legacy aliases，provider 能力不满足时显式拒绝，不隐式放宽权限。
4. 第一版 durable bounded queue，一个服务进程，任务 queued/running/succeeded/failed/cancelled/timed_out/interrupted；进程成功与业务验收 pending/accepted/rejected 分开。请求幂等键，取消进程组，超时，重启时 interrupted，不自动重复执行有写入的任务；retry 新 run，关联原记录。
5. HTTP 请求引用已注册 workspace ID 和服务配置角色，不传任意 shell/argv/env/可执行路径；角色 command 由本机管理员配置。write 默认独立 git worktree，不覆盖 dirty 工作区，不自动 adopt/commit/merge。read-only 用 provider flags，明确同 UID 进程不是硬安全边界。防多个 worker 同时写同一工作目录。
6. FastAPI + bearer token，loopback 默认；另一台电脑经 SSH 隧道即可调用，局域网绑定需显式配置。MCP stdio 是 API client 不是第二份 scheduler；SDK/CLI 共用 API。token/config/log 不入 git。API 提供 health/capabilities/submit/status/events/result/cancel/acceptance；debug 可看 argv 的脱敏结构、退出码、stderr、耗时、trace，dry-run，不写模型可伪造的成功回执。
7. Codex/Claude/Grok provider adapters 在能力允许范围运行。当前优先真实验证 codex read-only，小型 fixture 验证超时/取消/非零退出/队列恢复/假回执等。缺少某 provider 的真实 auth 时不能宣称已实测。
8. brainstorm adapter 只实现 Provider 协议，将服务 run ID 映射到业务 job；继续走其 api.py 写业务事件。宿主的 task state 与 service execution state分开。Codex/Claude 通过 MCP 和 skill 接入，先不硬改 vendor binary。

请回答：
A. 对上述核心边界的评价，有无根本缺陷，尤其 CLI 迁移、workspace 隔离、控制面完整性、取消/重启、执行 vs 验收？
B. 合理的角色拆分与路由规则（owner/scout/worker/reviewer，什么时候不要 dispatch），怎样兼容宿主的不同能力并避免递归调度和 agent 爆炸？
C. 最小稳定 API/MCP 合同和状态机，幂等/并发/重试/事件应该如何处理，哪些值得第一版实现，哪些推迟？
D. 调试与可观测、测试矩阵，必须验证的真实反例（模型写成功 receipt 后 exit 7/超时、worker 修改控制状态、cancel races、restart orphan、dirty worktree、返回巨大输出、prompt injection）。
E. 给用户可直接采用的脑暴/多PC/独立项目/Codex 接入方案，以及不能声称替代原生机制的部分。
F. 按严重程度给 5-10 个具体建议，不要泛泛的安全清单或不必要的 enterprise 复杂度。
