# Changelog

All notable changes are tracked here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

- Added `--version` / `-V` CLI flag to inspect package version from the command line.

### Fixed

- Aligned JSON-RPC 2.0 error codes to standard values (`-32603` for internal errors, `-32000` for server errors).
- Handled non-object JSON payloads and malformed requests gracefully in `serve_stdio` with standard `-32600` (Invalid Request) responses.
- Disallowed `bool` values in configuration integer/float parsers (`_expect_int`, `_expect_float`) to prevent booleans from passing as numbers.
- Tolerated empty or whitespace-only `COMPUTER_CONTROL_CONFIG` environment variables by falling back to default configuration.
- Sanitized boundary execution arguments in `Engine` for pointer moves, clicks, drags, scrolls, and keyboard type delays.
- Added semantic name fallback in accessibility tree node building to fall back to element role, localized control type, or help text when element name is empty or whitespace.
- Implemented `desktop_info`, simulated hotkey probing, and resource cleanup on `NullDriver` for complete test environment parity.
- Hardened `Rect.clamp_point` for rectangles with fractional dimensions smaller than 1.0 unit.
- Ensured `Rect.from_bbox` produces float attributes for consistent arithmetic operations.
- Fall back to standard surface geometry when driver virtual screen metrics report non-positive dimensions.
- Deduplicated `session.stopped` event emission so exactly one event is emitted on session shutdown.
- Handled `None` argument dictionaries safely in `risk_for` without raising `AttributeError`.
- Validated key name types in `parse_key_full` before normalization to raise `UnknownKeyError` consistently.
- Handled HTTP SSE client disconnections gracefully across replay, event broadcast, and keepalives without uncaught socket exceptions.
- Implemented `HttpClient.close()` to stop background SSE reader threads and terminate HTTP connections cleanly.
- Expanded `is_modifier` and `MODIFIER_NAMES` in `keys.py` to recognize all modifier aliases (`lctrl`, `rctrl`, `lalt`, `ralt`, `lshift`, `rshift`, `lwin`, `rwin`, `super`, `meta`).
- Fixed batch execution plan integrity where actions occurring after a confirming item were dropped from the batch payload upon approval.
- Fixed `_stash_batch_payload` to pre-scan, validate, and apply defaults across all batch items.
- Fixed `Surface.clamp_point` return type to return integer coordinates matching the contract.
- Fixed safety policy rule matching for `contains` to be case-insensitive for keys and string argument matching.
- Per-call timeouts now honor `runtime.max_wait_ms` instead of a hardcoded
  300s floor, so a configured small budget actually fails fast.
- Client request ids are unique across client instances; two clients sharing
  one server can no longer collide and cross-talk responses.
- The dsh bridge tolerates an abrupt server death without crashing the host
  on an unhandled pipe-stream error.

### Chore

- Removed an unreachable validation branch (`screen.capture` jpeg quality is
  always defaulted before validation).
- Chinese README now mirrors the English one's dsh bundle section.

## [0.1.0] - 2026-08-16

Initial release of the computer-control desktop plugin for dsh.

### Added

- Screen observation: `screen.capture` with region / PNG / JPEG / scale /
  grayscale options, returning a model-space canvas.
- Pointer: `pointer.move` / `pointer.click` / `pointer.drag` /
  `pointer.scroll` (horizontal/vertical wheel).
- Keyboard: `keyboard.press` (single key), `keyboard.combo` (chords, with
  `win`/`ctrl+alt` upgrades to high risk), `keyboard.type` (Unicode text).
- Accessibility-tree semantic actions: `a11y.snapshot` /
  `a11y.activate` / `a11y.input` (skeleton / standard / full density).
- `batch.execute` to run several actions in one call.
- Safety guards: emergency stop (hotkey / protocol / panic file), allow/deny
  rules with whitelist mode, confirmation flow for high-risk actions, idle
  standby, and a `dry-run` platform for safe rehearsals.
- Transports: line-delimited JSON-RPC 2.0 over stdio and an HTTP transport
  with an SSE event feed.
- dsh bundle: `package.json` declares `dsh.bundle`; `cordis.patch.yml`
  installs the plugin into a dsh profile; `index.js` bridges the harness to
  the Python server over the stdio protocol.

### Fixed

- Stale `a11y.snapshot` ids are rejected after new snapshots.
- Batch items are gated individually; a whole batch waits for confirmation
  when any item is high-risk.
- Deny rules always win over allow rules regardless of rule order.

[0.1.0]: https://github.com/JohnXu22786/computer-control/commits/