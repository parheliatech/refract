"""Single config store: ~/.config/refract/config.json.

One file, one schema. Top-level sections: "global" plus one section per
sub-experience ("desk", "three60", "play", "tak"). On first run, an existing
~/.config/xrdesk.json is imported into the "desk" section (one-way; the old
file is left in place untouched).
"""

import json
import os

CONFIG_DIR = os.path.expanduser("~/.config/refract")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
XRDESK_LEGACY_PATH = os.path.expanduser("~/.config/xrdesk.json")

SECTIONS = ("global", "desk", "three60", "play", "tak")


def _empty():
    return {name: {} for name in SECTIONS}


def load():
    """Load config, migrating from xrdesk.json on very first run."""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        for name in SECTIONS:
            cfg.setdefault(name, {})
        return cfg

    cfg = _empty()
    if os.path.exists(XRDESK_LEGACY_PATH):
        try:
            with open(XRDESK_LEGACY_PATH) as f:
                cfg["desk"] = json.load(f)
            cfg["global"]["migrated_from"] = "xrdesk.json"
        except (OSError, ValueError):
            pass    # unreadable legacy config is not worth failing boot over
    return cfg


def save(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)
