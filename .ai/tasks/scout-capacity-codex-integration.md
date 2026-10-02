# Scout 并行度与 Codex 接入

## 目标与约束

- 按用户要求更新 scout/worker/debug/reviewer/gate 模型与推理强度。
- 同一个父任务的十个只读 scout 可同时执行；继续支持两层 scout。
- 保留 Codex 原生设计、用户的原生 agent 配置及其线程上限。
- 在正常网络环境跑完整测试，核对实际服务加载；明确 fixture 与真实模型边界。
- 写任务独立 worktree、宿主验收、凭证不进入 Git、legacy 资产不重写。

## 决策

- 角色由服务默认注册表管理；debug 按已说明的 `gpt-6.1-sol / xhigh` 实现。
- 不把全服务限制为十个进程：4 个顶层槽位、每层 10 个 scout 槽位，共 24。
  各层容量由所有父任务共享，单父任务累计直接 child 配额为 10。
- Codex MCP 的 env_vars 是显式转发列表；委派 MCP 转发 scoped credential
  所需环境，并固定 child 标记，缺少凭证时拒绝回退到 owner token。
- 通过已启用的 MCP/Skill 使用服务；不会把原生 spawn 透明替换成外部 run。

## 派工与集成记录

| 专家 | 所有权/目录 | 状态与证据 | 待解决 |
|---|---|---|---|
| legacy_gate_facts / scout | legacy 与 Claude Haiku driver，只读 | 已返回 Bash driver、brief、取消与权限边界证据 | 无 |
| codex_current_dispatch_facts / scout | 原生配置、官方 docs 与 MCP，只读 | 已确认 MCP 启用；原生线程上限独立于服务 | 无 |
| parallel_ten_update / terra | 隔离 worktree `.worktrees/parallel-ten` 的配置、示例、测试与 MCP env 转发 | owner 逐份 diff 集成；局部 16 项通过 | 无 |
| owner | 核心 scout 生命周期、文档、集成、部署、验收 | 持续核对源码、进程与产物 | 见下 |

专家不 commit/push；原有未提交工作已在派工前备份。
技术入口：[Codex 接入与区别](../../docs/CODEX_INTEGRATION.md)。

## 当前验证与下一步

- 完整 pytest：84 passed，包含真实 TCP MCP fixture；legacy smoke 80 passed。
- 4/10/10 容量、24 个任务终态清理、配额与 scoped auth 均由测试覆盖。
- 服务已重载；API instance/source SHA256/角色/限制核对通过。
- 真实 stdio MCP → Codex scout → scoped MCP child 已通过；gpt-6.1-sol/high
  reviewer 真实模型文本 smoke 通过。原始证据保留在 Git 外。
- Codex 原生配置和 agent 文件校验值不变。原生并发仍与服务独立。
- 安装 checkout 的 `uv run --no-sync pytest -q`：84 passed；发布 diff 检查完成。
  实施和本机验收已完成，提交/推送版本由 Git 记录与远端 HEAD 核对。
- 十个并发为 fixture，不声称十个真实模型同时验收；未单独重验新的
  worker/debug 真实写入或另一台 PC。
