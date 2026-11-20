# 动作指南（Actions Guide）

所有动作统一形状：`{"tool": "...", "arguments": {...}}`。坐标原点在左上角，x 向右、y 向下增大。指针坐标使用最近一次成功截图的画布；截图区域则使用桌面画布（详见 [坐标映射](coordinates.md)）。超出画布的指针坐标会被钳制到画布内。参数校验严格：未知参数、越界值、未知按键名都会报 `invalid_arguments`。

## screen.capture

截取桌面（或区域）为图像。

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `region` | object | 无 | `{x, y, width, height}` 桌面画布坐标区域；缺省为全桌面 |
| `format` | string | `png` | `png` / `jpeg`（jpeg 更省 token） |
| `quality` | int | 85 | JPEG 质量 1-100（png 忽略） |
| `scale` | float | 1.0 | 画布相对缩放 0.1-1.0，越小越省 token |
| `grayscale` | bool | false | 灰度化以省 token |
| `show_grid` | bool | false | 在返回的图像上绘制当前截图坐标系的网格和标签；必须是布尔值 |

返回：`frame`（成功截图的递增帧号）、`format`、`width/height`、`bytes`、`data_url`、`canvas` 和 `capture_geometry`。开启网格时还返回 `grid`，包括 `present: true`、自动选择的 x/y `interval` 和 `action_frame`；默认或 `false` 时没有 `grid`。网格绘制在返回图像中，不改变桌面本身。

图像左上角为 `(0, 0)`，网格坐标可直接用于后续 `pointer.*` 动作。每次成功截图都会替换指针动作坐标系，包括裁剪或缩放的截图；截图失败时保留上一次坐标系。首次成功截图前使用桌面画布。后续成功截图会改变坐标含义，帧号不会阻止使用过期坐标；桌面布局或目标变化后应重新截图。截图区域 `region` 始终以桌面画布为基准，而非上一张图像。

`capture_geometry.physical_bounds` 是实际截取的物理像素矩形；`image` 是返回图像尺寸。`action_frame` 给出帧号、`capture_relative` 类型、`virtual_desktop` 目标、物理原点、`image_to_action_scale`（两轴均为 1）和 `physical_pixels_per_unit`。指针坐标先钳制到图像范围，再乘以该比例并加上物理原点，得到桌面物理坐标；两轴都使用宽度计算的比例。裁剪会与桌面相交，`scale` 影响输出宽度，面积上限还可能缩小输出；请以返回的尺寸和元数据为准。详细映射及示例见 [坐标映射](coordinates.md)。

`show_grid` 非布尔值、区域完全在桌面外，或图像太小无法绘制可读网格时返回 `invalid_arguments`。截图后端不可用、不支持网格或无法确认截图与指针使用同一动作坐标系、或未确认网格已绘制时返回 `backend_unavailable`；后端截图失败可返回 `driver_failed`。

示例：`{"tool":"screen.capture","arguments":{"region":{"x":0,"y":0,"width":400,"height":300},"format":"jpeg","scale":0.5,"show_grid":true}}`

## pointer.move

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `x`, `y` | float | 必填 | 目标位置（画布坐标） |
| `steps` | int | 1 | 插值步数，>1 为平滑移动 |

## pointer.click

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `x`, `y` | float | 无 | 必须同时提供或同时省略；提供时先移动再点击，省略时在当前位置点击 |
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
| `x`, `y` | float | 无 | 必须同时提供或同时省略；提供时先移动再滚，省略时在当前位置滚动 |

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
