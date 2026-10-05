"""The control channel: drive a running Refract from outside it.

A Unix DATAGRAM socket in the private runtime dir: every command is
processed, and the sender gets a reply, "ok <cmd>" or "unknown <cmd>". The
client side is refract.ctl.
"""

import os
import socket

from refract.core.config import runtime_dir

RUNTIME_DIR = runtime_dir()
SOCK_PATH = os.path.join(RUNTIME_DIR, "refract.sock")
# the control FILE older versions used; removed at startup
LEGACY_CTL_PATH = os.path.join(RUNTIME_DIR, "refract.ctl")


class ControlSocket:
    def __init__(self, path=None, legacy_path=None):
        """Bind the socket. `.ok` is False if that failed (refract.ctl then
        says Refract is not reachable, which is true).

        A socket left by a crashed run is replaced; datagrams queue in the
        receiving socket, so nothing sent to a dead instance reaches this
        one.
        """
        self.path = path or SOCK_PATH
        self.sock = None
        for stale in (self.path, legacy_path or LEGACY_CTL_PATH):
            try:
                os.unlink(stale)
            except OSError:
                pass
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            old = os.umask(0o177)          # 0600: nobody else drives us
            try:
                s.bind(self.path)
            finally:
                os.umask(old)
            s.setblocking(False)
            self.sock = s
        except OSError as e:
            print("  ctl: control socket unavailable: %s" % e, flush=True)

    @property
    def ok(self):
        return self.sock is not None

    def poll(self, handle, limit=16):
        """Run every waiting command through handle(cmd) -> bool and answer
        each sender. Bounded, so a flood can never stall a frame."""
        if self.sock is None:
            return
        for _ in range(limit):
            try:
                data, addr = self.sock.recvfrom(256)
            except OSError:              # BlockingIOError: nothing waiting
                return
            cmd = data.decode(errors="replace").strip().lower()
            ok = handle(cmd)
            if addr:
                try:
                    self.sock.sendto(("%s %s" % ("ok" if ok else "unknown",
                                                 cmd)).encode(), addr)
                except OSError:
                    pass                 # the sender stopped waiting

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
            try:
                os.unlink(self.path)
            except OSError:
                pass
