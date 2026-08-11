"""Drive a running Refract from outside it.

    python -m refract.ctl park          # hand the desktop back
    python -m refract.ctl resume
    python -m refract.ctl handoff       # toggle -- bind this to a hotkey
    python -m refract.ctl recenter | hud | quit | left | centre | right

Exists because the keyboard is not reliably ours: our window is fullscreen on
the glasses output, the wearer is typing into something on the laptop, and
GNOME swallows modifier combos before they reach us. A command written to the
control file always arrives, whatever has focus.

Bind the handoff to a system shortcut so it works from anywhere:
  Settings -> Keyboard -> Custom Shortcuts, command:
    /home/kendel/Vibe/Refract/.venv/bin/python -m refract.ctl handoff
"""

import os
import sys

from refract.core.render import CTL_PATH, already_running

COMMANDS = ["park", "resume", "handoff", "recenter", "hud", "save", "quit",
            "follow", "curve", "nearer", "farther", "smaller", "bigger",
            "fill", "left", "centre", "right"]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        print("  commands: %s" % " ".join(COMMANDS))
        return 0
    cmd = argv[0].strip().lower()
    if cmd not in COMMANDS:
        print("unknown command: %s\n  try: %s" % (cmd, " ".join(COMMANDS)))
        return 1
    if not already_running():
        print("Refract does not appear to be running.")
        return 1
    try:
        with open(CTL_PATH, "w") as f:
            f.write(cmd)
    except OSError as e:
        print("could not write %s: %s" % (CTL_PATH, e))
        return 1
    print("sent: %s" % cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
