# 配置说明（Configuration Reference）

配置为 JSON 对象，来源优先级：内置默认 < 配置文件（`--config` 可放在子命令之后，如 `python -m computer_control serve --config cfg.json`；或环境变量 `COMPUTER_CONTROL_CONFIG`）< `session.start` 参数 < `session.configure`（仅可运行时可调字段）。任何未知字段都会导致配置被拒绝（防拼写错误）。

## platform

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `name` | `auto` | `auto`（Windows 上选择真实驱动）/ `windows` / `dry-run`（演练，只记录不执行） |

## capture

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `default_width` | 1920 | 模型画布宽度（模型空间 `display_width_px`）；高度按物理纵横比导出 |
| `default_format` | `png` | 默认截图格式 |
| `default_quality` | 85 | 默认 JPEG 质量 |
| `grayscale` | false | 默认灰度化 |
| `max_area` | 5000000 | 单帧像素上限保护（token 成本保险丝） |
| `backend` | `auto` | `auto` / `mss` / `pillow` |

## safety

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `emergency_hotkey` | `ctrl+alt+f12` | 全局急停热键；空字符串禁用。**不可运行时修改** |
| `panic_file` | `""` | panic 文件路径；文件存在即急停（跨进程/外部监控用）。**不可运行时修改** |
| `visual_indicator` | true | 急停时桌面角落红色 STOP 横幅（tkinter，可用性自动探测） |
| `confirm_threshold` | `high` | 达到该风险等级的动作需人工确认：`benign` / `moderate` / `high` |
| `confirm_timeout_s` | 30 | 确认超时（0.01~3600），超时自动拒绝 |
| `idle_timeout_s` | 0 | 空闲超时；0 = 禁用 |
| `idle_action` | `standby` | 空闲后行为：`standby`（停摆拒动作）/ `none`（仅发事件） |
| `default_rule` | `allow` | 默认放行 / `deny`（白名单模式） |
| `rules` | `[]` | 规则列表（见下） |
| `auth_profile` | `full` | 授权模式（能力阶梯，由宽到窄：`full` → `limited` → `read_only` → `revoked`）：`full` 不收窄；`limited` 额外拒绝所有 `keyboard.*` 与 `a11y.input`（向焦点节点注入文本等价于键盘输入）；`read_only` 恒放行 `screen.capture`，`a11y.snapshot` 仅在宿主 opt-in（`a11y_snapshot_opt_in`）后放行；`revoked` 与 `read_only` 共享同一观察底线，但进入时取消待确认请求，且带内无法自行走出。被拒动作返回 `not_authorized`。**独立于操作状态的正交轴**：只收窄可通过安全门的工具集，不改变 `ready`/`standby`/`stopped`，也不触发急停，因此 `revoked` 会话的 `state` 仍是 `ready`。可运行时修改，但带内只能降（见下） |
| `a11y_snapshot_opt_in` | `false` | **宿主专用键**：`read_only` / `revoked` 下是否放行 `a11y.snapshot`。只能经宿主配置文件或带外控制文件通道（`a11y_snapshot` 指令）设置；带内（`session.start` 配置或 `session.configure` 补丁）出现即返回 `invalid_config` |

### 规则格式

```json
{"safety": {"rules": [
  {"match": {"tool": "keyboard.combo", "argument": {"name": "keys", "matcher": "contains", "value": "win"}}, "effect": "deny"},
  {"match": {"tool": "screen.*"}, "effect": "allow"}
]}}
```

- `match.tool`：支持 `*` 通配（`keyboard.*`、`*`）。
- `match.argument`：可选，按参数匹配：
  - `matcher: "equals"`：值相等（任意类型；不做修饰键别名归一）
  - `matcher: "glob"`：字符串通配（如 `text: "rm -rf*"`；不做修饰键别名归一）
  - `matcher: "contains"`：字符串包含，或列表元素包含；修饰键的别名视为同一族（如规则写 `"win"` 可命中 `lwin/rwin/super/meta`，`"ctrl"` 可命中 `lctrl/rctrl`，`"alt"` 可命中 `lalt/ralt`）。**针对修饰键的规则请使用 contains。**
- 求值：**显式 deny 永远优先**（无论规则顺序）；`default_rule: "deny"` 时未命中任何 allow 也拒绝。
- 规则不豁免确认流——确认阈值是独立的安全层，刻意不可用规则绕过。
- 授权模式（`auth_profile`）在规则扫描**之前**求值：已被模式拒绝的动作返回 `not_authorized`，即使存在会命中的 deny 规则；而 `read_only` / `revoked` 观察白名单内的动作仍继续受 deny 规则管辖。批量内层动作走同一求值点，无法借批量绕过授权模式。

### 推荐起始配置

```json
{
  "safety": {
    "confirm_threshold": "moderate",
    "rules": [
      {"match": {"tool": "keyboard.type", "argument": {"name": "text", "matcher": "glob", "value": "rm -rf*"}}, "effect": "deny"},
      {"match": {"tool": "keyboard.combo", "argument": {"name": "keys", "matcher": "contains", "value": "win"}}, "effect": "deny"}
    ]
  }
}
```

## a11y

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `default_level` | `standard` | 默认摘要等级 |
| `max_name_len` | 64 | 名称截断上限 |
| `include_rects` | true | 摘要默认携带包围盒 |
| `hard_walk_cap` | 5000 | 原始树遍历硬上限（防超大树拖垮） |

## runtime

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `batch_gap_ms` | 150 | 批量项间默认停顿 |
| `max_wait_ms` | 600000 | `wait.pause` 上限（超限截断） |

## 完整示例

见 [`examples/config.example.json`](../examples/config.example.json)。

## 运行时不可修改的字段

`platform.*`、`safety.emergency_hotkey`、`safety.panic_file`、`safety.visual_indicator`、`capture.backend`——这些在 `session.start` 时固化；`session.configure` 传这些字段会返回 `invalid_config`。

## 授权模式的运行时变更（带内降权 / 带外提权）

`safety.auth_profile` **不在**上面的不可变列表里，但它只能在带内**单向收窄**：

- `session.configure` 是 agent 可达的调用面，因此这里的 `auth_profile` 补丁只允许沿阶梯 `full → limited → read_only → revoked` 降级或保持不变；任何变宽（如 `read_only → full`，甚至 `revoked → read_only`）返回 `invalid_config`。
- 提权只能由宿主带外完成，有两条 agent 不可达的路径：进程内直接调用 `Session.grant_profile(new_profile, actor="host")`，可任意方向变更；插件以 stdio 子进程运行时（dsh 场景，宿主调不到 Python 方法）改写**控制文件通道**——启动时经 `serve --control-file PATH` 或环境变量 `COMPUTER_CONTROL_CONTROL_FILE` 指定，运行期轮询。两者**刻意不进 JSON-RPC 路由表**——凡可路由即可被 agent 触达，所以路由表里不存在任何能扩权的入口。`actor` 只是审计事件标签，扩权资格来自「宿主自己发起了这次变更」，不来自参数。`a11y_snapshot_opt_in` 同理为宿主专用：带内任何位置出现该键都返回 `invalid_config`。
- 控制文件通道的指令格式（宿主**原子替换**整个文件：同目录写临时文件后 `os.replace`）：
  ```json
  {"seq": 7, "auth_profile": "limited", "a11y_snapshot": true}
  ```
  - `seq`（必填，正整数，严格递增）：只有比上一次已生效的 `seq` 更大才生效——重授同一档位也要抬 seq；旧内容重放是无害的空操作。
  - `auth_profile` / `a11y_snapshot`（可选）：档位可任意方向变更；`a11y_snapshot` 即 `a11y_snapshot_opt_in` 的开关。
  - 内容非法（缺 seq、未知键、非法档位等）直接忽略且**不消耗 seq**，宿主可原 seq 改写重试；无会话在运行时指令**挂起待用**，落到下一个启动的会话；**删除文件不等于收回授权**——收回须显式写一条降档指令。通道只读不写，路径不出现在任何配置 schema、路由表、manifest 或事件里，agent 无法指名也无法重定向。
- `session.start` 同样属于带内，因此 **stop + start 也不是逃生门**：重启时会拿宿主自己的配置文件（`base_config`，即 `--config` / `COMPUTER_CONTROL_CONFIG`）里声明的 `auth_profile` 作为上限，请求比它更宽返回 `invalid_config`。宿主未声明限制时上限即默认值 `full`，此时不受约束。运行时提权**不跨重启保留**，重启后回落到配置文件的上限。
- 带内降权是**粘性的**：同一 `Session` 实例上，agent 降到过的档位会成为之后所有 `session.start` 的额外上限——重启时即使**不传** `auth_profile`，会话也以该档位（或更低）启动，而不是静默重新加载配置文件里更宽的档位；显式请求比粘性档位更宽同样返回 `invalid_config`。宿主调用 `grant_profile` 后，粘性上限更新为宿主授予的档位。`session.start` 的结果与 `session.started` 事件携带生效的 `auth_profile`，供宿主核对。
- 每次生效的模式变更都会发出 `safety.grant_changed` 事件（`from` / `to` / `actor` / `widened`）。
- 降级（含进入 `revoked`）会取消当前待确认请求中**不再被新档位放行**的项（发 `safety.confirmation_resolved`，`approve=false`），先前收集的批准不会再执行；进入 `revoked` 无条件取消待确认请求。对挂起的 `batch.execute` 会下探到其 payload 里的子动作：混合批中任一子动作被新档位拒绝（例如降级到 `limited` 后批内仍有 `keyboard.*`），整个批量确认即被取消——单查 `batch.execute` 本身是查不出来的。此外每个动作在真正执行前都会重新过门（`evaluate_allow_only`），且**最终检查与派发在同一把 admission 锁内完成**，`session.configure` / `grant_profile` 不可能插在两者之间：要么动作在降权前执行完，要么被降权拦截，不会出现「先降权、后执行」。被拒的已批准动作发 `action.finished`，`error_code=not_authorized`。`revoked` 不触发急停、不改 `state`，`screen.capture` 仍可运行（`a11y.snapshot` 需宿主 opt-in）。admission 锁只覆盖**单个动作**的最终检查与派发：批量每个 item 各自过锁，因此降级可以落在批量 item **之间**，让后续 item 被拒而不是整批逃脱检查。
- 已知代价：agent 可以自我降权直至 `revoked`，把自身会话锁在只观察档位，需要宿主才能解锁。这是降权，不构成提权。
