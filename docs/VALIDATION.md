# 验证记录 · 2026-09-28

## 完成的检查

| 检查 | 结果 | 范围 |
|---|---|---|
| Python 3.13.12 测试 | 64 passed | 原 runner 兼容、核心状态/并发/进程、HTTP、真实 MCP stdio 协议、adapter、安装备份 |
| Python 3.10.12 测试 | 64 passed | 独立虚拟环境复验，未替换正在运行服务的解释器 |
| 原 shell smoke suite | 80 passed, 0 failed | 临时 HOME + fake Codex + 原 gate/route hook 和 golden drivers |
| wheel 构建 | passed | `uv build --wheel` |
| Skill frontmatter/结构 | passed | skill-creator quick_validate |
| 原入口兼容检查 | passed | `python3 /home/gema/.local/bin/agent-exec check --no-probe`，原 tables hash `869628a4468c` |
| Codex 真实文本调用 | passed | run `5ed6c20097d7442496176959571c09dd`，exit 0，预期文本匹配 |
| Codex 真实读文件 | passed | run `7dbf93729fb3482781f3827177a96c72`，读取 README 和当时的 scripts 目录 |
| Grok 真实文本调用 | passed | run `2ca61007f618469ca372c29f4f45d51c`，exit 0，预期文本匹配 |
| Codex 真实 worktree 写入 | passed | run `0347abbbe1444fcca2b322dec2e1ae29`，原目录不变、只新增指定 proof 文件 |
| 新 Codex 宿主 → MCP → 服务 → Codex worker | passed | run `00496cc537154562bbee2028f6f0890c`，实际调用 submit/status/result，返回 `AGENT_EXEC_HOST_DELEGATION_OK` |
| 写入补丁独立复验 | passed | SHA256 + 独立 clone 中 `git apply --check`、实际应用和逐字节核验 |
| Brainstorm Council 完整链路 | passed，fixture 模型 | 原产品 API → HTTP → 7 次执行 → 独立/互评/主席 → HUMAN/AGENT 文件 |
| 用户服务 | active | 用户选择局域网直连后改为 `0.0.0.0:9891`，systemd user unit，当前用户 `Linger=yes` |
| 局域网监听和认证 | passed，本机发起请求 | loopback 和两个 LAN IPv4 均返回同一服务实例；无令牌/错误令牌均 401；有效令牌的 health/capabilities/plan 均 200 |

64 项 pytest 在两种 Python 上各有一条上游 Starlette TestClient/httpx deprecation warning，不影响结果。没有把本地 fixture 或工作进程自报结果当成真实模型验证。

重要反例覆盖：输出 `SUCCESS` 后退出 7、伪造 receipt 后超时、空最终答案、JSON semantic error、并发幂等、重复键冲突、取消队列/运行中任务、重试角色漂移、父进程 SIGKILL、PID 复用防护、输出洪泛、实时日志、dirty checkout 拒绝、原工作树保留、未跟踪文件补丁、补丁读取本身的超时、慢 Git admission 不阻塞取消、无效 JSON、未认证请求、超大 HTTP body，以及 MCP initialize/tools/list/tools/call。

补丁 SHA256：`861ebab6574d77634682606db06e6e85fb8a7795a6b808a6568c0788bea2ded8`。Owner 在独立复验后写入 accepted，worker 本身没有接受权限工具。

## 外部限制和保留的失败

- Claude 真实调用返回 exit 1 和账号 on hold 提示，run `679b3e7782fc4afcac277a45b1705040`。适配器正确保留 failed；没有更改账户、凭证或绕过限制。修复账户后应重新做真实调用。
- GPTPro 两次请求都在浏览器 reasoning-tier menu hydration 阶段失败，没有模型答复。完整请求和错误在 [review request](reviews/gptpro-request.md)，不存在可引用的 GPT Pro 设计结论。
- 初次 Codex 验证暴露“对不存在的 MCP 项写 enabled=false 会形成无 transport 的无效配置”，已改成仅禁用已存在项并加入回归测试。初次 Grok 验证暴露其实际 JSON 使用 `text/sessionId/stopReason`，已支持并加入回归测试。旧失败记录保留，没有涂改成成功。
- 初次安装暴露本机 systemd 的 WorkingDirectory 不接受带引号的写法，安装器已修正并加入安装检查；源入口替换前的原始备份仍在。
- 真实宿主测试发现未标注的 MCP 查询工具会进入 approval 路径，在 `approval_policy=never` 下失败。现在准确标记读写属性，并使用官方支持的逐工具 `approval_mode="approve"` 授权 submit/cancel；已通过新的非交互 Codex 会话验证。全局批准策略未放宽。
- 已按用户选择启用局域网直连，并从本机通过 LAN 地址验证认证 API；尚未从第二台物理 PC 连入验收。README 提供直连的 Client/MCP 环境变量、令牌复制和连通检查，SSH 隧道仍可选；实际跨机路由与防火墙尚未独立验证。
- 安装主机的 UFW 已启用，默认入站 DROP；当前会话没有无需密码的 sudo 权限，无法读取现有用户规则或添加放行规则。README 提供管理员可执行的网段限定命令。本机 LAN 地址请求不会证明外部流量已通过 UFW。
- Brainstorm 原生产服务和 dirty checkout 未修改或重启。注入 adapter 的临时 Council 链路通过；三家真实模型联合 Council 未通过验收（Claude 账号受限）。SearchProvider、TODO 自动执行和原生 child-thread/fork/message 语义不在本版实现内。
- 原 legacy 命令保留历史默认权限；新服务的 worktree/权限默认不追溯覆盖它。服务为同 UID 可信使用，不提供恶意多租户隔离、硬 token/cost 预算或分布式任务调度。

## 本机证据位置

原始日志、提示词、token 和状态库均在 Git 外。

- 最终测试日志：`/home/gema/.local/state/agent-exec/verification/final/pytest-py313.log`、`/home/gema/.local/state/agent-exec/verification/final/pytest-py310.log`。
- 旧 shell 套件：`/home/gema/.local/state/agent-exec/verification/final/legacy-smoke.log`。
- 服务实例/源码摘要：`/home/gema/.local/state/agent-exec/verification/final/service-health.json`。
- 局域网监听/认证探测：`/home/gema/.local/state/agent-exec/verification/final/lan-access.json`（实际 IP 仅保存在本机证据中）。
- Codex 宿主委派全过程：`/home/gema/.local/state/agent-exec/verification/final/codex-host-delegation.jsonl`。
- 真实 writer 独立验收：`/home/gema/.local/state/agent-exec/verification/real-writer-report.json`。
- 补丁：`/home/gema/.local/state/agent-exec/verification/real-writer.patch`。
- Brainstorm fixture：`/home/gema/.local/state/agent-exec/verification/brainstorm-20260928/report.json`。
- 未修改的原 runner 备份：`/home/gema/.local/state/agent-exec/install-backups/20260928T151255-67925b11/agent-exec`。
- 宿主配置备份：`/home/gema/.local/state/agent-exec/host-config-backups/20260928T151416-605ddf2a/`。

复现命令见 README 和 scripts。历史记录只代表所列版本/运行，后续模型、账号、CLI 或配置变化需要重新验证。


## 2026-10-02：角色、scout 并行度和 Codex MCP

- 正常网络环境 Python 3.13 pytest：84 passed（包括两个真实 TCP + stdio MCP fixture）。
- legacy shell smoke：80 passed、0 failed；历史资产和 golden runner 未修改。
- 新容量测试使用 fake Codex：10 个顶层、同一父任务 10 个 scout、下一层 10 个 scout
  同时进入已准备执行状态；第 11 个直接 child 被拒绝，取消后 30 个任务均清理完成。
- 真实 stdio MCP → 服务 → `gpt-6-luna / medium` Codex → scoped scout MCP
  → 同模型 child：父子 exit 0、succeeded，child 输出匹配；通过服务父子关联、
  provider session ID 和 supervisor 状态核对，未只采信模型报告。
- `gpt-6.1-sol / high` reviewer 真实文本 smoke：exit 0、succeeded，标记匹配。
- 服务已重载，API 确认新 instance、源码 SHA256、五个角色、gate 独立别名以及
  10/10/10 的槽位配置；Codex config 和原生 agent 文件部署前后校验值一致。
- 明确边界：十个并发使用 fixture；未同时运行十个真实模型，也未单独重验新
  worker/debug 的真实写入、Claude/Grok 或第二台 PC。3.10 本轮只做语法兼容检查。

MCP credential 传递使用官方 `env_vars` 显式转发；固定 child 标记使缺少 scoped
credential 的代理拒绝回退至 owner token。该修复有 command 回归与真实嵌套 smoke。
上下文、follow-up、原生线程与外部 run 的区别见 [Codex 接入说明](CODEX_INTEGRATION.md)。
原始日志、部署备份和模型运行证据留在 Git 外的私有本机更新目录。
