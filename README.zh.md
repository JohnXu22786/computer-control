[English](README.md)

# computer-control — 桌面控制插件（for dsh）

让 agent 直接操作电脑桌面：截屏观察、鼠标/键盘注入、通过可访问性树实现的语义操作（语义优先、像素坐标兜底），并内置急停、允许/拒绝规则、确认流与空闲待机等安全护栏。

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

## 在 dsh 中安装

```bash
dsh plugin --profile demo add github:JohnXu22786/computer-control
```

卸载：

```bash
dsh plugin --profile demo remove computer-control
```

### dsh bundle

仓库同时提供 Cordis bundle，凡是消费 `dsh.bundle` 清单的环境都可以同样方式安装：`package.json` 声明
`dsh.bundle.patch` 指向 `cordis.patch.yml`，`index.js` 即 dsh profile 加载的桥接层。它以子进程方式启动
`python -m computer_control serve`，把 `manifest.json` 中声明的全部工具经 stdio 协议重新暴露给 harness——
Python 核心保持不变。profile 级设置可通过 patch 行的 `config` 固定（例如 `platform.name: dry-run` 实现只演练安装）。

桥接层要求：`node >= 18`，且环境中装有 Python 3.9+ 并安装本包（核心工具只需 `pip install -r requirements.txt`）。

## 安装

要求：**Python 3.9+**，Windows 10/11（完整功能）；其他平台见「平台支持」。

```powershell
# 核心（截图 + 输入注入）
pip install -r requirements.txt

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

模型看到的是**画布**而非原始屏幕：截图被缩放到以 `display_width_px`（默认 1920）为宽的等比例画布，模型返回的坐标就在这个画布上；插件按 `scale = 物理宽 / 画布宽` 均匀映射回物理像素后执行。`screen.capture` 结果中带有 `canvas` 字段与 `display_width_px/display_height_px`，模型以此为准。多显示器（含主屏左侧/上方的负坐标区域）与每显示器 DPI 均已在执行层处理。

## 安全

详见 [`docs/configuration.md`](docs/configuration.md#safety) 与 README 下方要点：

- **急停（三重）**：默认全局热键 `Ctrl+Alt+F12`（配置 `safety.emergency_hotkey`，可置空禁用）；协议方法 `control.panic`；panic 文件（`safety.panic_file`，存在即停）。急停后所有动作返回 `safety_stopped`；`session.resume` 或再次按热键恢复。急停生效时桌面角落显示红色 STOP 横幅（`safety.visual_indicator`）。
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
