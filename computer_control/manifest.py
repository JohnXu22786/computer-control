"""Manifest data: what the plugin declares to the harness.

Kept in Python as the single source of truth; ``manifest.json`` at the repo
root is generated from it (they must match - the check command verifies).
"""

from __future__ import annotations

from computer_control import actions as act

MANIFEST_META = {
    "schema_version": 1,
    "id": "computer-control",
    "name": "computer-control",
    "title": "Computer Control",
    "description": ("Desktop control plugin for the dsh harness: screen capture, pointer and "
                    "keyboard injection, accessibility-tree driven semantic actions, with an "
                    "emergency stop, allow/deny rules, confirmation flow and idle standby."),
    "version": "0.1.0",
    "author": "plugin-contributors",
    "license": "MIT",
    "entry": {"command": ["python", "-m", "computer_control", "serve"], "working_directory": "."},
    "transport": {"type": "stdio-jsonrpc", "framing": "line-delimited-json",
                  "encoding": "utf-8", "protocol_version": "2.0"},
    "lifecycle": {"start": "session.start", "stop": "session.stop"},
    "config_schema": "docs/configuration.md",
}

_EVENTS = [
    {"name": "session.started", "summary": "A session began and is ready for tools."},
    {"name": "session.stopped", "summary": "A session ended; further tool calls are rejected until a new session starts."},
    {"name": "session.resumed", "summary": "A stopped or standby session returned to ready."},
    {"name": "session.configured", "summary": "Runtime configuration was applied."},
    {"name": "session.idle", "summary": "No actions for the configured idle timeout; the session entered standby."},
    {"name": "action.started", "summary": "A tool call began executing."},
    {"name": "action.finished", "summary": "A tool call finished, with its result envelope."},
    {"name": "batch.finished", "summary": "A batch completed with the aggregated results."},
    {"name": "safety.confirmation_requested", "summary": "A high-risk action is pending human approval; the harness must ask the user."},
    {"name": "safety.confirmation_resolved", "summary": "A pending confirmation was approved or denied."},
    {"name": "safety.confirmation_expired", "summary": "A pending confirmation timed out and was denied."},
    {"name": "safety.panic_triggered", "summary": "The emergency stop engaged (hotkey, protocol request or panic file)."},
    {"name": "safety.panic_released", "summary": "The emergency stop was disengaged."},
]


def manifest_tools() -> list:
    tools = []
    for name in act.tool_names():
        spec = act.get_spec(name)
        tools.append({
            "name": spec.name,
            "summary": spec.summary,
            "risk": spec.risk,
            "availability": spec.availability_hint or "platform dependent",
            "parameters": [p.schema() for p in spec.params],
        })
    return tools


def manifest_events() -> list:
    return [dict(e) for e in _EVENTS]


def manifest() -> dict:
    data = dict(MANIFEST_META)
    data["tools"] = manifest_tools()
    data["events"] = manifest_events()
    return data
