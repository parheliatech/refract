"""Settings schema: one description of a control, rendered anywhere.

A scene returns a list of Setting from `settings_schema()` and gets a HUD
panel for free -- no per-scene UI code. That is the machinery that keeps the
root screen shallow: adding a knob to a sub-experience must be easier than
sneaking one onto the home screen (DEVELOPMENT_PLAN.md invariant 3).

Values live in the single config store, in the section the Setting names.
Changes apply LIVE -- `on_change` fires immediately and there is no OK
button, because a wearer adjusting screen distance needs to see the result,
not commit to it blind.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

BOOL = "bool"
INT = "int"
FLOAT = "float"
ENUM = "enum"
ACTION = "action"
INFO = "info"


@dataclass
class Setting:
    label: str
    kind: str = INFO
    key: str = ""
    section: str = "global"
    default: Any = None
    lo: float = 0.0
    hi: float = 1.0
    step: float = 0.1
    unit: str = ""
    # enum options as ((value, label), ...)
    options: Sequence = field(default_factory=tuple)
    on_change: Optional[Callable] = None      # fn(app, value)
    run: Optional[Callable] = None            # fn(app), for ACTION
    text: str = ""                            # for INFO
    available: bool = True                    # greyed and inert when False

    @property
    def adjustable(self):
        return self.available and self.kind in (BOOL, INT, FLOAT, ENUM)


def get(app, s):
    if s.kind in (INFO, ACTION):
        return None
    return app.config.setdefault(s.section, {}).get(s.key, s.default)


def set_value(app, s, value):
    app.config.setdefault(s.section, {})[s.key] = value
    app.config_dirty = True
    if s.on_change:
        s.on_change(app, value)
    return value


def adjust(app, s, direction):
    """Move a value by one step. Returns True if anything changed."""
    if not s.adjustable:
        return False
    cur = get(app, s)
    if s.kind == BOOL:
        set_value(app, s, not bool(cur))
        return True
    if s.kind == ENUM:
        values = [o[0] for o in s.options]
        if not values:
            return False
        try:
            i = values.index(cur)
        except ValueError:
            i = 0
        # clamp rather than wrap: wrapping an IMU rate from 240 back to 60 on
        # one extra keypress is a surprise you feel in the tracking
        i = max(0, min(len(values) - 1, i + direction))
        set_value(app, s, values[i])
        return True
    cur = float(cur if cur is not None else s.default or 0.0)
    nxt = cur + direction * s.step
    nxt = max(s.lo, min(s.hi, nxt))
    if s.kind == INT:
        nxt = int(round(nxt))
    else:
        # keep float steps from accumulating 0.30000000000000004
        nxt = round(nxt, 4)
    if nxt == cur:
        return False
    set_value(app, s, nxt)
    return True


def activate(app, s):
    """Enter on a row: run actions, toggle bools, cycle enums."""
    if not s.available:
        return False
    if s.kind == ACTION and s.run:
        s.run(app)
        return True
    if s.kind == BOOL:
        return adjust(app, s, 1)
    if s.kind == ENUM:
        values = [o[0] for o in s.options]
        if not values:
            return False
        cur = get(app, s)
        i = values.index(cur) + 1 if cur in values else 0
        set_value(app, s, values[i % len(values)])
        return True
    return False


def format_value(app, s):
    if s.kind == INFO:
        return s.text
    if s.kind == ACTION:
        return ">"
    v = get(app, s)
    if s.kind == BOOL:
        return "on" if v else "off"
    if s.kind == ENUM:
        for value, label in s.options:
            if value == v:
                return label
        return str(v)
    if s.kind == INT:
        return "%d%s" % (int(v or 0), s.unit)
    return "%.2f%s" % (float(v or 0.0), s.unit)


def apply_all(app, schema):
    """Push every stored value through its on_change -- used at scene entry so
    a restored config actually takes effect instead of only being displayed."""
    for s in schema:
        if s.kind in (INFO, ACTION) or not s.on_change:
            continue
        s.on_change(app, get(app, s))
