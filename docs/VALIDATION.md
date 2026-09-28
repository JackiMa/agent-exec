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
| 用户服务 | active | `127.0.0.1:9891`，systemd user unit，当前用户 `Linger=yes` |

64 项 pytest 在两种 Python 上各有一条上游 Starlette TestClient/httpx deprecation warning，不影响结果。没有把本地 fixture 或工作进程自报结果当成真实模型验证。

重要反例覆盖：输出 `SUCCESS` 后退出 7、伪造 receipt 后超时、空最终答案、JSON semantic error、并发幂等、重复键冲突、取消队列/运行中任务、重试角色漂移、父进程 SIGKILL、PID 复用防护、输出洪泛、实时日志、dirty checkout 拒绝、原工作树保留、未跟踪文件补丁、补丁读取本身的超时、慢 Git admission 不阻塞取消、无效 JSON、未认证请求、超大 HTTP body，以及 MCP initialize/tools/list/tools/call。

补丁 SHA256：`861ebab6574d77634682606db06e6e85fb8a7795a6b808a6568c0788bea2ded8`。Owner 在独立复验后写入 accepted，worker 本身没有接受权限工具。

## 外部限制和保留的失败

- Claude 真实调用返回 exit 1 和账号 on hold 提示，run `679b3e7782fc4afcac277a45b1705040`。适配器正确保留 failed；没有更改账户、凭证或绕过限制。修复账户后应重新做真实调用。
- GPTPro 两次请求都在浏览器 reasoning-tier menu hydration 阶段失败，没有模型答复。完整请求和错误在 [review request](reviews/gptpro-request.md)，不存在可引用的 GPT Pro 设计结论。
- 初次 Codex 验证暴露“对不存在的 MCP 项写 enabled=false 会形成无 transport 的无效配置”，已改成仅禁用已存在项并加入回归测试。初次 Grok 验证暴露其实际 JSON 使用 `text/sessionId/stopReason`，已支持并加入回归测试。旧失败记录保留，没有涂改成成功。
- 初次安装暴露本机 systemd 的 WorkingDirectory 不接受带引号的写法，安装器已修正并加入安装检查；源入口替换前的原始备份仍在。
- 真实宿主测试发现未标注的 MCP 查询工具会进入 approval 路径，在 `approval_policy=never` 下失败。现在准确标记读写属性，并使用官方支持的逐工具 `approval_mode="approve"` 授权 submit/cancel；已通过新的非交互 Codex 会话验证。全局批准策略未放宽。
- 尚未从第二台物理 PC 连入验收；已提供同一认证 API、Client 和 SSH 隧道方法。实际跨机网络、SSH 账号与防火墙仍取决于用户环境。
- Brainstorm 原生产服务和 dirty checkout 未修改或重启。注入 adapter 的临时 Council 链路通过；三家真实模型联合 Council 未通过验收（Claude 账号受限）。SearchProvider、TODO 自动执行和原生 child-thread/fork/message 语义不在本版实现内。
- 原 legacy 命令保留历史默认权限；新服务的 worktree/权限默认不追溯覆盖它。服务为同 UID 可信使用，不提供恶意多租户隔离、硬 token/cost 预算或分布式任务调度。

## 本机证据位置

原始日志、提示词、token 和状态库均在 Git 外。

- 最终测试日志：`/home/gema/.local/state/agent-exec/verification/final/pytest-py313.log`、`/home/gema/.local/state/agent-exec/verification/final/pytest-py310.log`。
- 旧 shell 套件：`/home/gema/.local/state/agent-exec/verification/final/legacy-smoke.log`。
- 服务实例/源码摘要：`/home/gema/.local/state/agent-exec/verification/final/service-health.json`。
- Codex 宿主委派全过程：`/home/gema/.local/state/agent-exec/verification/final/codex-host-delegation.jsonl`。
- 真实 writer 独立验收：`/home/gema/.local/state/agent-exec/verification/real-writer-report.json`。
- 补丁：`/home/gema/.local/state/agent-exec/verification/real-writer.patch`。
- Brainstorm fixture：`/home/gema/.local/state/agent-exec/verification/brainstorm-20260928/report.json`。
- 未修改的原 runner 备份：`/home/gema/.local/state/agent-exec/install-backups/20260928T151255-67925b11/agent-exec`。
- 宿主配置备份：`/home/gema/.local/state/agent-exec/host-config-backups/20260928T151416-605ddf2a/`。

复现命令见 README 和 scripts。历史记录只代表所列版本/运行，后续模型、账号、CLI 或配置变化需要重新验证。
