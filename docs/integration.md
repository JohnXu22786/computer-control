# dsh 接入说明（Integration Guide）

本文说明插件化 harness（dsh）如何加载本插件、调用动作、消费事件，以及确认流与安全事件如何呈现给用户。插件本身只需 `python -m computer_control serve`，其余全部由本协议定义。

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
| `batch.finished` | 批量完成 | 记录批量结果 |
| `safety.confirmation_requested` | 高风险动作待批 | **弹出人工确认 UI**（见下） |
| `safety.confirmation_resolved` | 已批复 | 关闭确认 UI |
| `safety.confirmation_expired` | 超时未批（自动拒绝） | 提示已拒绝 |
| `safety.panic_triggered` / `safety.panic_released` | 急停触发/解除 | 显著提示用户；急停期间禁止再发动作 |

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

## 7. 其他建议

- **启动时**用 `tools.list` 检查各工具 `available` 标志（例如 UIA 缺失时 `a11y.*` 为 false），据此裁剪模型可见的工具集——能力广播，避免模型调用注定失败的工具。
- 模型提示词建议包含：坐标必须在画布内；语义优先；每步截图自检；高风险动作会被人工确认，属正常现象。
- 超时防护：harness 侧 `tools.call` 超时应大于 `runtime.max_wait_ms`（默认 600s）+ 余量；超时后动作**可能仍在执行**（结果未知，非未执行），需以新截图确认状态。
- `session.stop` 语义：排队与后续动作立即被拒绝；**正在执行中的注入无法中途打断**（硬件事件流），会执行完毕后结束会话。
