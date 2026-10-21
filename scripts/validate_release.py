#!/usr/bin/env python3
"""Validate updater-aware release metadata without publishing anything."""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.updater.contract import load_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    manifest = load_manifest(ROOT)
    base_tag = "v" + manifest["version"]
    if args.tag != base_tag and not re.fullmatch(re.escape(base_tag) + r"-rc\.[1-9][0-9]*", args.tag):
        parser.error("Tag must match VERSION and deployment manifest, optionally with -rc.N")
    changed = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"],
                             check=True, text=True, capture_output=True).stdout.splitlines()
    if any(line[3:] not in ("config/assistant.yaml", "config/thermal.yaml", "config/stereo_cal.npz")
           for line in changed):
        parser.error("Commit release source/metadata before validating")
    for name in manifest["units"]:
        if not (ROOT / "services/systemd" / name).is_file():
            parser.error(f"Missing service template {name}")
    print(f"{args.tag}: deployment metadata validated; run tests and review hardware compatibility before publishing.")


if __name__ == "__main__":
    main()
