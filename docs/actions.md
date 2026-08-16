# 动作指南（Actions Guide）

所有动作统一形状：`{"tool": "...", "arguments": {...}}`。坐标均为**模型画布坐标**（见 README「坐标契约」）；超出画布的值会被钳制到画布内。参数校验严格：未知参数、越界值、未知按键名都会报 `invalid_arguments`。

## screen.capture

截取桌面（或区域）为图像。

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `region` | object | 无 | `{x, y, width, height}` 模型坐标区域；缺省为全桌面 |
| `format` | string | `png` | `png` / `jpeg`（jpeg 更省 token） |
| `quality` | int | 85 | JPEG 质量 1-100（png 忽略） |
| `scale` | float | 1.0 | 画布相对缩放 0.1-1.0，越小越省 token |
| `grayscale` | bool | false | 灰度化以省 token |

返回：`frame`（单调递增帧号）、`width/height`、`bytes`、`data_url`、`canvas`。

示例：`{"region": {"x": 0, "y": 0, "width": 400, "height": 300}, "format": "jpeg", "scale": 0.5}`

## pointer.move

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `x`, `y` | float | 必填 | 目标位置（画布坐标） |
| `steps` | int | 1 | 插值步数，>1 为平滑移动 |

## pointer.click

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `x`, `y` | float | 无 | 先移动到此再点击；缺省在当前位置点击 |
| `button` | string | `left` | `left` / `middle` / `right` |
| `times` | int | 1 | 1/2/3（单击/双击/三击） |
| `hold_ms` | int | 0 | 按下后保持毫秒数（长按） |

## pointer.drag

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `from`, `to` | object | 必填 | `{x, y}` 起止位置 |
| `button` | string | `left` | 按住哪个键 |
| `steps` | int | 24 | 移动插值步数 |
| `hold_ms` | int | 0 | 到终点后保持 |

## pointer.scroll

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `axis` | string | `vertical` | `vertical` / `horizontal` |
| `amount` | float | 必填 | 滚轮格数，正上/右、负下/左（-100~100） |
| `x`, `y` | float | 无 | 先移动到此再滚 |

## keyboard.press

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `key` | string | 必填 | 按键名，如 `enter`、`f5`、`a`、`1`、`left` |

支持：字母、数字、F1-F24、方向键/导航键、`enter/esc/tab/space/backspace/delete/insert/home/end/pageup/pagedown`、修饰键（`ctrl/alt/shift/win` 及左右变体）、常用符号（`; , - . / ` [ \ ] ' =`）、`numpad*`、`printscreen/scrolllock/capslock/numlock/pause` 等。未知按键名报错。

## keyboard.combo

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `keys` | list/string | 必填 | `["ctrl","shift","esc"]` 或 `"ctrl+shift+esc"` |

- 按下顺序按列表，释放逆序。
- **风险升级**：含 `win`（任意）或同时含 `ctrl`+`alt` 的组合 → 高风险，触发确认流（`confirm_threshold` 默认 `high`）。

## keyboard.type

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `text` | string | 必填 | 文本（UTF-8，≤10000 字符） |
| `submit` | bool | false | 输入后按回车 |
| `interval_ms` | int | 0 | 字符间延迟 |

- 文本经 Unicode 键事件注入，不依赖剪贴板，任意语言/emoji 可用。
- `\n` → Enter，`\t` → Tab。

## wait.pause

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `ms` | int | 必填 | 毫秒（0~600000） |

## a11y.snapshot

可访问性树的分级摘要（模型视角的「语义桌面」）。

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `level` | string | `standard` | `skeleton`（浅）/ `standard` / `full`（深） |
| `depth` | int | 按 level | 覆盖树深度（含根） |
| `max_nodes` | int | 按 level | 覆盖节点数上限 |
| `include_rects` | bool | true | 是否携带包围盒（像素兜底需要） |

等级预设：

| level | depth | max_nodes | 名字截断 |
| --- | --- | --- | --- |
| skeleton | 2 | 80 | 32 字符 |
| standard | 4 | 400 | 64 字符 |
| full | 12 | 2000 | 256 字符 |

节点形状：`{"id", "role", "name", "rect": [l,t,r,b]?, "children": [...]?}`。`role` 为可读控件类型（button/edit/window/pane/listitem…）。`truncated` 为 true 表示节点预算耗尽被裁剪（深度裁剪属正常）。

**重要**：节点 id 仅在**当前快照**内有效。任何 `a11y.activate/a11y.input` 都必须使用最近一次 `a11y.snapshot` 返回的 `snapshot_id`，否则返回 `stale_snapshot`。

## a11y.activate

语义激活一个元素。

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `snapshot_id` | string | 必填 | 最近一次快照的 id |
| `node_id` | int | 必填 | 快照中的元素 id |
| `method` | string | `auto` | `auto`（模式优先）/ `pattern`（仅模式）/ `pointer`（跳过模式直接像素点击） |

执行顺序：Invoke 模式 → Toggle 模式 → SelectionItem 模式 → 包围盒中心像素点击（返回 `method_used` 表明实际通道）。

## a11y.input

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `snapshot_id` | string | 必填 | 最近一次快照的 id |
| `node_id` | int | 必填 | 元素 id |
| `text` | string | 必填 | 输入文本 |

执行顺序：Value 模式（`SetValue`）→ 像素兜底（点击元素中心 + 键盘输入，`method_used: "pointer_type"`）。

## batch.execute

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `items` | list | 必填 | `[{tool, arguments}, ...]` |
| `continue_on_error` | bool | false | 单项失败后是否继续 |
| `gap_ms` | int | 150 | 项间停顿 |

- 预扫描阶段逐项校验与过闸：非法参数/未知工具/策略拒绝 → 单项错误结果，不影响其他项。
- 任一项需要确认 → 整批进入 `awaiting_confirmation`，一次批复执行整批。
- 执行阶段（或批准后）每项仍会过安全门（拒绝规则、急停、待机实时生效）。
- `continue_on_error: false` 时首个失败项后终止，状态 `aborted`。
