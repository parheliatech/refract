#!/usr/bin/env python3
"""Run Refract's test suites and print one summary.

    .venv/bin/python tests/run.py              # quick set (~3 s)
    .venv/bin/python tests/run.py --all        # + desk, which makes REAL
                                               #   monitors appear briefly
    .venv/bin/python tests/run.py --only hud
    .venv/bin/python tests/run.py --list

Each suite runs as a SUBPROCESS, not an import. That is not tidiness: only
one OpenGL context can be current per process, so two window-driving suites
in one interpreter fail with "glXGetCurrentContext: cannot detect OpenGL
context" (found the hard way in phase 3). Subprocesses also mean a hang or a
segfault in one suite cannot take the rest of the run with it.

Nothing here touches the glasses or writes the real config. On-glasses
output is verified separately, by capture -- see DEVELOPMENT_PLAN.md.
"""

import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


class Suite:
    def __init__(self, name, script, what, display=False, disruptive=False,
                 timeout=120):
        self.name = name
        self.script = script
        self.what = what
        self.display = display
        self.disruptive = disruptive
        self.timeout = timeout


SUITES = [
    Suite("selftest", "selftest.py",
          "head solve, pointer maths, desk arrangement", timeout=60),
    Suite("home", "smoke_home.py",
          "launcher: keys, pointer, launch/return", display=True),
    Suite("hud", "smoke_hud.py",
          "hud: combos, quick-switch, live settings", display=True),
    Suite("desk", "smoke_desk.py",
          "desk: real monitors, mirror, layout", display=True,
          disruptive=True, timeout=300),
]

COUNT_RE = re.compile(r"(\d+) checks passed")


def have_display():
    return bool(os.environ.get("WAYLAND_DISPLAY")
                or os.environ.get("DISPLAY"))


def run_one(suite, outdir, verbose):
    cmd = [sys.executable, os.path.join(HERE, suite.script)]
    if suite.script != "selftest.py":
        cmd += ["--outdir", os.path.join(outdir, suite.name)]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=REPO, timeout=suite.timeout,
                           capture_output=not verbose, text=True)
        out = "" if verbose else (p.stdout or "") + (p.stderr or "")
        rc = p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        rc, out = 124, out + "\n  TIMED OUT after %ds" % suite.timeout
    secs = time.time() - t0
    found = COUNT_RE.findall(out)
    checks = int(found[-1]) if found else 0
    return rc, checks, secs, out


def main():
    ap = argparse.ArgumentParser(description="run Refract's test suites")
    ap.add_argument("-a", "--all", action="store_true",
                    help="include disruptive suites (desk creates and "
                         "destroys real monitors while it runs)")
    ap.add_argument("--only", help="comma-separated suite names")
    ap.add_argument("--list", action="store_true", help="list suites and exit")
    ap.add_argument("--outdir", default="/tmp/refract-tests",
                    help="where per-step PNGs land (default %(default)s)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="stream each suite's output instead of summarising")
    a = ap.parse_args()

    if a.list:
        for s in SUITES:
            tags = []
            if s.display:
                tags.append("needs a display")
            if s.disruptive:
                tags.append("DISRUPTIVE: makes real monitors")
            print("  %-9s %-42s %s" % (s.name, s.what,
                                       "; ".join(tags) or "safe anywhere"))
        return 0

    wanted = SUITES
    if a.only:
        names = [n.strip() for n in a.only.split(",")]
        wanted = [s for s in SUITES if s.name in names]
        unknown = [n for n in names if not any(s.name == n for s in SUITES)]
        if unknown:
            sys.exit("no such suite: %s (try --list)" % ", ".join(unknown))
    elif not a.all:
        wanted = [s for s in SUITES if not s.disruptive]

    display = have_display()
    os.makedirs(a.outdir, exist_ok=True)

    rows, total, failed, skipped = [], 0, 0, 0
    for s in wanted:
        if s.display and not display:
            rows.append((s.name, "skip", 0, 0.0, "no display"))
            skipped += 1
            continue
        if not a.verbose:
            print("  running %-9s %s" % (s.name, s.what), flush=True)
        rc, checks, secs, out = run_one(s, a.outdir, a.verbose)
        total += checks
        if rc != 0:
            failed += 1
            rows.append((s.name, "FAIL", checks, secs, "rc=%d" % rc))
            if not a.verbose:
                tail = [ln for ln in out.strip().splitlines()][-12:]
                print("\n  --- %s output (tail) ---" % s.name)
                for ln in tail:
                    print("  %s" % ln)
                print("  --- end %s ---\n" % s.name)
        else:
            rows.append((s.name, "ok", checks, secs, ""))

    print("\n  %-9s %-6s %7s %8s" % ("suite", "result", "checks", "time"))
    for name, status, checks, secs, note in rows:
        print("  %-9s %-6s %7s %7.1fs %s"
              % (name, status, checks or "-", secs, note))
    ran = len(rows) - failed - skipped
    print("\n  %d checks passed across %d suite%s%s"
          % (total, ran, "" if ran == 1 else "s",
             ", %d FAILED" % failed if failed else ""))
    if skipped:
        print("  %d skipped (no display; run these on the desktop session)"
              % skipped)
    if not a.all and not a.only:
        print("  desk not run -- add --all (it briefly creates real monitors)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
