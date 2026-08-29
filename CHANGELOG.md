# Changelog

All notable changes are tracked here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Fixed

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