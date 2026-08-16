# 协议说明（Protocol Reference）

JSON-RPC 2.0，行分隔（每行一个 JSON 对象），UTF-8。HTTP 传输见文末。

## 1. 方法总表

| 方法 | 参数 | 说明 |
| --- | --- | --- |
| `session.start` | 配置对象（可选） | 启动会话；返回 `{state, surface, capabilities}` |
| `session.stop` | — | 结束会话，释放驱动与看门狗 |
| `session.configure` | 可配置子集 | 运行时调整规则/阈值等 |
| `session.confirm` | `{request_id, approve}` | 批复待确认动作 |
| `session.resume` | — | 从 stopped/standby 恢复 |
| `control.panic` | `{on: bool}` | 程序化急停/解除 |
| `tools.call` | `{tool, arguments}` | 执行单个动作 |
| `tools.call_batch` | `{items, continue_on_error?, gap_ms?}` | 批量执行 |
| `tools.list` | — | 工具清单（含 available 能力广播） |
| `system.status` | — | 状态/能力/画布/待批信息 |

请求：

```json
{"jsonrpc": "2.0", "id": 1, "method": "tools.call", "params": {"tool": "pointer.click", "arguments": {"x": 960, "y": 540}}}
```

响应（无 `id` 的请求视为通知，不响应）：

```json
{"jsonrpc": "2.0", "id": 1, "result": {"ok": true, "result": {...}, "error": null, "meta": {...}}}
```

## 2. 错误码

### 协议级（JSON-RPC error 字段）

| code | 含义 |
| --- | --- |
| -32700 | 解析错误（非法 JSON） |
| -32600 | 请求不是合法对象 / 缺 jsonrpc 字段 |
| -32601 | 未知方法 |
| -32602 | 参数非法（非对象等） |
| -32000 | 内部错误 |

### 动作级（信封 error.code）

| code | 触发 |
| --- | --- |
| `not_started` / `already_started` | 会话未启动 / 重复启动 |
| `invalid_config` | 配置校验失败（含运行时不可改字段） |
| `unknown_tool` | 未知工具名 |
| `invalid_arguments` | 参数校验失败（`data.issues` 列出问题） |
| `policy_denied` | 命中拒绝规则 / 白名单外（`data.rule`） |
| `safety_stopped` | 急停生效中 |
| `safety_standby` | 空闲待机中 |
| `busy` | 有待批确认未处理 |
| `confirmation_not_found` / `confirmation_expired` | 确认 id 不存在 / 已超时 |
| `stale_snapshot` | a11y 动作引用了非最新 snapshot_id |
| `backend_unavailable` | 后端缺失（如无 UIA、平台无驱动） |
| `driver_failed` | 执行层失败（SendInput 失败等） |
| `timeout` | 动作超过内部执行窗口 |

## 3. 事件（通知）

```json
{"jsonrpc": "2.0", "method": "event", "params": {"type": "safety.panic_triggered", "payload": {"source": "hotkey"}}}
```

事件清单见 `manifest.json` 或 `docs/integration.md` 第 3 节。

## 4. 关键结果形状

### screen.capture

```json
{"ok": true, "result": {
  "frame": 3, "format": "png", "width": 1920, "height": 1080, "bytes": 153200,
  "data_url": "data:image/png;base64,...",
  "canvas": {"display_width_px": 1920, "display_height_px": 1080, "physical": {...}, "scale": 0.71}
}}
```

### 确认待批

```json
{"ok": true, "result": {"status": "awaiting_confirmation", "request_id": "cfm-3f9a2c1d0b", "risk": "high", "reason": "risk high >= confirm threshold high"}}
```

### a11y.snapshot

```json
{"ok": true, "result": {
  "snapshot_id": "snap-2", "node_count": 64, "truncated": false,
  "generated_at": 1755300000.0,
  "tree": {"id": 1, "role": "pane", "name": "Desktop", "children": [
    {"id": 2, "role": "window", "name": "记事本", "rect": [10, 10, 800, 600],
     "children": [{"id": 3, "role": "edit", "name": "文本编辑器", "rect": [12, 40, 780, 540]}]}]}
}}
```

### a11y.activate / a11y.input

```json
{"ok": true, "result": {"node_id": 3, "method_used": "invoke"}}
{"ok": true, "result": {"node_id": 3, "method_used": "pointer", "position": {"x": 400, "y": 300}}}
{"ok": true, "result": {"node_id": 3, "method_used": "value", "chars": 5}}
```

`method_used` 说明本次实际走的通道：`invoke/toggle/select`（语义模式）、`value`（语义输入）、`pointer/pointer_type`（像素兜底）。

### batch.execute

```json
{"ok": true, "result": {"status": "completed", "items": [<每个动作的信封>]}}
{"ok": true, "result": {"status": "aborted", "items": [<已完成部分的信封>]}}
{"ok": true, "result": {"status": "awaiting_confirmation", "request_id": "...", "risk": "high"}}
```

## 5. 传输

### stdio（默认）

`python -m computer_control serve`——stdin/stdout 行分隔 JSON；stderr 为日志。

### HTTP

`python -m computer_control serve --transport http --port 8765`

| 端点 | 说明 |
| --- | --- |
| `POST /rpc` | 请求体为单个 JSON-RPC 请求对象 |
| `GET /health` | 存活探针 |
| `GET /events` | SSE 事件流（先回放最近 200 条） |

## 6. 完整对话示例

```text
> {"jsonrpc":"2.0","id":1,"method":"session.start","params":{"capture":{"default_width":1920},"safety":{"confirm_threshold":"high"}}}
< {"jsonrpc":"2.0","id":1,"result":{"ok":true,"result":{"state":"ready", ...},"error":null,"meta":{}}}
< {"jsonrpc":"2.0","method":"event","params":{"type":"session.started","payload":{...}}}

> {"jsonrpc":"2.0","id":2,"method":"tools.call","params":{"tool":"screen.capture","arguments":{"format":"jpeg","scale":0.5}}}
< {"jsonrpc":"2.0","id":2,"result":{"ok":true,"result":{"frame":1,"format":"jpeg","width":960,"height":540,"data_url":"data:image/jpeg;base64,...",...},"error":null,"meta":{}}}

> {"jsonrpc":"2.0","id":3,"method":"tools.call","params":{"tool":"keyboard.combo","arguments":{"keys":["win","r"]}}}
< {"jsonrpc":"2.0","id":3,"result":{"ok":true,"result":{"status":"awaiting_confirmation","request_id":"cfm-...","risk":"high","reason":"..."},"error":null,"meta":{}}}
< {"jsonrpc":"2.0","method":"event","params":{"type":"safety.confirmation_requested","payload":{"request_id":"cfm-...","tool":"keyboard.combo","arguments":{"keys":["win","r"]},"risk":"high","timeout_s":30}}}

> {"jsonrpc":"2.0","id":4,"method":"session.confirm","params":{"request_id":"cfm-...","approve":true}}
< {"jsonrpc":"2.0","id":4,"result":{"ok":true,"result":{"request_id":"cfm-...","status":"approved"},"error":null,"meta":{}}}
< {"jsonrpc":"2.0","method":"event","params":{"type":"safety.confirmation_resolved","payload":{"request_id":"cfm-...","approve":true,"tool":"keyboard.combo"}}}
< {"jsonrpc":"2.0","method":"event","params":{"type":"action.started","payload":{"tool":"keyboard.combo","arguments":{"keys":["win","r"]}}}}
< {"jsonrpc":"2.0","method":"event","params":{"type":"action.finished","payload":{"tool":"keyboard.combo","ok":true,"duration_ms":82}}}
```

批复响应立即返回；被批准的动作经工作线程串行执行，事件顺序为 `confirmation_resolved` → `action.started` → `action.finished`。
