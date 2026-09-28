# agent-exec

把 agent 执行从 Claude/Codex 的宿主会话里拆出来：同一个本机服务提供持久任务、受控并发、进程监督、HTTP API、Python Client、CLI 和 MCP。宿主负责拆任务和验收，服务负责把每次执行做完、留下可查的结果。

项目提取了原来的 Claude-era `agent-exec`，原始执行器按字节保存在 `src/agent_exec/_legacy.py`，来源哈希见 `legacy/provenance.json`。旧任务目录和用户配置不进入 Git。

## 安装和启动

Linux、Git、Python 3.10+、uv。需要调用的模型 CLI 和登录由本机提供。

```sh
git clone https://github.com/JackiMa/agent-exec.git /home/gema/workspace/agent-exec
cd /home/gema/workspace/agent-exec
uv sync --locked
uv run pytest -q
uv run python scripts/verify_legacy_smoke.py
uv run python scripts/install_user.py --start
agent-exec capabilities
```

安装器创建独立的 `~/.config/agent-exec/service.yaml`、0600 权限的 `service.token`、`~/.local/state/agent-exec/` 和 `agent-exec.service` 用户服务；已有配置保留，替换入口前保存唯一备份。Claude/Codex 的 Skill 以符号链接挂载到此项目。机器的实际路径见安装输出。用户服务默认随用户会话启动；退出登录后常驻取决于系统的 user lingering 设置。

默认只监听 `127.0.0.1:9891`。旧根命令 `start/dispatch/list/wait/result/check/...` 继续进入原执行器，新服务使用 `task` 子命令，不会接管旧 `~/.codex-worker` 中的运行记录。

## 调用

```sh
agent-exec plan --workspace agent-exec --role codex-scout --goal '检查项目结构，列出事实和路径'
agent-exec task submit --workspace agent-exec --role codex-scout \
  --goal '检查项目结构，列出事实和路径' --caller-task-id project:task-42 \
  --idempotency-key task-42-scout-1 --wait
agent-exec task result RUN_ID
agent-exec task events RUN_ID
agent-exec task logs RUN_ID --stream stderr
agent-exec task diff RUN_ID
agent-exec task cancel RUN_ID
agent-exec task retry RUN_ID
agent-exec task verdict RUN_ID --accept --reason '独立核对完成' --evidence /absolute/path/to/test.log
```

`plan` 校验配置和权限，不启动模型。提交立即返回 run ID；等待超时不会隐式取消，取消是单独操作。重复使用同一个幂等键和同一 JSON 请求体，返回同一 run；变更请求体则冲突。失败、取消、超时或中断后的重试会产生新 run，并记录 `retry_of`，不会自动重跑写入任务。

Python API：

```python
from agent_exec.client import Client

with Client() as client:
    run = client.submit({
        "workspace": "agent-exec", "role": "codex-scout",
        "goal": "Read the README and summarize the API.",
        "caller_task_id": "my-project:42", "timeout_seconds": 120,
    }, idempotency_key="my-project-42-attempt-1")
    terminal = client.wait(run["id"], timeout=150)
    result = client.result(run["id"])
    print(terminal["status"], result["output"])
```

HTTP 使用 `Authorization: Bearer ...`。`POST /v1/runs` 提交，`GET /v1/runs/{id}` 查询，后接 `/events`、`/logs`、`/result`、`/diff` 获取证据；`POST .../cancel`、`.../retry`、`.../verdict` 操作状态。完整字段见 [API 合同](docs/TRANSPORT_CONTRACT.md)。服务默认不向浏览器开启 CORS，公共 `/healthz` 仅暴露健康状态和版本。

## 角色和权限

| 角色 | 默认模型/引擎 | 用途 | 执行权限 |
|---|---|---|---|
| codex-scout | Codex / gpt-5.6-luna / medium | 只读取证 | read-only |
| codex-reviewer | Codex / gpt-5.6-terra / high | 独立复核 | read-only |
| codex-worker | Codex / gpt-5.6-terra / high | 已定方案的实现 | 独立 worktree |
| codex-debug | Codex / gpt-5.6-sol / high | 复杂复现与修复 | 独立 worktree |
| claude-chat | 本机 Claude 默认模型 | 纯文本讨论 | 禁用工具和自定义配置 |
| grok-chat | 本机 Grok 默认模型 | 纯文本讨论 | 禁用内置工具、子 agent 和 web；拒绝工具权限 |

模型可在本机 YAML 中覆盖，不等同于账号一定有访问权限。服务支持旧角色名的显式别名；新的写入角色统一要求 workspace 注册允许写入、主 checkout 干净，且在独立 worktree 执行。脏目录会被拒绝并保留；需要旧的 dirty snapshot 流程时显式使用 legacy 命令。

服务冻结最终 patch（包含未跟踪文件），提供 SHA256 和无损 Base64，保留 worktree 给 owner 复核。不会自动提交、合并或删除它。`succeeded` 表示执行和最终响应成功，`acceptance=pending` 仍要求宿主核验；API caller 写入的 verdict 是判断记录，不是独立证明。

## 其他电脑

局域网直连：在服务端 `/home/gema/.config/agent-exec/service.yaml` 设置 `host: 0.0.0.0`，保留 `port: 9891` 和 `token_file`。先确认没有运行或排队的任务，再执行 `systemctl --user restart agent-exec.service`。此绑定同时保留本机 MCP 的 loopback 连接；客户端使用服务端实际局域网 IP，不使用 `0.0.0.0`。非 loopback 必须有至少 32 字符令牌。

另一台电脑安装本仓库的 Python 包后，通过 SSH 安全复制令牌文件并连接（将 `SERVER_LAN_IP` 和本机绝对路径替换为实际值）：

```sh
mkdir -p /absolute/private/path
chmod 700 /absolute/private/path
scp gema@SERVER_LAN_IP:/home/gema/.config/agent-exec/service.token /absolute/private/path/service.token
chmod 600 /absolute/private/path/service.token
export AGENT_EXEC_URL=http://SERVER_LAN_IP:9891
export AGENT_EXEC_TOKEN_FILE=/absolute/private/path/service.token
agent-exec capabilities
agent-exec plan --workspace agent-exec --role codex-scout --goal '检查项目结构，列出事实和路径'
```

Python Client、CLI 和 MCP 都读取这两个环境变量。只安装客户端时不需要执行服务安装器，也不需要在客户端登录模型；任务由服务端已登录的 CLI 执行。仓库文件仍在服务端；第一版不做仓库同步或远端工作树上传。远端 MCP 的进程环境也需传入上述 URL 和 token 文件路径，模板见 [examples](examples/)。令牌授予当前服务配置内的任务操作权限，应只交给可信的电脑。

局域网 HTTP 的令牌和任务内容不加密，适用于可信网络；跨不可信网络使用 TLS 反向代理或 SSH 隧道。端口须能通过服务端防火墙，安装器不自动修改防火墙或路由器端口映射。可在另一台电脑运行 `curl --noproxy '*' http://SERVER_LAN_IP:9891/healthz` 检查网络，再运行 `agent-exec capabilities` 验证令牌；只有后者能访问任务 API。

使用 UFW 且入站被拦时，在服务端终端以管理员权限放行客户端网段。将示例 `192.168.1.0/24` 替换为实际可信网段，不必向所有来源开放：

```sh
sudo ufw allow from 192.168.1.0/24 to any port 9891 proto tcp comment 'agent-exec trusted LAN'
sudo ufw status
```

SSH 隧道仍可选：

```sh
ssh -N -L 9891:127.0.0.1:9891 gema@YOUR_HOST
```

使用隧道时将 `AGENT_EXEC_URL` 改为 `http://127.0.0.1:9891`，继续使用私有 token 文件。

## 宿主接入

```sh
codex mcp add agent_exec -- /home/gema/workspace/agent-exec/.venv/bin/agent-exec mcp
claude mcp add --transport stdio --scope user agent_exec -- /home/gema/workspace/agent-exec/.venv/bin/agent-exec mcp
```

MCP 是 HTTP 代理，不再启动一套调度器。宿主可调用 `agent_exec_capabilities/plan/submit/status/events/result/diff/cancel`。模板在 [examples](examples/)，调度方法在 [Skill](skills/agent-exec/SKILL.md)。新宿主会话会发现挂载的 Skill/MCP；已打开会话是否动态刷新取决于宿主。

Codex 非交互会话还需要逐工具授权。示例 TOML 将默认设为 `writes`，并只对 `agent_exec_submit`、`agent_exec_cancel` 设置 `approval_mode="approve"`；这明确允许宿主在服务已注册的权限范围里自主派工/取消。删除这两项即可保留逐次批准。查询工具带准确的 `readOnlyHint`，提交工具仍标为有副作用，取消工具标为破坏性。不要把全局 sandbox 改成 bypass 来解决 MCP 批准问题。

Brainstorm 的 [完成接口适配器](src/agent_exec/integrations/brainstorm.py) 可注入 `api.start_council(..., providers=...)`，例子见 [Council](examples/brainstorm_council.py)。Council 临时 cwd 使用显式 `prompt_only=True`，在已注册空工作目录运行完整提示词。Brainstorm 自己仍拥有业务事件、阶段和来源核验；该适配器不接管 research 搜索、不自动执行 TODO。

外部服务接管执行排队和进程生命周期；宿主原生的 child thread、上下文继承、消息注入、UI 和 session resume 并未被模拟。本机 Codex npm 安装是 native binary，优先使用公开的 `codex exec`/MCP 接口。独立源码 fork 的接入方式和限制见 [设计](docs/ARCHITECTURE.md)。

## 调试和验证

```sh
systemctl --user status agent-exec.service
journalctl --user -u agent-exec.service -n 100 --no-pager
agent-exec task show RUN_ID
agent-exec task logs RUN_ID --stream stderr
agent-exec task events RUN_ID
cd /home/gema/workspace/agent-exec
uv run pytest -q
```

状态、提示词和有界日志留在私有 state 目录，不上传 GitHub。一个进程独占 SQLite 状态库；第二个服务实例会被拒绝。超时、取消、父进程死亡、重启恢复、假成功回执、日志溢出和 MCP 协议均有 fixture 测试。实际模型与宿主验收另见 [验证记录](docs/VALIDATION.md)。

认证后的 `/v1/health` 返回服务实例 ID 和启动时的 Python 源码摘要，run 也记录执行它的实例和源码摘要，可区分磁盘更新与进程已加载的版本。

这是同一用户下的可信本机执行服务，不是面向恶意租户的多用户沙箱。worktree 隔离改动，Codex sandbox 限制工具访问；不能阻止具有相同 OS 身份的恶意进程读取凭证或绕过控制面。`command` provider 仅供管理员配置的可信命令/fixture，明确没有沙箱保证。服务版本一不提供强隔离、多租户配额或分布式调度。
