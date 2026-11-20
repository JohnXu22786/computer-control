[简体中文](README.zh.md)

# computer-control — Desktop Control Plugin (for dsh)

Lets an agent operate the computer desktop directly: screenshot observation, mouse/keyboard injection, semantic actions via the accessibility tree (semantics first, pixel-coordinate fallback), with built-in safety guards — emergency stop, authorization profiles, allow/deny rules, confirmation flow and idle standby.

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
- **Authorization profiles**: `read_only` / `limited` / `full` / `revoked` narrow which tools may pass the gate — an axis orthogonal to the operational state; an agent may only narrow it in-band, widening needs the host
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

## Using with DSH

```bash
dsh plugin --profile demo add github:JohnXu22786/computer-control
```

Replace `demo` with the profile you want to use.

The repository includes a Cordis bundle: `package.json` points to
`cordis.patch.yml`, and `index.js` starts the Python server and exposes the
manifest tools to dsh. The bundle requires **Node.js 18.17+** and **Python
3.9+**. Install the Python package into the same Python environment that dsh
uses. Profile-level settings can be set in the bundle patch (for example,
`platform.name: dry-run` for a rehearsal-only setup). To use the released
`v0.1.1` Python package:

```bash
python -m pip install "git+https://github.com/JohnXu22786/computer-control.git@v0.1.1"
python -m computer_control check
```

The unpinned source install is also available:

```bash
python -m pip install git+https://github.com/JohnXu22786/computer-control.git
```

Remove the plugin with:

```bash
dsh plugin --profile demo remove computer-control
```

### Using with Codex, Claude Code, or another harness

**Codex and Claude Code do not currently have a direct integration for this
plugin.** They commonly connect external tools through MCP, while
computer-control speaks its own line-delimited JSON-RPC 2.0 protocol. The
dsh `index.js` bundle is a Cordis adapter, not an MCP server; adding
`python -m computer_control serve` as an MCP server will not work.

A harness that supports custom stdio tools can integrate the protocol by
starting:

```bash
python -m computer_control serve
```

The host must send `session.start`, expose `tools.call` or
`tools.call_batch` as model tools, forward server events, and send
`session.stop` when the session ends. It must also turn `screen.capture`
`data_url` results into images the model can inspect. For high-risk actions,
surface `safety.confirmation_requested` to the user and send
`session.confirm` only after the user approves; never auto-approve these
requests. The stdio protocol and examples are documented in
[`docs/protocol.md`](docs/protocol.md) and
[`docs/integration.md`](docs/integration.md).

The server can also expose its custom JSON-RPC API over HTTP:

```bash
python -m computer_control serve --transport http --port 8765
```

Use `POST /rpc` for requests and `GET /events` for the SSE event stream. This
is still the plugin's custom JSON-RPC protocol, not MCP. To use it from Codex
or Claude Code as an MCP tool, a separate MCP adapter must translate MCP tool
calls and lifecycle into this protocol and preserve image results and human
confirmation handling.

Full dsh lifecycle, event, and confirmation details are in
[`docs/integration.md`](docs/integration.md).

## Installation

Requirements: **Python 3.9+**, Windows 10/11 (full functionality); other platforms see "Platform Support".

```powershell
# Core (screenshot + input injection)
python -m pip install .

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

The model sees a **canvas**, not the raw screen: each `screen.capture` result reports the captured image's actual dimensions in `canvas.display_width_px` and `canvas.display_height_px`, and subsequent pointer coordinates use that canvas. This includes the optional `scale` setting and captured regions; the plugin maps image coordinates back to the corresponding physical rectangle. Multi-monitor setups (including negative-coordinate regions left/above the primary display) and per-monitor DPI are handled in the execution layer. Capture regions use the desktop canvas rather than the latest crop; see the [coordinate mapping guide](docs/coordinates.md) for clipping, rounding, and examples.

## Safety

See [`docs/configuration.md`](docs/configuration.md#safety) and the points below:

- **Emergency stop (triple)**: default global hotkey `Ctrl+Alt+F12` (configurable via `safety.emergency_hotkey`, set empty to disable); protocol method `control.panic`; panic file (`safety.panic_file`, presence stops everything). After a panic all actions return `safety_stopped`; `session.resume` or pressing the hotkey again restores operation. A red STOP banner shows in the screen corner while engaged (`safety.visual_indicator`).
- **Authorization profiles** (`safety.auth_profile`, default `full`): a capability ladder, widest first. `full` narrows nothing; `limited` also refuses every `keyboard.*` action and `a11y.input` (semantic text injection is keyboard-equivalent); `read_only` admits `screen.capture` always and `a11y.snapshot` only after the host opts in (`safety.a11y_snapshot_opt_in`); `revoked` shares that same observation floor but cancels a pending confirmation the moment it is entered. Refused actions return `not_authorized`. **This is an axis orthogonal to the operational state**, not another state: it shrinks the set of tools that clears the gate and revocation changes authorization only, preserving the current operational state (`ready`, `standby`, or `stopped`) without triggering the emergency stop. The check sits ahead of the allow/deny rule scan and is shared by plain and batch-internal actions, so batching cannot bypass a profile.
  `session.configure`, which the agent can reach over RPC, may only narrow or hold `auth_profile`; any in-band widening returns `invalid_config`, including `revoked → read_only`. The host can change the profile out of band by calling `Session.grant_profile(new_profile, actor="host")` in-process or, for a stdio subprocess such as dsh, writing the startup-selected control file (`serve --control-file PATH` or `COMPUTER_CONTROL_CONTROL_FILE`). Neither path is in the JSON-RPC handler table, and `a11y_snapshot_opt_in` is also host-only. A stop/start by the agent cannot widen access: the restarted profile is bounded by the latest profile floor on that `Session`; if a persisted host control snapshot is present, its explicit profile grant is applied on startup just as it is at runtime. A host `a11y_snapshot` opt-in/out remains effective across stop/start on the same `Session`.

  The control file format is `{"seq": N, "auth_profile": "...", "a11y_snapshot": bool}`; every directive is a complete desired-state snapshot, so repeat the unchanged setting when updating the other. `seq` increases only within one server process. After a process restart the in-memory counter resets and the persistent snapshot is reapplied. Deleting the file does not change a running session; a new process without a control file starts from its host config. On Linux the file and parent must be root-owned, not group/world writable, and free of effective write grants to other principals in access/default POSIX ACLs. The plugin must run as an unprivileged user with read-only access; on Linux, each process thread must have empty permitted, effective, ambient, and inheritable capability sets, and its CapBnd must exclude CAP_CHOWN, CAP_DAC_OVERRIDE, CAP_FOWNER, CAP_SETUID, CAP_SYS_PTRACE, and CAP_SYS_ADMIN (the first four can bypass file-owner/mode checks or switch the filesystem UID to root; CAP_SYS_PTRACE can control same-user-namespace processes, including a trusted host-control writer, and CAP_SYS_ADMIN can mount an attacker-controlled filesystem over the control path). Each plugin thread must also report NoNewPrivs=1. The login/session manager must establish NoNewPrivs and the restricted bounding set for the whole interactive session before starting its desktop and launchers. Runtime checks cover plugin threads only; the runtime cannot attest same-UID sibling or future processes, which require enforceable system-wide isolation. On Windows the file and parent must be owned by LocalSystem or built-in Administrators, with write ACLs limited to those principals; the plugin must run unelevated. Other POSIX platforms fail closed until their ACLs can be verified. The channel checks these conditions and rejects unsafe paths. Do not grant sudo or administrator rights to the plugin's interactive OS identity: desktop input could otherwise open a shell under that identity and forge host directives. Profile changes emit `safety.grant_changed`; a pure accessibility opt-in/out emits `session.configured`. A downgrade cancels a pending confirmation whose tool the new profile no longer authorizes (looking through `batch.execute` to its sub-actions), and every action is re-gated at execution time, so an approval collected before a downgrade/revoke cannot run afterwards. Known cost: an agent can narrow itself into `revoked` and then needs the host to unlock it.
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
