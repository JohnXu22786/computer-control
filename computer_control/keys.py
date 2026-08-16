"""Key-name parsing shared by configuration, action validation and drivers.

Key names are case-insensitive aliases mapped to Windows virtual-key codes
(which the Windows driver translates to scan codes at injection time).
Non-Windows drivers may interpret the same names in their own way.
"""

from __future__ import annotations

MODIFIER_NAMES = ("ctrl", "alt", "shift", "win")

# Virtual-key codes for the common desktop keys.
KEY_ALIASES = {
    "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "ins": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "ctrl": 0x11, "lctrl": 0xA2, "rctrl": 0xA3,
    "alt": 0x12, "lalt": 0xA4, "ralt": 0xA5,
    "shift": 0x10, "lshift": 0xA0, "rshift": 0xA1,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "super": 0x5B, "meta": 0x5B,
    "apps": 0x5D, "menu": 0x5D,
    "capslock": 0x14,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "printscreen": 0x2C, "prtsc": 0x2C, "prtscn": 0x2C,
    "pause": 0x13, "break": 0x13,
    # punctuation commonly needed in chords (layout-dependent; injected by scan code)
    ";": 0xBA, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE, "=": 0xBB,
}

# F1..F24
for _i in range(1, 25):
    KEY_ALIASES["f%d" % _i] = 0x6F + _i

# Numpad (numpad_enter is the numpad Return key: same virtual key as Enter,
# but its scan code carries the extended prefix)
_NUMPAD = {
    "numpad0": 0x60, "numpad1": 0x61, "numpad2": 0x62, "numpad3": 0x63,
    "numpad4": 0x64, "numpad5": 0x65, "numpad6": 0x66, "numpad7": 0x67,
    "numpad8": 0x68, "numpad9": 0x69, "numpad_enter": 0x0D, "numpad_*": 0x6A,
    "numpad_+": 0x6B, "numpad_-": 0x6D, "numpad_/": 0x6F, "numpad_.": 0x6E,
}
KEY_ALIASES.update(_NUMPAD)


class UnknownKeyError(ValueError):
    pass


def parse_key(name: str) -> int:
    """Resolve a friendly key name to a virtual-key code.

    Accepts aliases, single letters/digits (A-Z, 0-9) and the punctuation
    entries above. Raises UnknownKeyError otherwise.
    """
    if not isinstance(name, str) or not name.strip():
        raise UnknownKeyError("empty key name")
    normalized = name.strip().lower()
    if normalized in KEY_ALIASES:
        return KEY_ALIASES[normalized]
    if len(normalized) == 1:
        code = ord(normalized)
        if "a" <= normalized <= "z":
            return code - 32  # VK_A == ord('A')
        if "0" <= normalized <= "9":
            return code
    raise UnknownKeyError("unknown key: %r" % name)


def parse_hotkey(spec: str):
    """Parse a hotkey spec like ``"ctrl+alt+f12"`` into a list of key names.

    Returns an empty list for an empty/disabled spec. Raises UnknownKeyError
    for unknown names, and ValueError for structural problems (duplicates).
    """
    if not spec or not spec.strip():
        return []
    parts = [p.strip().lower() for p in spec.split("+")]
    if any(not p for p in parts):
        raise ValueError("empty component in hotkey spec %r" % spec)
    if len(parts) != len(set(parts)):
        raise ValueError("duplicate key in hotkey spec %r" % spec)
    for part in parts:
        parse_key(part)  # raises UnknownKeyError for bad names
    return parts


def is_modifier(key: str) -> bool:
    return key.lower() in MODIFIER_NAMES


def parse_key_full(name: str):
    """Resolve a key name to (virtual_key_code, extended).

    ``extended`` marks keys whose scan codes carry the 0xE0 prefix, which the
    injection layer needs. Most names map to (vk, False); numpad_enter maps to
    (VK_RETURN, True) so it can be injected as the numpad key rather than the
    main Enter.
    """
    normalized = name.strip().lower()
    if normalized == "numpad_enter":
        return KEY_ALIASES["enter"], True
    return parse_key(name), False
