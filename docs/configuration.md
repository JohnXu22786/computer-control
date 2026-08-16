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
