[简体中文](README.zh.md)

# computer-control — Desktop Control Plugin (for dsh)

Lets an agent operate the computer desktop directly: screenshot observation, mouse/keyboard injection, semantic actions via the accessibility tree (semantics first, pixel-coordinate fallback), with built-in safety guards — emergency stop, allow/deny rules, confirmation flow and idle standby.

The plugin is self-contained and loadable by dsh directly: it declares tools and events via `manifest.json`, speaks **line-delimited JSON-RPC 2.0** (stdio) as its transport protocol, and `python -m computer_control serve` is the entry point.

---

## Feature Overview

| Capability | Description |
| --- | --- |
| Screenshot `screen.capture` | Full-screen or region capture, PNG/JPEG, scaling, grayscale — token costs under control |
| Mouse `pointer.*` | Move, left/middle/right click (double/triple), drag, scroll (horizontal/vertical) |
| Keyboard `keyboard.*` | Single keys, chords (scancode injection, layout-independent), arbitrary Unicode text input |
| Wait `wait.pause` | Pause between actions so the UI can settle before the next screenshot |
| Semantic actions `a11y.*` | Hierarchical accessibility-tree summaries (skeleton/standard/full), semantic activate/input — UIA mode first, automatic fallback to bounding-box pixel clicks |
| Batch `batch.execute` | Run several actions in one call to reduce model round trips; confirmable as a whole, can continue on error |
| Capability broadcast | `tools.list` / `system.status` report per-backend availability; semantic tools are only exposed when UIA is available |

Safety guards (see the "Safety" section):

- **Emergency stop**: triple trigger — global hotkey (default `Ctrl+Alt+F12`, configurable), protocol command, panic file; a STOP banner shows in the screen corner
- **Allow/deny rules**: matched by tool name and arguments, deny always wins; whitelist mode switchable
- **Confirmation flow**: high-risk actions (including chords with `win` or `ctrl+alt`) wait for human approval, auto-denied on timeout
- **Idle standby**: no activity past a threshold puts the session into standby, rejecting all actions until resumed
- **Dry-run mode**: `platform: "dry-run"` logs actions without executing them, for safe rehearsals

## Directory Structure

```
computer-control/
├── manifest.json            # dsh plugin manifest (tools/events/entry/transport)
├── pyproject.toml           # packaging metadata and dependency declarations
├── requirements.txt         # core dependencies (Pillow only)
├── requirements-optional.txt# optional capability deps (UIA/mss/hotkey)
├── README.md
├── docs/                    # integration / protocol / actions / configuration
├── examples/                # config examples, session examples, demo scripts
├── computer_control/        # plugin implementation (Python package)
│   ├── cli.py __main__.py   # entry: serve / check / list
│   ├── session.py           # session lifecycle and serial action execution
│   ├── engine.py            # action execution engine (coordinate mapping, events)
│   ├── policy.py            # safety gate: rules/confirmation/panic/watchdog
│   ├── actions.py           # action registry and argument validation
│   ├── geometry.py          # model canvas <-> physical pixel mapping
│   ├── protocol.py server.py# JSON-RPC routing and stdio/HTTP transports
│   ├── client.py            # client for harness/scripts
│   ├── overlay.py           # emergency-stop visual banner (optional, tkinter)
│   ├── drivers/             # execution-layer abstraction + windows impl + dry-run driver
│   └── a11y/                # accessibility-tree summaries + Windows UIA bridge
└── tests/                   # pure-logic and protocol tests (no real hardware)
```

## Installing in DSH

```bash
dsh plugin --profile demo add github:JohnXu22786/computer-control
```

Remove with:

```bash
dsh plugin --profile demo remove computer-control
```

## Installation

Requirements: **Python 3.9+**, Windows 10/11 (full functionality); other platforms see "Platform Support".

```powershell
# Core (screenshot + input injection)
pip install -r requirements.txt

# Optional capabilities (recommended):
#   comtypes  -> accessibility-tree (UIA) semantic actions
#   mss       -> faster multi-monitor screenshot backend
pip install -r requirements-optional.txt
```

Environment self-check:

```powershell
python -m computer_control check
```

Prints diagnostics for platform, DPI mode, virtual-desktop geometry, screenshot backend, UIA availability, hotkeys, etc.

## Quick Start

```powershell
# List declared tools and events
python -m computer_control list

# Start the plugin service (stdio, for dsh to load)
python -m computer_control serve
```

Run a rehearsal session with `examples/demo.py` (defaults to `dry-run`, touches nothing on the real desktop):

```powershell
python examples/demo.py
```

## dsh Integration (Summary)

Full integration notes: [`docs/integration.md`](docs/integration.md).

1. **Loading**: dsh reads `manifest.json`, starts the process via `entry.command`, and establishes a stdio pipe (UTF-8, one JSON object per line).
2. **Lifecycle**: send `session.start` (optionally with config) → wait for the `session.started` event, then call tools; send `session.stop` at the end.
3. **Calling actions**: `tools.call` (single) or `tools.call_batch` (batch). Responses use a unified `{ok, result, error, meta}` envelope.
4. **Events**: the server pushes `action.started/finished`, `safety.confirmation_requested`, etc. as `event` notifications.
5. **Confirmation flow**: high-risk actions return `awaiting_confirmation` and emit a confirmation event; the harness should surface a human approval prompt, then reply via `session.confirm`; timed out confirmations are auto-denied.

The recommended model loop: `screen.capture` to observe → execute `pointer.*` / `a11y.*` on canvas coordinates → `wait.pause` (if needed) → a fresh screenshot to verify the result.

## Action Reference (Summary)

All actions, parameters and examples: [`docs/actions.md`](docs/actions.md).

| Action | Purpose | Risk |
| --- | --- | --- |
| `screen.capture` | Screenshot (region/format/scale/grayscale) | none |
| `pointer.move` | Move pointer | moderate |
| `pointer.click` | Single/double/triple click, optional position | moderate |
| `pointer.drag` | Press and drag | moderate |
| `pointer.scroll` | Wheel (horizontal/vertical) | moderate |
| `keyboard.press` | Single key | moderate |
| `keyboard.combo` | Chord (upgrades to high when it includes win or ctrl+alt) | moderate/high |
| `keyboard.type` | Text input (Unicode) | moderate |
| `wait.pause` | Pause | none |
| `a11y.snapshot` | Hierarchical accessibility-tree summary | none |
| `a11y.activate` | Semantic activation (pattern first, pixel fallback) | moderate |
| `a11y.input` | Semantic text input (value pattern first) | moderate |
| `batch.execute` | Batch execution | max of items |

### Coordinate Contract

The model sees a **canvas**, not the raw screen: screenshots are scaled to an aspect-preserving canvas whose width is `display_width_px` (default 1920), and coordinates returned by the model live on that canvas; the plugin maps them back to physical pixels uniformly via `scale = physical width / canvas width`. The `screen.capture` result carries a `canvas` field along with `display_width_px/display_height_px` as the source of truth. Multi-monitor setups (including negative-coordinate regions left/above the primary display) and per-monitor DPI are handled in the execution layer.

## Safety

See [`docs/configuration.md`](docs/configuration.md#safety) and the points below:

- **Emergency stop (triple)**: default global hotkey `Ctrl+Alt+F12` (configurable via `safety.emergency_hotkey`, set empty to disable); protocol method `control.panic`; panic file (`safety.panic_file`, presence stops everything). After a panic all actions return `safety_stopped`; `session.resume` or pressing the hotkey again restores operation. A red STOP banner shows in the screen corner while engaged (`safety.visual_indicator`).
- **Allow/deny rules** (`safety.rules`): `{match: {tool: "keyboard.*", argument: {name, matcher, value}}, effect: "deny"}`; deny rules always win over allow rules. `safety.default_rule: "deny"` switches to whitelist mode (anything not explicitly allowed is denied). Rules can be adjusted at runtime via `session.configure`.
- **Confirmation flow**: `safety.confirm_threshold` (default `high`) decides which risk levels need human approval; `safety.confirm_timeout_s` (default 30s) auto-denies on timeout. Approved actions execute normally and emit `action.finished`.
- **Idle standby**: enabled when `safety.idle_timeout_s` > 0; after no activity for the timeout the session enters standby (emits `session.idle`), `session.resume` restores it; `idle_action: "none"` only emits the event without pausing.
- **Dry-run mode**: with `platform: "dry-run"` every action is logged but never executed — handy for integration debugging and safety rehearsals.

## Platform Support

| Platform | Driver | Notes |
| --- | --- | --- |
| Windows | `drivers/windows.py` | Full implementation: SendInput scancode injection, per-monitor DPI awareness, virtual-desktop coordinates, mss/Pillow screenshots, UIA semantic layer |
| macOS / Linux | interface abstracted | `drivers/base.py` defines the complete driver contract (capture/pointer/keys/a11y/hotkey); implement it per platform to plug in (`drivers/windows.py` is a complete example). Until implemented, `platform: "auto"` fails with a clear error |
| Any platform | `drivers/dummy.py` | Dry-run driver: records every action, touches no hardware |

**Dependencies and degradation**:

- `Pillow` (required): screenshots and encoding. The plugin refuses to start without it.
- `mss` (optional): faster and more reliable on multi-monitor Windows; falls back to Pillow ImageGrab when missing.
- `comtypes` (optional): UIA semantic actions. When missing, `a11y.*` tools are marked unavailable in `tools.list` and calls return `backend_unavailable` — the pixel-coordinate path (screenshot + click) is unaffected.
- `keyboard` (optional, reserved): global hotkeys on macOS/Linux drivers will depend on it; only the Windows and dry-run drivers ship today, and Windows uses built-in `GetAsyncKeyState` polling, so the package is not needed.

**Known limitations**:

- Secure attention sequences (e.g. `Ctrl+Alt+Del`) cannot be triggered via input injection — an OS-level protection that the plugin cannot bypass either.
- UIA depends on the target program exposing an accessibility interface; programs that do not (some games, self-drawn UIs) can only use the pixel path.
- Keyboard scancode injection works better with DirectInput/raw-input programs, but some anti-cheat programs may still reject it (normal protective behavior).

## Testing

```powershell
python -m unittest discover -s tests -v
```

Tests use only pure logic and the dry-run driver — no real input is injected, no real hardware is touched; the real Windows chain is verified manually via `python -m computer_control check` and `examples/demo.py --live`.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 JohnXu22786.
