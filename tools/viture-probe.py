#!/usr/bin/env python3
"""
viture-probe -- identify VITURE XR glasses and dump everything Phase 1 needs.

Zero dependencies, no root required for the scan itself. Reads sysfs directly.

    python3 viture-probe.py

PID table and vendor ID recovered from SpaceWalker.apk v1.7.2.0
(com.eden.sensor.glass.GlassManager).
"""

import os
import sys
import glob

VITURE_VID = 0x35CA
CAMERA_VID = 0x0C45  # Sonix camera module, skipped by GlassManager
MIC_PID = 0x1102     # 0x35CA:0x1102 is the microphone interface, not the glasses

# PIDs referenced by GlassManager's device matcher. Generation mapping is
# inferred from DeviceType {GEN1=0, GEN2=1, CARINA=2} and is provisional --
# only nativeGetDeviceType() on real hardware is authoritative.
KNOWN_PIDS = {
    0x1000: "glasses (early/dev)",
    0x1011: "glasses", 0x1013: "glasses", 0x1015: "glasses",
    0x1017: "glasses", 0x1019: "glasses", 0x101B: "glasses",
    0x101D: "glasses",
    0x1101: "glasses", 0x1104: "glasses",
    0x1121: "glasses", 0x1131: "glasses", 0x1141: "glasses",
    0x1151: "glasses",
    0x1201: "glasses", 0x1211: "glasses",
    0x1301: "glasses",
    0x4000: "glasses (alt/DFU?)",
    MIC_PID: "microphone interface (NOT the control device)",
}

USB_CLASS = {
    0x01: "audio", 0x02: "comm", 0x03: "HID", 0x05: "physical",
    0x06: "image", 0x07: "printer", 0x08: "mass-storage", 0x09: "hub",
    0x0A: "cdc-data", 0x0B: "smartcard", 0x0E: "video", 0x10: "audio/video",
    0xEF: "misc", 0xFE: "app-specific", 0xFF: "vendor-specific",
}


def rd(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def rd_hex(path):
    v = rd(path)
    try:
        return int(v, 16)
    except ValueError:
        return None


def find_usb_devices():
    """Yield sysfs paths of all USB devices (not interfaces)."""
    for p in sorted(glob.glob("/sys/bus/usb/devices/*")):
        if os.path.exists(os.path.join(p, "idVendor")):
            yield p


def interfaces_of(devpath):
    base = os.path.basename(devpath)
    out = []
    for p in sorted(glob.glob(os.path.join(devpath, base + ":*"))):
        out.append({
            "path": p,
            "num": rd(os.path.join(p, "bInterfaceNumber")),
            "cls": rd_hex(os.path.join(p, "bInterfaceClass")),
            "sub": rd_hex(os.path.join(p, "bInterfaceSubClass")),
            "proto": rd_hex(os.path.join(p, "bInterfaceProtocol")),
            "driver": os.path.basename(
                os.path.realpath(os.path.join(p, "driver"))
            ) if os.path.exists(os.path.join(p, "driver")) else None,
            "eps": sorted(
                os.path.basename(e)
                for e in glob.glob(os.path.join(p, "ep_*"))
            ),
        })
    return out


def hidraw_for(iface_path):
    """hidraw nodes under an interface.

    Deliberately NOT a recursive glob: sysfs is full of symlink cycles and
    ** will wander into them. hidraw lives at either
    <iface>/hidraw/hidrawN or <iface>/<hid-bus-id>/hidraw/hidrawN.
    """
    out = []
    for pat in ("hidraw/hidraw*", "*/hidraw/hidraw*"):
        out += [os.path.basename(h)
                for h in glob.glob(os.path.join(iface_path, pat))]
    return sorted(set(out))


def video_for(iface_path):
    return [os.path.basename(v)
            for v in glob.glob(os.path.join(iface_path, "video4linux", "video*"))]


def devnode(devpath):
    busnum = rd(os.path.join(devpath, "busnum"))
    devnum = rd(os.path.join(devpath, "devnum"))
    if not busnum or not devnum:
        return None
    return "/dev/bus/usb/%03d/%03d" % (int(busnum), int(devnum))


def describe(devpath, vid, pid):
    print("=" * 68)
    print("  %04x:%04x  %s" % (vid, pid, rd(os.path.join(devpath, "product"), "?")))
    print("=" * 68)
    print("  sysfs        : %s" % devpath)
    print("  manufacturer : %s" % rd(os.path.join(devpath, "manufacturer"), "?"))
    print("  serial       : %s" % rd(os.path.join(devpath, "serial"), "?"))
    print("  bcdDevice    : %s" % rd(os.path.join(devpath, "bcdDevice"), "?"))
    print("  USB version  : %s   speed: %s Mbps"
          % (rd(os.path.join(devpath, "version"), "?").strip(),
             rd(os.path.join(devpath, "speed"), "?")))

    known = KNOWN_PIDS.get(pid)
    if known:
        print("  PID match    : KNOWN -- %s" % known)
    else:
        print("  PID match    : UNKNOWN to the extracted table "
              "(new model or new firmware)")

    node = devnode(devpath)
    if node:
        ok = os.access(node, os.R_OK | os.W_OK)
        print("  usbfs node   : %s  [%s]"
              % (node, "read/write OK" if ok else "NO ACCESS -- udev rule needed"))

    ifaces = interfaces_of(devpath)
    print("\n  Interfaces (%d):" % len(ifaces))
    for i in ifaces:
        cls_name = USB_CLASS.get(i["cls"], "?")
        print("    if%-3s class=0x%02x (%-16s) sub=0x%02x proto=0x%02x  driver=%s"
              % (i["num"], i["cls"] or 0, cls_name, i["sub"] or 0,
                 i["proto"] or 0, i["driver"] or "-none-"))
        if i["eps"]:
            print("            endpoints: %s" % ", ".join(i["eps"]))
        for h in hidraw_for(i["path"]):
            hp = "/dev/" + h
            acc = "rw OK" if os.access(hp, os.R_OK | os.W_OK) else "NO ACCESS"
            print("            -> %s  [%s]" % (hp, acc))
        for v in video_for(i["path"]):
            print("            -> /dev/%s" % v)
    print()


def main():
    print("\nVITURE probe -- scanning USB for vendor 0x%04X\n" % VITURE_VID)

    found = []
    cameras = []
    for devpath in find_usb_devices():
        vid = rd_hex(os.path.join(devpath, "idVendor"))
        pid = rd_hex(os.path.join(devpath, "idProduct"))
        if vid == VITURE_VID:
            found.append((devpath, vid, pid))
        elif vid == CAMERA_VID:
            cameras.append((devpath, vid, pid))

    if not found:
        print("  No VITURE device found (vendor 0x35CA).")
        print("  Plug the glasses in via USB-C and re-run.\n")
        print("  Note: DisplayPort alt-mode video can be present while the USB")
        print("  control interface is not enumerated -- if the display works but")
        print("  nothing appears here, the cable or port may be video-only.\n")
        return 1

    for devpath, vid, pid in found:
        describe(devpath, vid, pid)

    if cameras:
        print("  Also present (Sonix camera VID 0x0C45 -- SpaceWalker ignores these):")
        for devpath, vid, pid in cameras:
            print("    %04x:%04x  %s"
                  % (vid, pid, rd(os.path.join(devpath, "product"), "?")))
        print()

    # DRM outputs, to see whether the glasses are also attached as a display
    print("  DRM outputs:")
    for s in sorted(glob.glob("/sys/class/drm/card*/status")):
        name = os.path.basename(os.path.dirname(s))
        print("    %-28s %s" % (name, rd(s)))
    print()

    non_mic = [p for _, _, p in found if p != MIC_PID]
    if non_mic:
        print("  Suggested udev rule -- write to")
        print("  /etc/udev/rules.d/99-viture.rules :\n")
        print('    SUBSYSTEM=="usb", ATTR{idVendor}=="35ca", MODE="0666", '
              'TAG+="uaccess"')
        print('    KERNEL=="hidraw*", ATTRS{idVendor}=="35ca", MODE="0666", '
              'TAG+="uaccess"')
        print("\n  Then:  sudo udevadm control --reload-rules && "
              "sudo udevadm trigger\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
