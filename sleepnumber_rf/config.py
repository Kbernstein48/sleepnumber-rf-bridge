"""Validated configuration, stored separately from application updates."""

import json
import os
from pathlib import Path
import re
import tempfile

DEFAULTS = {
    "name": "Sleep Number base", "device": "/dev/ttyUSB1", "channel": 15,
    "enabled": True, "light_outlets": [3], "presets": {"flat": 0},
    "lock_file": "/tmp/nightstand-husbzb-capture.lock",
}


def validate(data):
    if not isinstance(data, dict):
        raise ValueError("Configuration must be an object")
    unknown = set(data) - set(DEFAULTS) - {"target"}
    if unknown:
        raise ValueError("Unknown configuration fields: " + ", ".join(sorted(unknown)))
    result = {**DEFAULTS, **data}
    target = result.get("target", "")
    if not isinstance(target, str) or not re.fullmatch(r"[0-9a-fA-F]{4}", target) or int(target, 16) in (0, 65535):
        raise ValueError("target must be the base's four-digit hexadecimal RF address")
    result["target"] = target.lower()
    for key in ("name", "device", "lock_file"):
        if not isinstance(result[key], str) or not result[key].strip() or len(result[key]) > 250:
            raise ValueError(f"Invalid {key}")
    if not result["device"].startswith("/dev/") or not result["lock_file"].startswith("/"):
        raise ValueError("Use an absolute serial device and lock path")
    if type(result["channel"]) is not int or not 11 <= result["channel"] <= 26:
        raise ValueError("channel must be an integer from 11 to 26")
    if type(result["enabled"]) is not bool:
        raise ValueError("enabled must be true or false")
    outlets = result["light_outlets"]
    if not isinstance(outlets, list) or not outlets or any(type(x) is not int or x not in (3, 4) for x in outlets) or len(set(outlets)) != len(outlets):
        raise ValueError("light_outlets must contain 3 and/or 4")
    presets = result["presets"]
    if not isinstance(presets, dict) or len(presets) > 12:
        raise ValueError("presets must be an object of at most twelve positions")
    for name, position in presets.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", name) or type(position) is not int or not 0 <= position <= 100:
            raise ValueError("Preset names use lowercase letters, numbers and underscores; positions are 0–100")
    result["presets"] = {**presets, "flat": 0}
    return result


def load(path):
    return validate(json.loads(Path(path).read_text(encoding="utf-8")))


def save(path, data):
    data = validate(data)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".config-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(data, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return data
