"""Copy this reusable component into an existing HA config directory."""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("config_directory", type=Path)
args = parser.parse_args()
root = args.config_directory.resolve()
if not (root / "configuration.yaml").is_file():
    parser.error("Directory must contain Home Assistant's configuration.yaml")
source = Path(__file__).resolve().parents[1] / "custom_components" / "sleepnumber_rf"
destination = root / "custom_components" / "sleepnumber_rf"
if destination.exists():
    backup = root / "sleepnumber-rf-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup.parent.mkdir(parents=True, exist_ok=True)
    destination.rename(backup)
else:
    backup = None
try:
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
except Exception:
    if backup is not None and not destination.exists():
        backup.rename(destination)
    raise
print(f"Installed {destination}. Restart Home Assistant, then add Sleep Number RF in Devices & services.")
