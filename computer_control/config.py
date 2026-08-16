"""Configuration model: typed sections, defaults, file loading, validation.

The configuration is plain JSON with four sections:

    platform   - driver selection ("auto" picks the best driver for the OS;
                 "dry-run" rehearses every action on a recording driver)
    capture    - default screenshot encoding and the model-space canvas width
    safety     - emergency stop, rules, confirmation flow, idle standby
    a11y       - accessibility-tree summary defaults
    runtime    - timing knobs

Any unknown key anywhere is a hard error so typos fail loudly.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any, List, Optional

from computer_control.keys import UnknownKeyError, parse_hotkey

RISK_LEVELS = ("benign", "moderate", "high")
CAPTURE_FORMATS = ("png", "jpeg")
A11Y_LEVELS = ("skeleton", "standard", "full")
PLATFORM_NAMES = ("auto", "windows", "dry-run")
IDLE_ACTIONS = ("standby", "none")
CAPTURE_BACKENDS = ("auto", "pillow", "mss")
RULE_MATCHERS = ("equals", "glob", "contains")
RULE_EFFECTS = ("allow", "deny")

CONFIG_SECTIONS = ("platform", "capture", "safety", "a11y", "runtime")


class ConfigError(ValueError):
    """Raised when configuration is malformed or fails validation."""


def _expect_type(value, types, what, section, key):
    if not isinstance(value, types):
        raise ConfigError("%s.%s must be %s, got %r" % (section, key, what, value))


def _expect_int(value, what, section, key, minimum=None, maximum=None):
    _expect_type(value, (int, float), "a number", section, key)
    if isinstance(value, float) and not value.is_integer():
        raise ConfigError("%s.%s must be an integer, got %r" % (section, key, value))
    value = int(value)
    if minimum is not None and value < minimum:
        raise ConfigError("%s.%s must be >= %d" % (section, key, minimum))
    if maximum is not None and value > maximum:
        raise ConfigError("%s.%s must be <= %d" % (section, key, maximum))
    return value


def _expect_float(value, what, section, key, minimum=None, maximum=None):
    _expect_type(value, (int, float), "a number", section, key)
    value = float(value)
    if minimum is not None and value < minimum:
        raise ConfigError("%s.%s must be >= %s" % (section, key, minimum))
    if maximum is not None and value > maximum:
        raise ConfigError("%s.%s must be <= %s" % (section, key, maximum))
    return value


def _expect_enum(value, choices, section, key):
    if value not in choices:
        raise ConfigError("%s.%s must be one of %s, got %r" % (section, key, "/".join(choices), value))
    return value


@dataclass
class PlatformConfig:
    name: str = "auto"


@dataclass
class CaptureConfig:
    default_width: int = 1920
    default_format: str = "png"
    default_quality: int = 85
    grayscale: bool = False
    max_area: int = 5_000_000
    backend: str = "auto"


@dataclass
class RuleConfig:
    tool_pattern: str
    argument_name: Optional[str]
    matcher: str
    value: Any
    effect: str

    def as_dict(self) -> dict:
        return {
            "match": {
                "tool": self.tool_pattern,
                "argument": None if self.argument_name is None else {
                    "name": self.argument_name, "matcher": self.matcher, "value": self.value,
                },
            },
            "effect": self.effect,
        }


@dataclass
class SafetyConfig:
    emergency_hotkey: str = "ctrl+alt+f12"
    confirm_threshold: str = "high"
    confirm_timeout_s: float = 30.0
    idle_timeout_s: float = 0.0
    idle_action: str = "standby"
    panic_file: str = ""
    visual_indicator: bool = True
    default_rule: str = "allow"
    rules: List[RuleConfig] = field(default_factory=list)


@dataclass
class A11yConfig:
    default_level: str = "standard"
    max_name_len: int = 64
    include_rects: bool = True
    hard_walk_cap: int = 5000


@dataclass
class RuntimeConfig:
    batch_gap_ms: int = 150
    max_wait_ms: int = 600_000


@dataclass
class Config:
    platform: PlatformConfig = field(default_factory=PlatformConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    a11y: A11yConfig = field(default_factory=A11yConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

    def as_dict(self) -> dict:
        return {
            "platform": {
                "name": self.platform.name,
            },
            "capture": {
                "default_width": self.capture.default_width,
                "default_format": self.capture.default_format,
                "default_quality": self.capture.default_quality,
                "grayscale": self.capture.grayscale,
                "max_area": self.capture.max_area,
                "backend": self.capture.backend,
            },
            "safety": {
                "emergency_hotkey": self.safety.emergency_hotkey,
                "confirm_threshold": self.safety.confirm_threshold,
                "confirm_timeout_s": self.safety.confirm_timeout_s,
                "idle_timeout_s": self.safety.idle_timeout_s,
                "idle_action": self.safety.idle_action,
                "panic_file": self.safety.panic_file,
                "visual_indicator": self.safety.visual_indicator,
                "default_rule": self.safety.default_rule,
                "rules": [r.as_dict() for r in self.safety.rules],
            },
            "a11y": {
                "default_level": self.a11y.default_level,
                "max_name_len": self.a11y.max_name_len,
                "include_rects": self.a11y.include_rects,
                "hard_walk_cap": self.a11y.hard_walk_cap,
            },
            "runtime": {
                "batch_gap_ms": self.runtime.batch_gap_ms,
                "max_wait_ms": self.runtime.max_wait_ms,
            },
        }


def _parse_rule(raw, index) -> RuleConfig:
    _expect_type(raw, dict, "an object", "safety.rules[%d]" % index, "")
    allowed = {"match", "effect"}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError("safety.rules[%d]: unknown keys %s" % (index, sorted(unknown)))
    match = raw.get("match")
    effect = raw.get("effect")
    if not isinstance(match, dict):
        raise ConfigError("safety.rules[%d]: 'match' must be an object" % index)
    if effect not in RULE_EFFECTS:
        raise ConfigError("safety.rules[%d]: 'effect' must be allow or deny" % index)
    unknown_match = set(match) - {"tool", "argument"}
    if unknown_match:
        raise ConfigError("safety.rules[%d].match: unknown keys %s" % (index, sorted(unknown_match)))
    tool = match.get("tool")
    if not isinstance(tool, str) or not tool:
        raise ConfigError("safety.rules[%d].match: 'tool' must be a non-empty string" % index)
    arg = match.get("argument")
    argument_name = matcher = value = None
    if arg is not None:
        if not isinstance(arg, dict):
            raise ConfigError("safety.rules[%d].match: 'argument' must be an object" % index)
        if set(arg) != {"name", "matcher", "value"}:
            raise ConfigError("safety.rules[%d].match.argument: must have exactly name/matcher/value" % index)
        argument_name = arg["name"]
        matcher = arg["matcher"]
        value = arg["value"]
        if not isinstance(argument_name, str) or not argument_name:
            raise ConfigError("safety.rules[%d].match.argument.name must be a non-empty string" % index)
        if matcher not in RULE_MATCHERS:
            raise ConfigError("safety.rules[%d].match.argument.matcher must be one of %s"
                              % (index, "/".join(RULE_MATCHERS)))
    return RuleConfig(tool, argument_name, matcher, value, effect)


def from_dict(data: dict) -> Config:
    """Build a Config from a raw dict, applying validation."""
    if not isinstance(data, dict):
        raise ConfigError("configuration must be a JSON object")
    unknown = set(data) - set(CONFIG_SECTIONS)
    if unknown:
        raise ConfigError("unknown configuration section(s): %s" % ", ".join(sorted(unknown)))

    cfg = Config()

    # platform
    platform = data.get("platform", {})
    if not isinstance(platform, dict):
        raise ConfigError("platform must be an object")
    unknown = set(platform) - {"name"}
    if unknown:
        raise ConfigError("unknown platform option(s): %s" % ", ".join(sorted(unknown)))
    if "name" in platform:
        cfg.platform.name = _expect_enum(platform["name"], PLATFORM_NAMES, "platform", "name")

    # capture
    capture = data.get("capture", {})
    if not isinstance(capture, dict):
        raise ConfigError("capture must be an object")
    unknown = set(capture) - {"default_width", "default_format", "default_quality", "grayscale", "max_area", "backend"}
    if unknown:
        raise ConfigError("unknown capture option(s): %s" % ", ".join(sorted(unknown)))
    if "default_width" in capture:
        cfg.capture.default_width = _expect_int(capture["default_width"], "an integer", "capture", "default_width", minimum=1, maximum=16_384)
    if "default_format" in capture:
        cfg.capture.default_format = _expect_enum(capture["default_format"], CAPTURE_FORMATS, "capture", "default_format")
    if "default_quality" in capture:
        cfg.capture.default_quality = _expect_int(capture["default_quality"], "an integer", "capture", "default_quality", minimum=1, maximum=100)
    if "grayscale" in capture:
        _expect_type(capture["grayscale"], bool, "a boolean", "capture", "grayscale")
        cfg.capture.grayscale = capture["grayscale"]
    if "max_area" in capture:
        cfg.capture.max_area = _expect_int(capture["max_area"], "an integer", "capture", "max_area", minimum=1024)
    if "backend" in capture:
        cfg.capture.backend = _expect_enum(capture["backend"], CAPTURE_BACKENDS, "capture", "backend")

    # safety
    safety = data.get("safety", {})
    if not isinstance(safety, dict):
        raise ConfigError("safety must be an object")
    unknown = set(safety) - {"emergency_hotkey", "confirm_threshold", "confirm_timeout_s", "idle_timeout_s",
                             "idle_action", "panic_file", "visual_indicator", "default_rule", "rules"}
    if unknown:
        raise ConfigError("unknown safety option(s): %s" % ", ".join(sorted(unknown)))
    if "emergency_hotkey" in safety:
        _expect_type(safety["emergency_hotkey"], str, "a string", "safety", "emergency_hotkey")
        try:
            parse_hotkey(safety["emergency_hotkey"])
        except (UnknownKeyError, ValueError) as exc:
            raise ConfigError("safety.emergency_hotkey invalid: %s" % exc)
        cfg.safety.emergency_hotkey = safety["emergency_hotkey"]
    if "confirm_threshold" in safety:
        cfg.safety.confirm_threshold = _expect_enum(safety["confirm_threshold"], RISK_LEVELS, "safety", "confirm_threshold")
    if "confirm_timeout_s" in safety:
        cfg.safety.confirm_timeout_s = _expect_float(safety["confirm_timeout_s"], "a number", "safety", "confirm_timeout_s", minimum=0.01, maximum=3600)
    if "idle_timeout_s" in safety:
        cfg.safety.idle_timeout_s = _expect_float(safety["idle_timeout_s"], "a number", "safety", "idle_timeout_s", minimum=0, maximum=86_400)
    if "idle_action" in safety:
        cfg.safety.idle_action = _expect_enum(safety["idle_action"], IDLE_ACTIONS, "safety", "idle_action")
    if "panic_file" in safety:
        _expect_type(safety["panic_file"], str, "a string", "safety", "panic_file")
        cfg.safety.panic_file = safety["panic_file"]
    if "visual_indicator" in safety:
        _expect_type(safety["visual_indicator"], bool, "a boolean", "safety", "visual_indicator")
        cfg.safety.visual_indicator = safety["visual_indicator"]
    if "default_rule" in safety:
        cfg.safety.default_rule = _expect_enum(safety["default_rule"], RULE_EFFECTS, "safety", "default_rule")
    if "rules" in safety:
        _expect_type(safety["rules"], list, "a list", "safety", "rules")
        cfg.safety.rules = [_parse_rule(raw, i) for i, raw in enumerate(safety["rules"])]

    # a11y
    a11y = data.get("a11y", {})
    if not isinstance(a11y, dict):
        raise ConfigError("a11y must be an object")
    unknown = set(a11y) - {"default_level", "max_name_len", "include_rects", "hard_walk_cap"}
    if unknown:
        raise ConfigError("unknown a11y option(s): %s" % ", ".join(sorted(unknown)))
    if "default_level" in a11y:
        cfg.a11y.default_level = _expect_enum(a11y["default_level"], A11Y_LEVELS, "a11y", "default_level")
    if "max_name_len" in a11y:
        cfg.a11y.max_name_len = _expect_int(a11y["max_name_len"], "an integer", "a11y", "max_name_len", minimum=1, maximum=4096)
    if "include_rects" in a11y:
        _expect_type(a11y["include_rects"], bool, "a boolean", "a11y", "include_rects")
        cfg.a11y.include_rects = a11y["include_rects"]
    if "hard_walk_cap" in a11y:
        cfg.a11y.hard_walk_cap = _expect_int(a11y["hard_walk_cap"], "an integer", "a11y", "hard_walk_cap", minimum=100, maximum=100_000)

    # runtime
    runtime = data.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ConfigError("runtime must be an object")
    unknown = set(runtime) - {"batch_gap_ms", "max_wait_ms"}
    if unknown:
        raise ConfigError("unknown runtime option(s): %s" % ", ".join(sorted(unknown)))
    if "batch_gap_ms" in runtime:
        cfg.runtime.batch_gap_ms = _expect_int(runtime["batch_gap_ms"], "an integer", "runtime", "batch_gap_ms", minimum=0, maximum=60_000)
    if "max_wait_ms" in runtime:
        cfg.runtime.max_wait_ms = _expect_int(runtime["max_wait_ms"], "an integer", "runtime", "max_wait_ms", minimum=1, maximum=86_400_000)

    return cfg


def default_config() -> Config:
    return Config()


def load_config(path: Optional[str]) -> Config:
    """Load a configuration file (JSON). ``None`` or missing file -> defaults.

    The environment variable COMPUTER_CONTROL_CONFIG overrides ``path`` when
    ``path`` is not given explicitly.
    """
    effective = path or os.environ.get("COMPUTER_CONTROL_CONFIG")
    if not effective:
        return default_config()
    if not os.path.exists(effective):
        raise ConfigError("config file not found: %s" % effective)
    try:
        with open(effective, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot read config file %s: %s" % (effective, exc))
    return from_dict(raw)


def apply_overrides(base: Config, patch: dict) -> Config:
    """Return a new Config with ``patch`` (raw dict shape) applied on top of
    ``base``. The base config is never mutated."""
    merged = copy.deepcopy(base.as_dict())
    _deep_merge(merged, patch)
    return from_dict(merged)


def _deep_merge(target: dict, patch: dict) -> None:
    for key, value in patch.items():
        if key not in target:
            raise ConfigError("unknown configuration key: %s" % key)
        if isinstance(value, dict) and isinstance(target[key], dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value
