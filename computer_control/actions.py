"""The action registry: every tool the agent can call, its parameter schema,
validation, and risk classification.

One uniform shape for all tools: ``{"tool": "...", "arguments": {...}}``.
Validation is strict on purpose - unknown parameters and out-of-range values
are rejected so the model cannot silently drift into undefined behavior.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, List, Optional

from computer_control.keys import UnknownKeyError, parse_key

RISK_BENIGN = "benign"
RISK_MODERATE = "moderate"
RISK_HIGH = "high"
RISK_LEVELS = (RISK_BENIGN, RISK_MODERATE, RISK_HIGH)

# Aliases that refer to the same physical key. Risk classification and rule
# matching normalize through these so "lwin", "super", "meta" cannot dodge
# the win-key rules.
_WIN_LIKE = frozenset({"win", "lwin", "rwin", "super", "meta"})
_CTRL_LIKE = frozenset({"ctrl", "lctrl", "rctrl"})
_ALT_LIKE = frozenset({"alt", "lalt", "ralt"})

TYPES = ("int", "float", "number", "str", "bool", "list", "region")


class ValidationError(ValueError):
    """Raised when tool arguments fail validation. Message lists the issues."""

    def __init__(self, issues: List[str]):
        self.issues = issues
        super().__init__("; ".join(issues))


@dataclass
class ParamSpec:
    name: str
    type: str
    description: str
    required: bool = False
    default: Any = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: Optional[List[Any]] = None

    def schema(self) -> dict:
        spec = {"name": self.name, "type": self.type, "description": self.description, "required": self.required}
        if self.default is not None:
            spec["default"] = self.default
        if self.minimum is not None:
            spec["minimum"] = self.minimum
        if self.maximum is not None:
            spec["maximum"] = self.maximum
        if self.choices is not None:
            spec["choices"] = self.choices
        return spec


@dataclass
class ActionSpec:
    name: str
    summary: str
    risk: str
    params: List[ParamSpec]
    examples: List[dict] = field(default_factory=list)
    availability_hint: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "summary": self.summary,
            "risk": self.risk,
            "availability_hint": self.availability_hint,
            "parameters": [p.schema() for p in self.params],
            "examples": self.examples,
        }


def _p(name, type_, description, required=False, default=None, minimum=None, maximum=None, choices=None):
    return ParamSpec(name, type_, description, required, default, minimum, maximum, choices)


def _build_registry() -> dict:
    tools = {}

    tools["screen.capture"] = ActionSpec(
        name="screen.capture",
        summary="Capture the desktop (or a region) as an image. The returned data URL is exactly the model-space canvas the coordinates below refer to.",
        risk=RISK_BENIGN,
        params=[
            _p("region", "region", "Model-space region {x, y, width, height} to capture; omit for the whole desktop.", required=False),
            _p("format", "str", "Image encoding: png or jpeg (jpeg costs fewer tokens).", default="png", choices=["png", "jpeg"]),
            _p("quality", "int", "JPEG quality 1-100 (ignored for png).", default=85, minimum=1, maximum=100),
            _p("scale", "float", "Relative canvas scale 0.1-1.0; smaller = fewer tokens.", default=1.0, minimum=0.1, maximum=1.0),
            _p("grayscale", "bool", "Convert to grayscale to save tokens.", default=False),
        ],
        examples=[
            {},
            {"region": {"x": 0, "y": 0, "width": 400, "height": 300}, "format": "jpeg", "scale": 0.5},
        ],
    )

    tools["pointer.move"] = ActionSpec(
        name="pointer.move",
        summary="Move the mouse pointer to a model-space position without clicking.",
        risk=RISK_MODERATE,
        params=[
            _p("x", "float", "Model-space x coordinate.", required=True, minimum=0),
            _p("y", "float", "Model-space y coordinate.", required=True, minimum=0),
            _p("steps", "int", "Interpolation steps for a smooth move (1 = instant).", default=1, minimum=1, maximum=200),
        ],
        examples=[{"x": 960, "y": 540}],
    )

    tools["pointer.click"] = ActionSpec(
        name="pointer.click",
        summary="Click with a button, optionally after moving to a position. Prefer a11y.activate when the element is visible in the accessibility tree.",
        risk=RISK_MODERATE,
        params=[
            _p("x", "float", "Model-space x coordinate; omit to click at the current pointer position.", minimum=0),
            _p("y", "float", "Model-space y coordinate; omit to click at the current pointer position.", minimum=0),
            _p("button", "str", "Which button.", default="left", choices=["left", "middle", "right"]),
            _p("times", "int", "1, 2 or 3 (single, double, triple click).", default=1, minimum=1, maximum=3),
            _p("hold_ms", "int", "Hold the button down this long before releasing.", default=0, minimum=0, maximum=10_000),
        ],
        examples=[{"x": 100, "y": 200, "button": "left"}, {"x": 500, "y": 300, "times": 2}],
    )

    tools["pointer.drag"] = ActionSpec(
        name="pointer.drag",
        summary="Press a button at one position, move to another position, then release.",
        risk=RISK_MODERATE,
        params=[
            _p("from", "region", "Start position {x, y} in model space.", required=True),
            _p("to", "region", "End position {x, y} in model space.", required=True),
            _p("button", "str", "Which button to hold.", default="left", choices=["left", "middle", "right"]),
            _p("steps", "int", "Number of interpolation steps between the two positions.", default=24, minimum=1, maximum=400),
            _p("hold_ms", "int", "Pause at the destination before releasing.", default=0, minimum=0, maximum=10_000),
        ],
        examples=[{"from": {"x": 100, "y": 100}, "to": {"x": 400, "y": 400}}],
    )

    tools["pointer.scroll"] = ActionSpec(
        name="pointer.scroll",
        summary="Scroll the wheel at the current pointer position (or after moving to a position).",
        risk=RISK_MODERATE,
        params=[
            _p("axis", "str", "Scroll axis.", default="vertical", choices=["vertical", "horizontal"]),
            _p("amount", "float", "Wheel notches, signed: positive = up/right, negative = down/left.", required=True, minimum=-100, maximum=100),
            _p("x", "float", "Move here first (model space).", minimum=0),
            _p("y", "float", "Move here first (model space).", minimum=0),
        ],
        examples=[{"amount": -3}, {"amount": 5, "x": 800, "y": 450}],
    )

    tools["keyboard.press"] = ActionSpec(
        name="keyboard.press",
        summary="Press and release a single key (letters, digits, F-keys, arrows, punctuation).",
        risk=RISK_MODERATE,
        params=[
            _p("key", "str", "Key name, e.g. 'enter', 'f5', 'a', '1', 'left'.", required=True),
        ],
        examples=[{"key": "enter"}],
    )

    tools["keyboard.combo"] = ActionSpec(
        name="keyboard.combo",
        summary="Press several keys together as a chord, e.g. ctrl+shift+esc. Chords that include win or ctrl+alt are high-risk and trigger the confirmation flow.",
        risk=RISK_MODERATE,
        params=[
            _p("keys", "list", "Chord keys as a list ['ctrl','shift','esc'] or a string 'ctrl+shift+esc'.", required=True),
        ],
        examples=[{"keys": ["ctrl", "c"]}, {"keys": "ctrl+shift+esc"}],
    )

    tools["keyboard.type"] = ActionSpec(
        name="keyboard.type",
        summary="Type text via keyboard events. Newlines become Enter, tabs become Tab.",
        risk=RISK_MODERATE,
        params=[
            _p("text", "str", "Text to type (UTF-8, up to 10000 characters).", required=True),
            _p("submit", "bool", "Press Enter after the text.", default=False),
            _p("interval_ms", "int", "Delay between characters.", default=0, minimum=0, maximum=1000),
        ],
        examples=[{"text": "Hello, world!"}, {"text": "ls -la\n", "interval_ms": 5}],
    )

    tools["wait.pause"] = ActionSpec(
        name="wait.pause",
        summary="Wait, letting the UI settle. Use before screenshots after actions that trigger animation.",
        risk=RISK_BENIGN,
        params=[
            _p("ms", "int", "Milliseconds to wait.", required=True, minimum=0, maximum=600_000),
        ],
        examples=[{"ms": 250}],
    )

    tools["a11y.snapshot"] = ActionSpec(
        name="a11y.snapshot",
        summary="Semantic view of the desktop: a hierarchical summary of the accessibility tree with token-cost levels (skeleton/standard/full).",
        risk=RISK_BENIGN,
        params=[
            _p("level", "str", "Summary density: skeleton (cheap), standard, full (expensive).", default="standard", choices=["skeleton", "standard", "full"]),
            _p("depth", "int", "Override tree depth.", minimum=1, maximum=40),
            _p("max_nodes", "int", "Override maximum summarized nodes.", minimum=1, maximum=10_000),
            _p("include_rects", "bool", "Include element bounding rectangles (needed for pointer fallback).", default=True),
        ],
        examples=[{"level": "skeleton"}, {"level": "full"}],
    )

    tools["a11y.activate"] = ActionSpec(
        name="a11y.activate",
        summary="Semantically activate an element from the latest snapshot (invoke/toggle/select pattern first; pixel click on its bounding box as fallback).",
        risk=RISK_MODERATE,
        params=[
            _p("snapshot_id", "str", "The snapshot_id of the most recent a11y.snapshot. Stale ids are rejected.", required=True),
            _p("node_id", "int", "Element id from that snapshot.", required=True, minimum=1),
            _p("method", "str", "auto = pattern then pointer fallback; pattern = patterns only; pointer = skip patterns.", default="auto", choices=["auto", "pattern", "pointer"]),
        ],
        examples=[{"snapshot_id": "snap-3", "node_id": 12}],
    )

    tools["a11y.input"] = ActionSpec(
        name="a11y.input",
        summary="Semantically set text on an editable element from the latest snapshot (value pattern first; click-and-type as fallback).",
        risk=RISK_MODERATE,
        params=[
            _p("snapshot_id", "str", "The snapshot_id of the most recent a11y.snapshot. Stale ids are rejected.", required=True),
            _p("node_id", "int", "Element id from that snapshot.", required=True, minimum=1),
            _p("text", "str", "Text to enter.", required=True),
        ],
        examples=[{"snapshot_id": "snap-3", "node_id": 21, "text": "query"}],
    )

    tools["batch.execute"] = ActionSpec(
        name="batch.execute",
        summary="Run several actions in one call to cut round trips. Each item is individually gated by the safety policy; the whole batch is confirmation-gated if any item is high-risk.",
        risk=RISK_BENIGN,  # effective risk = max over items, computed at validation time
        params=[
            _p("items", "list", "List of {tool, arguments} objects.", required=True),
            _p("continue_on_error", "bool", "Keep running after an item fails.", default=False),
            _p("gap_ms", "int", "Pause between items.", default=150, minimum=0, maximum=60_000),
        ],
        examples=[{"items": [{"tool": "pointer.click", "arguments": {"x": 100, "y": 100}}, {"tool": "wait.pause", "arguments": {"ms": 300}}]}],
    )

    return tools


ACTION_REGISTRY = _build_registry()


def get_spec(tool: str) -> Optional[ActionSpec]:
    return ACTION_REGISTRY.get(tool)


def _coerce(param: ParamSpec, raw, issues: List[str], tool: str) -> Any:
    """Coerce + range-check a single raw value."""
    if param.type == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            if raw.lower() in ("true", "1"):
                return True
            if raw.lower() in ("false", "0"):
                return False
        issues.append("%s: expected a boolean, got %r" % (param.name, raw))
        return None
    if param.type in ("int", "float", "number"):
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            issues.append("%s: expected a number, got %r" % (param.name, raw))
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            issues.append("%s: expected a number, got %r" % (param.name, raw))
            return None
        if not _isfinite(value):
            issues.append("%s: expected a finite number, got %r" % (param.name, raw))
            return None
        if param.type == "int" and not value.is_integer():
            issues.append("%s: expected an integer, got %r" % (param.name, raw))
            return None
        value = int(value) if param.type == "int" else value
        if param.minimum is not None and value < param.minimum:
            issues.append("%s: must be >= %s" % (param.name, param.minimum))
        if param.maximum is not None and value > param.maximum:
            issues.append("%s: must be <= %s" % (param.name, param.maximum))
        return value
    if param.type == "str":
        if not isinstance(raw, str):
            issues.append("%s: expected a string, got %r" % (param.name, raw))
            return None
        if param.choices is not None and raw not in param.choices:
            issues.append("%s: must be one of %s" % (param.name, "/".join(param.choices)))
        return raw
    if param.type == "list":
        if isinstance(raw, str):
            raw = raw.split("+")
        if not isinstance(raw, list) or not raw:
            issues.append("%s: expected a non-empty list" % param.name)
            return raw
        return raw
    if param.type == "region":
        if not isinstance(raw, dict):
            issues.append("%s: expected an object" % param.name)
            return raw
        allowed = {"x", "y", "width", "height"}
        if param.name in ("from", "to"):
            allowed = {"x", "y"}
        if set(raw) != allowed:
            issues.append("%s: expected exactly %s keys" % (param.name, sorted(allowed)))
            return raw
        for key, value in raw.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                issues.append("%s.%s: expected a number" % (param.name, key))
            elif not _isfinite(float(value)):
                issues.append("%s.%s: expected a finite number" % (param.name, key))
        if "width" in raw and raw["width"] <= 0:
            issues.append("%s.width must be positive" % param.name)
        if "height" in raw and raw["height"] <= 0:
            issues.append("%s.height must be positive" % param.name)
        if "x" in raw and raw["x"] < 0:
            issues.append("%s.x must be >= 0" % param.name)
        if "y" in raw and raw["y"] < 0:
            issues.append("%s.y must be >= 0" % param.name)
        return raw
    issues.append("%s: unsupported type %s" % (param.name, param.type))
    return None


def _validate_key_param(keys, issues, tool) -> None:
    """Validate key names for keyboard.press / keyboard.combo."""
    if tool == "keyboard.press":
        try:
            parse_key(keys)
        except UnknownKeyError as exc:
            issues.append(str(exc))
        return
    seen = set()
    for name in keys:
        if not isinstance(name, str):
            issues.append("keys: expected strings, got %r" % (name,))
            continue
        try:
            parse_key(name)
        except UnknownKeyError as exc:
            issues.append(str(exc))
            continue
        if name.lower() in seen:
            issues.append("keys: duplicate key %r" % name)
        seen.add(name.lower())


def clean_arguments(tool: str, raw_args) -> dict:
    """Validate and normalize arguments for a tool.

    Returns the cleaned arguments (defaults filled, types coerced, key names
    normalized). Raises ValidationError with a list of issues.
    """
    spec = get_spec(tool)
    if spec is None:
        raise ValidationError(["unknown tool %r" % tool])
    if raw_args is None:
        raw_args = {}
    if not isinstance(raw_args, dict):
        raise ValidationError(["arguments must be an object"])

    by_name = {p.name: p for p in spec.params}
    issues = []
    cleaned = {}
    for param in spec.params:
        if param.name in raw_args:
            raw = raw_args[param.name]
            if raw is None and not param.required:
                cleaned[param.name] = param.default  # explicit null falls back to the default
            else:
                cleaned[param.name] = _coerce(param, raw, issues, tool)
        elif param.required:
            issues.append("missing required parameter: %s" % param.name)
        elif param.default is not None:
            cleaned[param.name] = param.default
        else:
            cleaned[param.name] = None

    unknown = set(raw_args) - set(by_name)
    for name in sorted(unknown):
        issues.append("unknown parameter: %s" % name)

    # cross-field validation
    if tool == "keyboard.combo":
        keys = cleaned.get("keys")
        if isinstance(keys, list):
            _validate_key_param(keys, issues, tool)
    elif tool == "keyboard.press":
        _validate_key_param(cleaned.get("key"), issues, tool)
    elif tool == "pointer.click":
        if (cleaned.get("x") is None) != (cleaned.get("y") is None):
            issues.append("x and y must be given together (or neither)")
    elif tool == "pointer.scroll":
        if (cleaned.get("x") is None) != (cleaned.get("y") is None):
            issues.append("x and y must be given together (or neither)")
    elif tool == "screen.capture":
        if cleaned.get("format") == "jpeg" and cleaned.get("quality") is None:
            issues.append("quality is required for jpeg")
    elif tool in ("keyboard.type", "a11y.input"):
        text = cleaned.get("text")
        if isinstance(text, str) and len(text) > 10000:
            issues.append("text: too long (%d characters, max 10000)" % len(text))
    elif tool == "batch.execute":
        items = cleaned.get("items") or []
        if not items:
            issues.append("items: must contain at least one action")
        else:
            sub_issues = []
            for i, item in enumerate(items):
                if not isinstance(item, dict):
                    sub_issues.append("items[%d]: must be an object {tool, arguments}" % i)
                    continue
                item_tool = item.get("tool")
                if not isinstance(item_tool, str) or not item_tool:
                    sub_issues.append("items[%d]: missing tool" % i)
                    continue
                # unknown tools are allowed here; run_batch reports them as
                # per-item failures so a batch can continue past them
                if get_spec(item_tool) is not None:
                    try:
                        clean_arguments(item_tool, item.get("arguments"))
                    except ValidationError as exc:
                        for issue in exc.issues:
                            sub_issues.append("items[%d]: %s" % (i, issue))
            issues.extend(sub_issues)
    elif tool == "a11y.activate":
        if cleaned.get("method") == "pointer" and cleaned.get("snapshot_id") is None:
            issues.append("snapshot_id is required even for pointer method (staleness guard)")

    if issues:
        raise ValidationError(issues)
    return cleaned


def risk_for(tool: str, arguments: dict) -> str:
    """Effective risk of a tool call, after argument inspection."""
    spec = get_spec(tool)
    if spec is None:
        return RISK_MODERATE
    risk = spec.risk
    if tool == "keyboard.combo":
        keys = arguments.get("keys") or []
        if isinstance(keys, str):
            keys = keys.split("+")
        lowered = {str(k).lower() for k in keys}
        if lowered & _WIN_LIKE or (lowered & _CTRL_LIKE and lowered & _ALT_LIKE):
            risk = RISK_HIGH
    elif tool == "batch.execute":
        item_risks = [risk_for(item.get("tool", ""), item.get("arguments") or {}) for item in (arguments.get("items") or [])]
        if RISK_HIGH in item_risks:
            risk = RISK_HIGH
        elif RISK_MODERATE in item_risks:
            risk = RISK_MODERATE
    return risk


def tool_names() -> List[str]:
    return sorted(ACTION_REGISTRY.keys())


def match_tool_pattern(pattern: str, tool: str) -> bool:
    return fnmatch.fnmatchcase(tool, pattern)


def _isfinite(value: float) -> bool:
    import math

    return math.isfinite(value)
