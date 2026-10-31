"""Build clean, reproducible source and Home Assistant installation ZIPs."""

from hashlib import sha256
from pathlib import Path
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
EXCLUDED = {".git", ".venv", "__pycache__", "dist", "build", "data", ".pytest_cache"}
VERSION = json.loads((ROOT / "custom_components/sleepnumber_rf/manifest.json").read_text())["version"]


def build():
    DIST.mkdir(exist_ok=True)
    outputs = []
    for name, source_only in ((f"sleepnumber-rf-bridge-{VERSION}.zip", False), (f"sleepnumber-rf-home-assistant-{VERSION}.zip", True)):
        path = DIST / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in sorted(ROOT.rglob("*")):
                relative = item.relative_to(ROOT)
                if not item.is_file() or any(part in EXCLUDED or part.endswith(".egg-info") for part in relative.parts):
                    continue
                if item.suffix in (".pyc", ".jsonl", ".log", ".key", ".pem") or item.name == "config.json" or item.name.startswith(".env"):
                    continue
                if source_only and relative.parts[0] != "custom_components":
                    continue
                info = zipfile.ZipInfo(relative.as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, item.read_bytes())
        outputs.append(path)
    assets = outputs + sorted(DIST.glob(f"sleepnumber_rf_bridge-{VERSION}-*.whl"))
    (DIST / "SHA256SUMS").write_text("".join(f"{sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in assets), encoding="utf-8")
    for path in outputs:
        print(path)


if __name__ == "__main__":
    build()
