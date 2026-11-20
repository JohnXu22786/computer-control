[English](README.md)

# computer-control — 桌面控制插件（for dsh）

让 agent 直接操作电脑桌面：截屏观察、鼠标/键盘注入、通过可访问性树实现的语义操作（语义优先、像素坐标兜底），并内置急停、授权模式、允许/拒绝规则、确认流与空闲待机等安全护栏。

插件自包含、可被 dsh 直接加载：通过 `manifest.json` 声明工具与事件，以**行分隔 JSON-RPC 2.0**（stdio）为传输协议，`python -m computer_control serve` 即入口。

---

## 功能总览

| 能力 | 说明 |
| --- | --- |
| 截图 `screen.capture` | 全屏或区域截图，支持 PNG/JPEG、缩放、灰度——token 成本可控 |
| 鼠标 `pointer.*` | 移动、左/中/右键单击（含双击/三击）、拖拽、滚轮（横/纵） |
| 键盘 `keyboard.*` | 单键、组合键（扫描码注入，与键盘布局无关）、任意 Unicode 文本输入 |
| 等待 `wait.pause` | 动作间暂停，让界面稳定后再截图 |
| 语义操作 `a11y.*` | 可访问性树分级摘要（skeleton/standard/full）、语义激活/输入——先走 UIA 模式，失败自动降级为包围盒像素点击 |
| 批量 `batch.execute` | 一次调用执行多个动作，减少模型往返；可整批确认、可遇错继续 |
| 能力广播 | `tools.list`/`system.status` 报告各后端是否可用，语义工具仅在 UIA 可用时暴露 |

安全护栏（详见「安全」一节）：

- **急停**：全局热键（默认 `Ctrl+Alt+F12`，可配）、协议指令、panic 文件三重触发；屏幕角落显示 STOP 横幅
- **授权模式**：`read_only` / `limited` / `full` / `revoked` 收窄可通过安全门的工具集——它独立于操作状态；运行时可带内降权，只有宿主能通过带外控制路径授权变宽
- **允许/拒绝规则**：按工具名与参数匹配，拒绝永远优先；可切换白名单模式
- **确认流**：高风险动作（含 `win` 键或 `ctrl+alt` 的组合键等）等待人工批准，超时自动拒绝
- **空闲待机**：无操作超过阈值自动进入待机，拒绝一切动作直到恢复
- **演练模式**：`platform: "dry-run"` 只记录不执行，用于安全排练

## 目录结构

```
computer-control/
├── manifest.json            # dsh 插件清单（工具/事件/入口/传输）
├── pyproject.toml           # 打包元数据与依赖声明
├── requirements.txt         # 核心依赖（仅 Pillow）
├── requirements-optional.txt# 可选能力依赖（UIA/mss/热键）
├── README.md
├── docs/                    # integration / protocol / actions / configuration
├── examples/                # 配置示例、会话示例、演示脚本
├── computer_control/        # 插件实现（Python 包）
│   ├── cli.py __main__.py   # 入口：serve / check / list
│   ├── session.py           # 会话生命周期与动作串行执行
│   ├── engine.py            # 动作执行引擎（坐标映射、事件）
│   ├── policy.py            # 安全门：规则/确认/急停/看门狗
│   ├── actions.py           # 动作注册表与参数校验
│   ├── geometry.py          # 模型画布 <-> 物理像素 映射
│   ├── protocol.py server.py# JSON-RPC 路由与 stdio/HTTP 传输
│   ├── client.py            # 供 harness/脚本使用的客户端
│   ├── overlay.py           # 急停可视横幅（可选，tkinter）
│   ├── drivers/             # 执行层抽象 + windows 实现 + 演练驱动
│   └── a11y/                # 可访问性树摘要 + Windows UIA 桥
└── tests/                   # 纯逻辑与协议测试（不触碰真实硬件）
```

## 在 dsh 中使用

```bash
dsh plugin --profile demo add github:JohnXu22786/computer-control
```

将 `demo` 替换成目标 profile 名称即可。

仓库同时提供 Cordis bundle：`package.json` 指向 `cordis.patch.yml`，`index.js`
负责启动 Python 服务并把工具暴露给 dsh。该 bundle 需要 **Node.js 18.17+** 和
**Python 3.9+**。profile 级设置可以写在 bundle patch 中（例如设置
`platform.name: dry-run` 进行演练）。请将 Python 包安装到 dsh 实际使用的 Python
环境中。安装已发布的
`v0.1.1` Python 包：

```bash
python -m pip install "git+https://github.com/JohnXu22786/computer-control.git@v0.1.1"
python -m computer_control check
```

也可以安装仓库当前默认分支的源码：

```bash
python -m pip install git+https://github.com/JohnXu22786/computer-control.git
```

卸载插件：

```bash
dsh plugin --profile demo remove computer-control
```

### 在 Codex、Claude Code 或其他 harness 中使用

**目前 Codex 和 Claude Code 尚不能直接接入本插件。** 它们通常通过 MCP
连接外部工具，而 computer-control 使用自定义的行分隔 JSON-RPC 2.0 协议。
仓库中的 dsh `index.js` 是 Cordis 适配器，不是 MCP server；不能把
`python -m computer_control serve` 直接填进 MCP server 配置。

支持自定义 stdio 工具的 harness 可以自行接入该协议，启动命令为：

```bash
python -m computer_control serve
```

宿主需要在启动时发送 `session.start`，把 `tools.call` 或
`tools.call_batch` 映射为模型工具，转发服务端事件，并在会话结束时发送
`session.stop`。宿主还需要把 `screen.capture` 返回的 `data_url` 转为模型可读取的
图片。高风险动作触发 `safety.confirmation_requested` 时，应向用户展示确认请求；
只有用户明确批准后才能发送 `session.confirm`，不可自动批准。stdio 协议示例见
[`docs/protocol.md`](docs/protocol.md)，生命周期和事件处理见
[`docs/integration.md`](docs/integration.md)。

服务也可通过 HTTP 暴露自定义 JSON-RPC API：

```bash
python -m computer_control serve --transport http --port 8765
```

请求发往 `POST /rpc`，事件通过 `GET /events` 的 SSE 流接收。这仍然是插件自己的
JSON-RPC 协议，并非 MCP。要把它作为 Codex 或 Claude Code 的 MCP 工具使用，仍需单独的
MCP 适配器来转换工具调用和会话生命周期，并正确传递图片结果与人工确认流程。

完整 dsh 生命周期、事件和确认流说明见 [`docs/integration.md`](docs/integration.md)。

## 安装

要求：**Python 3.9+**，Windows 10/11（完整功能）；其他平台见「平台支持」。

```powershell
# 核心（截图 + 输入注入）
python -m pip install .

# 可选能力（推荐）：
#   comtypes  -> 可访问性树（UIA）语义操作
#   mss       -> 更快的多显示器截图后端
pip install -r requirements-optional.txt
```

自检环境：

```powershell
python -m computer_control check
```

输出平台、DPI 模式、虚拟桌面几何、截图后端、UIA 可用性、热键等诊断。

## 快速开始

```powershell
# 查看声明的工具与事件
python -m computer_control list

# 启动插件服务（stdio，供 dsh 加载）
python -m computer_control serve
```

用 `examples/demo.py` 跑一个演练会话（默认 `dry-run`，不触碰真实桌面）：

```powershell
python examples/demo.py
```

## dsh 接入（摘要）

完整接入说明见 [`docs/integration.md`](docs/integration.md)。

1. **加载**：dsh 读取 `manifest.json`，按 `entry.command` 启动进程，建立 stdio 管道（UTF-8，一行一个 JSON 对象）。
2. **生命周期**：先发 `session.start`（可带配置）→ 收到 `session.started` 事件后即可调用工具；结束发 `session.stop`。
3. **调用动作**：`tools.call`（单个）或 `tools.call_batch`（批量）。响应统一为 `{ok, result, error, meta}` 信封。
4. **事件**：服务端以 `event` 通知推送 `action.started/finished`、`safety.confirmation_requested` 等。
5. **确认流**：高风险动作返回 `awaiting_confirmation` 并发出确认事件；harness 应弹出人工确认，再以 `session.confirm` 批复；超时自动拒绝。

模型调用动作的推荐循环：`screen.capture` 观察 → 用画布坐标执行 `pointer.*`/`a11y.*` → `wait.pause`（如需）→ 新截图验证结果。

## 动作指南（摘要）

全部动作、参数与示例见 [`docs/actions.md`](docs/actions.md)。

| 动作 | 作用 | 风险 |
| --- | --- | --- |
| `screen.capture` | 截图（区域/格式/缩放/灰度） | 无 |
| `pointer.move` | 移动指针 | 中 |
| `pointer.click` | 单击/双击/三击，可选位置 | 中 |
| `pointer.drag` | 按住拖拽 | 中 |
| `pointer.scroll` | 滚轮（横/纵） | 中 |
| `keyboard.press` | 单键 | 中 |
| `keyboard.combo` | 组合键（含 win 或 ctrl+alt 时升级为高） | 中/高 |
| `keyboard.type` | 文本输入（Unicode） | 中 |
| `wait.pause` | 暂停 | 无 |
| `a11y.snapshot` | 可访问性树分级摘要 | 无 |
| `a11y.activate` | 语义激活（模式优先，像素兜底） | 中 |
| `a11y.input` | 语义文本输入（Value 模式优先） | 中 |
| `batch.execute` | 批量执行 | 取各项最大风险 |

### 坐标契约

模型看到的是**画布**而非原始屏幕：每次 `screen.capture` 的 `canvas.display_width_px` 和 `canvas.display_height_px` 给出返回图像的实际尺寸，后续指针坐标使用该画布。区域截图和可选的 `scale` 缩放也遵循此规则；插件将图像坐标映射回对应的物理区域。执行层处理多显示器（包括主屏左侧或上方的负坐标区域）及每显示器 DPI。截图的 `region` 使用桌面画布坐标，而非上一次裁剪的坐标；裁剪、舍入和示例详见[坐标映射指南](docs/coordinates.md)。

例如，请求网格，查看返回图像，然后点击图像坐标 `(100, 100)` 处的目标（假设该点在图像内）：

```json
{"tool": "screen.capture", "arguments": {"show_grid": true}}
{"tool": "pointer.click", "arguments": {"x": 100, "y": 100}}
```

网格标签使用最近一次成功截图的图像坐标，区域截图或缩放后也是如此。检查截图结果中 `grid.action_frame` 与 `capture_geometry.action_frame` 是否一致；两者描述的都是相对于截图的动作坐标系。网格只绘制在返回的图像中，不会在物理桌面上显示覆盖层。点击后再次截图以验证结果。

## 安全

详见 [`docs/configuration.md`](docs/configuration.md#safety) 与 README 下方要点：

- **急停（三重）**：默认全局热键 `Ctrl+Alt+F12`（配置 `safety.emergency_hotkey`，可置空禁用）；协议方法 `control.panic`；panic 文件（`safety.panic_file`，存在即停）。急停后所有动作返回 `safety_stopped`；`session.resume` 或再次按热键恢复。急停生效时桌面角落显示红色 STOP 横幅（`safety.visual_indicator`）。
- **授权模式**（`safety.auth_profile`，默认 `full`）：一条由宽到窄的能力阶梯。`full` 不做收窄；`limited` 在此基础上额外拒绝所有 `keyboard.*` 动作与 `a11y.input`（向焦点节点注入文本等价于键盘输入）；`read_only` 恒放行 `screen.capture`，`a11y.snapshot` 仅在宿主显式 opt-in（`safety.a11y_snapshot_opt_in`）后放行；`revoked` 与 `read_only` 共享同一观察底线，但进入的那一刻会取消待确认请求。被拒动作返回 `not_authorized`。**它是独立于上述操作状态的正交轴**，而非第五个状态：只缩小能通过安全门的工具集合；撤销授权只影响授权，保留当前操作状态（`ready` / `standby` / `stopped`），且不触发急停。该检查位于允许/拒绝规则扫描**之前**，且普通动作与批量内层动作共用同一求值点，因此无法借批量绕过授权模式。
  agent 可通过 `session.configure` 收窄或维持 `auth_profile`；任何带内变宽（包括 `revoked → read_only`）都会返回 `invalid_config`。宿主可直接调用 `Session.grant_profile(new_profile, actor="host")`，或在 stdio 子进程场景写入启动时指定的控制文件（`serve --control-file PATH` / `COMPUTER_CONTROL_CONTROL_FILE`）；这两条路径均不在 JSON-RPC 路由表中。`a11y_snapshot_opt_in` 也是宿主专用键。agent 的 `session.stop` / `session.start` 不能扩权：同一 `Session` 重启后的有效档位受宿主配置上限和最近有效档位共同约束；宿主授权高于配置上限时，重启会回到配置上限。宿主通过 `a11y_snapshot` 指令设置的 opt-in/opt-out 会在同一 `Session` 的重启间保留。

  控制文件格式为 `{"seq": N, "auth_profile": "...", "a11y_snapshot": bool}`；每条指令都是完整期望状态快照，修改其中一项时也要重复写入另一项的当前值。`seq` 只在当前服务进程内递增；进程重启后计数清零，并重新应用仍存在的完整快照。删除文件不会改变正在运行的会话；若新进程启动时文件不存在，会话从宿主配置开始。Linux 下控制文件和父目录须由 root 拥有，不可被 group/other 写入，访问/默认 POSIX ACL 也不得向其他主体授予有效写权限；插件必须以非特权用户运行且只有读取权限，每个进程线程的 permitted、effective、ambient 与 inheritable capability 集都必须为空，且插件线程的 CapBnd 不得包含 CAP_CHOWN、CAP_DAC_OVERRIDE、CAP_FOWNER、CAP_SETUID、CAP_SYS_PTRACE 或 CAP_SYS_ADMIN（前四项可绕过文件属主/权限检查或将文件系统 UID 切换为 root；CAP_SYS_PTRACE 可控制同一用户命名空间内的进程，包括可信的宿主控制文件写入者；CAP_SYS_ADMIN 可将攻击者控制的文件系统挂载到控制路径上）；无法验证 NoNewPrivs=1、capability 集或 CapBnd 时会拒绝启用控制通道。为防止同 UID 进程利用 setuid-root 程序，登录/会话管理器须在启动整个交互会话（含桌面、终端和启动器）前设置 NoNewPrivs 并移除危险 bounding-set 位。运行时只检查插件线程，不验证同 UID 的兄弟进程或未来进程；若要保护这些进程，须采用可强制执行的系统级隔离。Windows 下文件和父目录须由 LocalSystem 或内置 Administrators 所有，写权限仅授予这些主体；插件必须以未提升权限运行。其他 POSIX 平台因无法验证扩展 ACL 会拒绝启用控制通道。程序会检查并拒绝不安全路径。不要给插件的交互式 OS 身份 sudo/管理员权限：否则 agent 可通过桌面输入打开 shell 并伪造宿主指令。档位变更发出 `safety.grant_changed`；纯 a11y 开关通过 `session.configured` 报告。降级会取消不再满足新档位的待确认请求（对 `batch.execute` 会下探到子动作）；每个动作在执行前都会重新过门，批准后才被降权/收回的动作不会执行。已知代价：agent 可以把自身一路降到 `revoked`，之后必须由宿主解锁。
- **允许/拒绝规则**（`safety.rules`）：`{match: {tool: "keyboard.*", argument: {name, matcher, value}}, effect: "deny"}`；拒绝规则永远优先于允许。`safety.default_rule: "deny"` 可切换为白名单模式（未显式允许的动作一律拒绝）。规则可运行时通过 `session.configure` 调整。
- **确认流**：`safety.confirm_threshold`（默认 `high`）决定哪些风险等级需要人工批准；`safety.confirm_timeout_s`（默认 30s）超时自动拒绝。批准后动作照常执行并发出 `action.finished`。
- **空闲待机**：`safety.idle_timeout_s` 大于 0 时启用，无操作超时进入待机（`session.idle` 事件），`session.resume` 恢复；`idle_action: "none"` 则只发事件不停摆。
- **演练模式**：`platform: "dry-run"` 下一切动作只记录不执行，便于接入联调与安全排练。

## 平台支持

| 平台 | 驱动 | 说明 |
| --- | --- | --- |
| Windows | `drivers/windows.py` | 完整实现：SendInput 扫描码注入、每显示器 DPI 感知、虚拟桌面坐标、mss/Pillow 截图、UIA 语义层 |
| macOS / Linux | 接口已抽象 | `drivers/base.py` 定义了完整驱动契约（capture/pointer/keys/a11y/hotkey）；按契约实现对应平台驱动即可接入（`drivers/windows.py` 提供了完整示例）。未实现前 `platform: "auto"` 会给出明确报错 |
| 任何平台 | `drivers/dummy.py` | 演练驱动：记录一切动作，不触碰硬件 |

**依赖与降级**：

- `Pillow`（必需）：截图与编码。缺失时插件拒绝启动。
- `mss`（可选）：Windows 上更快、多显示器更可靠；缺失自动回退 Pillow ImageGrab。
- `comtypes`（可选）：UIA 语义操作。缺失时 `a11y.*` 工具在 `tools.list` 中标记为不可用，调用返回 `backend_unavailable`——像素坐标路径（截图+点击）不受影响。
- `keyboard`（可选，预留）：macOS/Linux 驱动的全局热键将依赖它；当前仅 Windows 与演练驱动随插件发布，Windows 内置 `GetAsyncKeyState` 轮询，无需该包。

**已知限制**：

- 安全注意序列（如 `Ctrl+Alt+Del`）无法通过输入注入触发——系统级保护，插件同样无法绕过。
- UIA 依赖目标程序暴露可访问性接口；不暴露的程序（部分游戏、自绘 UI）只能走像素路径。
- 键盘扫描码注入对 DirectInput/raw input 程序更友好，但仍可能被部分反作弊类程序拒绝（属正常防护行为）。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试全部使用纯逻辑与演练驱动，不注入真实输入、不触碰真实硬件；Windows 真实链路由 `python -m computer_control check` 与 `examples/demo.py --live` 人工验证。
