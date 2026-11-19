# dsh 接入说明（Integration Guide）

本文说明 dsh 如何加载本插件、调用动作、消费事件，以及如何向用户呈现确认和安全事件。dsh 可以通过仓库自带的 Cordis bundle 直接加载本插件。

**兼容性说明：** 插件服务使用自定义的行分隔 JSON-RPC 2.0 协议，不是 MCP server。Codex 和 Claude Code 目前不能直接加载本插件；仓库的 `index.js` 只适配 dsh/Cordis，并非 MCP 适配器。其他 harness 若能接入自定义 stdio 子进程或 HTTP JSON-RPC 服务，可按本文协议自行桥接。若想通过 MCP 使用，则需要另行实现 MCP 适配器。

在 dsh 中安装插件：

```bash
dsh plugin --profile demo add github:JohnXu22786/computer-control
```

将 `demo` 替换为目标 profile 名称。

bundle 要求 Node.js 18.17+ 和 Python 3.9+。Python 包必须安装到 dsh 实际调用的 Python 环境；固定安装 v0.1.1：

```bash
python -m pip install "git+https://github.com/JohnXu22786/computer-control.git@v0.1.1"
python -m computer_control check
```

完整安装说明也见 [README.zh.md](../README.zh.md)。

## 1. 加载流程

1. **发现**：dsh 在插件目录找到 `manifest.json`。
2. **解析清单**：
   - `entry.command`：启动命令（`["python", "-m", "computer_control", "serve"]`），工作目录为插件目录；也可改为绝对解释器路径。
   - `transport`：`stdio-jsonrpc`，行分隔 JSON（每行一个 JSON 对象，UTF-8）。
   - `tools` / `events`：插件声明的能力与事件（供模型工具列表与 harness 事件订阅用）。
3. **拉起进程**：以管道连接 stdin/stdout（stderr 仅用于日志，勿解析）。
4. **握手**：发送 `session.start`（参数为配置对象，可省略）。收到 `session.started` 事件或 `ok:true` 响应后进入就绪态。
5. **退出**：发送 `session.stop`，随后可终止进程。

> stdout 只承载协议流量；插件日志一律走 stderr。

## 2. 调用工具

- 单个：`tools.call {tool, arguments}`
- 批量：`tools.call_batch {items, continue_on_error?, gap_ms?}`

响应统一为信封：

```json
{"ok": true, "result": {...}, "error": null, "meta": {"tool": "...", "duration_ms": 12, "state": "ready", "frame": 3}}
{"ok": false, "result": null, "error": {"code": "policy_denied", "message": "...", "data": {...}}, "meta": {...}}
```

动作级失败（参数错误、策略拒绝、快照过期、后端缺失……）都在信封内表达；JSON-RPC 层 `error` 仅用于协议级问题（非法 JSON、未知方法、参数非对象）。

### 模型调用循环（推荐）

```
screen.capture (观察)  ->  依据图像内容决策
pointer.click / keyboard.type / a11y.activate (执行)
wait.pause (如界面有动画)
screen.capture (验证结果，必要时重复)
```

- 所有坐标都是**模型画布坐标**（见 README「坐标契约」），插件负责映射到物理像素。
- 语义操作优先：元素在可访问性树中可见时用 `a11y.snapshot` + `a11y.activate`；树不可用或目标不可达时退回 `pointer.click` 坐标点击。
- 截图带了 `frame` 序号；`a11y.snapshot` 带了 `snapshot_id`。模型必须使用**最近一次**的 snapshot_id 做语义动作，否则返回 `stale_snapshot`——这是防止「按旧布局乱点」的防呆机制。

## 3. 事件

服务端主动推送的通知：

```json
{"jsonrpc": "2.0", "method": "event", "params": {"type": "action.finished", "payload": {...}}}
```

| 事件 | 触发时机 | harness 应做什么 |
| --- | --- | --- |
| `session.started` / `session.stopped` | 会话开始/结束 | 更新可用状态 |
| `session.resumed` / `session.configured` | 恢复/重配 | 刷新状态展示 |
| `session.idle` | 空闲超时进入待机 | 提示用户「已待机」，可自动或提示恢复 |
| `action.started` / `action.finished` | 每个动作 | 记录动作轨迹；`finished` 带 ok 与耗时 |
| `batch.finished` | 批量完成 | 记录批量结果；`results` 包含每项的完整结果信封（成功结果、错误详情和元数据） |
| `safety.confirmation_requested` | 高风险动作待批 | **弹出人工确认 UI**（见下） |
| `safety.confirmation_resolved` | 已批复 | 关闭确认 UI |
| `safety.confirmation_expired` | 超时未批（自动拒绝） | 提示已拒绝 |
| `safety.panic_triggered` / `safety.panic_released` | 急停触发/解除 | 显著提示用户；急停期间禁止再发动作 |
| `safety.grant_changed` | 授权模式变更（`{from, to, actor, widened}`） | 刷新权限展示；`widened: true` 意味着宿主刚在带外提权，值得记一笔审计日志 |

HTTP 传输下 `GET /events` 提供 SSE 流，会先回放最近 200 条事件再续传（harness 重连不丢上下文）。

## 4. 确认流（高风险动作）

流程：

```
model: tools.call keyboard.combo {keys:[win,r]}
server: {"ok":true,"result":{"status":"awaiting_confirmation","request_id":"cfm-...","risk":"high","reason":"..."}}
server → event safety.confirmation_requested {request_id, tool, arguments, risk, expires_at, timeout_s}
harness: 弹窗展示「模型想按 Win+R，批准吗？」（显示工具、参数、风险、剩余时间）
human:   批准
harness: session.confirm {request_id, approve: true}
server → 立即响应（不阻塞）；随后经工作线程执行动作
server → event safety.confirmation_resolved
server → event action.started → event action.finished
```

- 批复响应立即返回，被批准的动作在工作线程上与其他动作串行执行；事件顺序为 `confirmation_resolved` → `action.started` → `action.finished`。
- 批准后、执行前若授权模式被降级/收回，该动作不会执行，而是发出 `action.finished`（`ok:false`、`error_code:not_authorized`）；宿主应据此刷新权限展示并提示用户。最终授权检查与派发在同一把 admission 锁内完成，`session.configure` / `grant_profile` / 控制文件通道（复用同一应用路径）不可能插在两者之间；批量每个 item 各自过锁，降级可以落在 item 之间。
- 降级落在挂起的批量确认上时按**子动作**深检：批内任一子动作不再被新档位放行（如降级到 `limited` 后仍有 `keyboard.*`），整批确认被取消（发 `confirmation_resolved, approve:false`）。
- 待批期间新动作一律返回 `busy`（先批复再继续）。
- 超时（`safety.confirm_timeout_s`，默认 30s）自动拒绝并发出 `confirmation_expired`；之后用旧 id 批复返回 `confirmation_expired` 错误。
- 批量中任一项需要确认 → **整批**等待一次批复，批准后整批执行（含触发确认的那一项）。
- 急停/会话停止会取消待批项（发出 `confirmation_resolved, approve:false`）。
- 空闲看门狗不会把「有待批确认」的会话转入待机。

## 5. 会话状态机

```
idle --session.start--> ready
ready --高风险动作待批--> confirming（隐含）
ready --急停--> stopped
ready --空闲超时--> standby
stopped / standby --session.resume（或热键）--> ready
任意 --session.stop--> idle
```

`system.status` 返回当前 `state`、`capabilities`、`surface`、`pending_confirmation`（含剩余秒数）等。

## 6. 运行时配置

- 配置来源优先级：内置默认 < 配置文件（`--config` 或 `COMPUTER_CONTROL_CONFIG`）< `session.start` 参数 < `session.configure`。配置文件的规则、白名单模式、热键等在 `session.start` 前即生效。
- `session.configure {capture|safety|a11y|runtime 的子集}` 运行时调整（规则、确认阈值、空闲超时等）；`platform`、热键、panic 文件、`capture.backend` 等不可运行时修改（返回 `invalid_config`）。
- 规则可随时替换：`session.configure {safety: {rules: [...]}}`。

## 7. 宿主控制通道（带外档位变更）

dsh 场景下插件是 **stdio 子进程**，宿主调不到 Python 方法 `Session.grant_profile`；而凡可经 JSON-RPC 路由的方法 agent 都触达得到，路由表里不能有任何扩权入口。宿主因此用**控制文件通道**改档位 / 开关 `a11y_snapshot`：

- **启用**：启动时 `serve --control-file PATH`（或环境变量 `COMPUTER_CONTROL_CONTROL_FILE`）。dsh 场景最简单的接法是在 harness 环境里设该环境变量——`index.js` 派生子进程时未覆盖 env，父环境会原样继承；也可在 bundle 的 `entry.command` 里追加 `--control-file PATH`。通道只读该文件、每 0.1s 轮询一次；同一服务进程内的 `session` 重启不丢已生效的 `seq`。服务进程重启会清零内存计数，并重新应用仍存在的控制文件作为宿主当前期望状态。
- **指令格式**（宿主**原子替换**整个文件：同目录写临时文件后 `os.replace`）：
  ```json
  {"seq": 7, "auth_profile": "limited", "a11y_snapshot": true}
  ```
  - `seq`（必填，正整数，当前服务进程内严格递增）：只有比上一次已生效值更大的 `seq` 才生效——**重授同一档位也要抬 seq**；同一进程内旧文件内容重放是无害空操作，服务进程重启后现存文件会再次应用。
  - `auth_profile` / `a11y_snapshot`（均必填）：每个文件都是完整宿主期望状态快照；更新其中一项时重复写入另一项的当前值。`auth_profile` 可任意方向变更；`a11y_snapshot` 控制 `read_only` / `revoked` 下是否放行 `a11y.snapshot`（`safety.a11y_snapshot_opt_in`）。
  - 内容非法（缺字段、未知键、非法档位）直接忽略且**不消耗 seq**；无会话在运行时指令**挂起待用**，落到下一个启动的会话。删除文件不会改变当前会话；若新进程启动时文件不存在，会话从宿主配置开始。
- **生效确认**：档位变更发 `safety.grant_changed`（`from` / `to` / `actor:"host"` / `widened`）；纯 `a11y_snapshot` 开关发 `session.configured`（`patch.safety.a11y_snapshot_opt_in`）。收不到事件即未生效，宿主应抬 seq 重写。
- **安全边界**：路径不出现在任何配置 schema、路由表、manifest 或事件里，agent 无法指名也无法重定向；插件自身没有可写任意文件的工具。授权边界由 OS ACL 提供，不能依赖路径保密：Linux 下文件和父目录须由 root 拥有、不可被 group/other 写入，且访问/默认 POSIX ACL 不得向其他主体授予有效写权限；插件以非特权用户只读访问，每个进程线程的 permitted、effective、ambient 与 inheritable capability 集都必须为空，且插件线程的 CapBnd 不得包含 CAP_CHOWN、CAP_DAC_OVERRIDE、CAP_FOWNER、CAP_SETUID、CAP_SYS_PTRACE 或 CAP_SYS_ADMIN（前四项可绕过文件属主/权限检查或将文件系统 UID 切换为 root；CAP_SYS_PTRACE 可控制同一用户命名空间内的进程，包括可信的宿主控制文件写入者；CAP_SYS_ADMIN 可将攻击者控制的文件系统挂载到控制路径上）；无法验证 NoNewPrivs=1、capability 集或 CapBnd 时会拒绝启用控制通道。为防止同 UID 进程利用 setuid-root 程序，登录/会话管理器须在启动整个交互会话（含桌面、终端和启动器）前设置 NoNewPrivs 并移除危险 bounding-set 位。运行时只检查插件线程，不验证同 UID 的兄弟进程或未来进程；若要保护这些进程，须采用可强制执行的系统级隔离。Windows 下文件和父目录须由 LocalSystem 或内置 Administrators 所有，写权限仅授予这些主体；插件必须以未提升权限运行。程序会检查并拒绝不安全路径。不要给插件的交互式 OS 身份 sudo/管理员权限，否则 agent 可通过桌面输入打开 shell 并伪造宿主指令。其他 POSIX 平台因无法验证扩展 ACL 会拒绝启用控制通道。
- 事件流同时是**审计通道**：`safety.grant_changed` 里 `widened: true` 即宿主带外提权，建议 harness 记审计日志并向用户显著提示。

## 8. 其他建议

- **启动时**用 `tools.list` 检查各工具 `available` 标志（例如 UIA 缺失时 `a11y.*` 为 false），据此裁剪模型可见的工具集——能力广播，避免模型调用注定失败的工具。
- 模型提示词建议包含：坐标必须在画布内；语义优先；每步截图自检；高风险动作会被人工确认，属正常现象。
- 超时防护：harness 侧 `tools.call` 超时应大于 `runtime.max_wait_ms`（默认 600s）+ 余量；超时后动作**可能仍在执行**（结果未知，非未执行），需以新截图确认状态。
- `session.stop` 语义：排队与后续动作立即被拒绝；**正在执行中的注入无法中途打断**（硬件事件流），会执行完毕后结束会话。

## 9. 接入其他 harness

Codex 和 Claude Code 常用 MCP 协议接入外部工具。不能把 `python -m computer_control serve` 直接写入 MCP server 配置：本服务虽然也是 JSON-RPC，但方法名、初始化流程、工具定义和事件格式均为本插件自定义协议。dsh 的 Cordis 桥接层也不能直接充当 MCP server。

如果宿主支持自定义 stdio 工具，启动 Python 子进程：

```bash
python -m computer_control serve
```

宿主需要实现以下适配职责：

1. 启动进程并通过 stdin/stdout 读写 UTF-8 行分隔 JSON；stdout 只解析协议消息，stderr 用作日志。
2. 建立会话：发送 `session.start`，读取 `manifest.json` 或调用 `tools.list` 来发现工具，并将宿主的工具调用映射为 `tools.call` / `tools.call_batch`。
3. 持续读取服务器发出的 `event` 通知。将 `screen.capture` 的 `data_url` 转为宿主支持的图片内容，便于模型观察屏幕。
4. 收到 `safety.confirmation_requested` 时，把工具名、参数、风险和超时展示给用户。只有用户明确批准后才调用 `session.confirm`；拒绝、超时应保持拒绝，不能由模型或适配器自动批准。
5. 会话结束时调用 `session.stop`，然后关闭子进程。

若宿主更适合 HTTP，可运行：

```bash
python -m computer_control serve --transport http --port 8765
```

请求通过 `POST /rpc` 发送，事件通过 `GET /events` 的 SSE 流接收。HTTP 传输仍然使用本插件的 JSON-RPC 协议，并不会使服务自动变成 MCP server。完整方法和消息格式见 [`protocol.md`](protocol.md)。
