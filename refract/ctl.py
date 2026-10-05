"""Drive a running Refract from outside it.

    python -m refract.ctl park          # hand the desktop back
    python -m refract.ctl resume
    python -m refract.ctl handoff       # toggle -- bind this to a hotkey
    python -m refract.ctl recenter | hud | quit | left | centre | right

Exists because the keyboard is not reliably ours: our window is fullscreen on
the glasses output, the wearer is typing into something on the laptop, and
GNOME swallows modifier combos before they reach us. A command sent to the
control socket always arrives, whatever has focus, and Refract answers it --
so this prints whether the command was understood, and exits non-zero if
it was not or Refract is not running.

Bind the handoff to a system shortcut so it works from anywhere:
  Settings -> Keyboard -> Custom Shortcuts, command:
    /home/kendel/Vibe/Refract/.venv/bin/python -m refract.ctl handoff
"""

import socket
import sys

from refract.core.control import SOCK_PATH

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
    return send(cmd)


def send(cmd, timeout=2.0, path=None):
    """Send one command, print Refract's answer. Returns an exit code."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.bind("")                  # autobind: an address to be answered at
        s.settimeout(timeout)
        try:
            s.sendto(cmd.encode(), path or SOCK_PATH)
        except (FileNotFoundError, ConnectionRefusedError):
            print("Refract does not appear to be running.")
            return 1
        try:
            reply = s.recv(256).decode(errors="replace")
        except socket.timeout:
            # it was delivered; the main loop is just busy (a park or resume
            # can take a few seconds) -- not a failure
            print("sent: %s (no answer within %.0fs)" % (cmd, timeout))
            return 0
        print(reply)
        return 0 if reply.startswith("ok ") else 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
