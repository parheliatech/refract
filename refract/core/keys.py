"""Key, modifier and mouse-button codes for scenes and the HUD.

The numbers are GLFW's (Refract started on GLFW, and every scene compares
against these names), so nothing that handles input had to change when the
window moved to GTK 4. from_gdk() translates a GTK key event.

Codes are PHYSICAL keys, like GLFW's: the key that types "=" is KEY_EQUAL
with or without Shift.
"""

RELEASE, PRESS, REPEAT = 0, 1, 2

MOD_SHIFT, MOD_CONTROL, MOD_ALT, MOD_SUPER = 0x1, 0x2, 0x4, 0x8

MOUSE_BUTTON_LEFT, MOUSE_BUTTON_RIGHT, MOUSE_BUTTON_MIDDLE = 0, 1, 2

KEY_UNKNOWN = -1
KEY_SPACE = 32
KEY_APOSTROPHE = 39
KEY_COMMA = 44
KEY_MINUS = 45
KEY_PERIOD = 46
KEY_SLASH = 47
KEY_SEMICOLON = 59
KEY_EQUAL = 61
KEY_LEFT_BRACKET = 91
KEY_BACKSLASH = 92
KEY_RIGHT_BRACKET = 93
KEY_GRAVE_ACCENT = 96
KEY_ESCAPE = 256
KEY_ENTER = 257
KEY_TAB = 258
KEY_BACKSPACE = 259
KEY_INSERT = 260
KEY_DELETE = 261
KEY_RIGHT = 262
KEY_LEFT = 263
KEY_DOWN = 264
KEY_UP = 265
KEY_PAGE_UP = 266
KEY_PAGE_DOWN = 267
KEY_HOME = 268
KEY_END = 269
KEY_KP_ENTER = 335

for _i in range(10):
    globals()["KEY_%d" % _i] = 48 + _i
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    globals()["KEY_" + _c] = ord(_c)
for _i in range(1, 13):
    globals()["KEY_F%d" % _i] = 289 + _i

# GDK keyval names (of the unshifted key) -> code
_GDK_NAMES = {
    "space": KEY_SPACE, "apostrophe": KEY_APOSTROPHE, "comma": KEY_COMMA,
    "minus": KEY_MINUS, "period": KEY_PERIOD, "slash": KEY_SLASH,
    "semicolon": KEY_SEMICOLON, "equal": KEY_EQUAL,
    "bracketleft": KEY_LEFT_BRACKET, "backslash": KEY_BACKSLASH,
    "bracketright": KEY_RIGHT_BRACKET, "grave": KEY_GRAVE_ACCENT,
    "Escape": KEY_ESCAPE, "Return": KEY_ENTER, "Tab": KEY_TAB,
    "BackSpace": KEY_BACKSPACE, "Insert": KEY_INSERT, "Delete": KEY_DELETE,
    "Right": KEY_RIGHT, "Left": KEY_LEFT, "Down": KEY_DOWN, "Up": KEY_UP,
    "Page_Up": KEY_PAGE_UP, "Page_Down": KEY_PAGE_DOWN, "Home": KEY_HOME,
    "End": KEY_END, "KP_Enter": KEY_KP_ENTER,
}


def from_gdk_name(name):
    """Code for a GDK keyval name ("a", "A", "bracketleft", "F5"...)."""
    if not name:
        return KEY_UNKNOWN
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    if name in _GDK_NAMES:
        return _GDK_NAMES[name]
    if name[0] == "F" and name[1:].isdigit() and 1 <= int(name[1:]) <= 12:
        return 289 + int(name[1:])
    return KEY_UNKNOWN


def from_gdk(display, keyval, keycode):
    """Code for a GTK key event: the key's UNSHIFTED meaning, so Shift+=
    is still KEY_EQUAL (GLFW semantics)."""
    from gi.repository import Gdk
    base = keyval
    try:
        ok, kv, _grp, _lvl, _cons = display.translate_key(
            keycode, Gdk.ModifierType(0), 0)
        if ok:
            base = kv
    except Exception:                                     # noqa: BLE001
        pass
    return from_gdk_name(Gdk.keyval_name(base))


def mods_from_gdk(state):
    """MOD_* bitmask from a Gdk.ModifierType."""
    from gi.repository import Gdk
    mods = 0
    if state & Gdk.ModifierType.SHIFT_MASK:
        mods |= MOD_SHIFT
    if state & Gdk.ModifierType.CONTROL_MASK:
        mods |= MOD_CONTROL
    if state & Gdk.ModifierType.ALT_MASK:
        mods |= MOD_ALT
    if state & Gdk.ModifierType.SUPER_MASK:
        mods |= MOD_SUPER
    return mods
