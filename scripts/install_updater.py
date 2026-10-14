#!/usr/bin/env python3
"""One-time privileged updater setup for an existing VERA device."""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=os.environ.get("SUDO_USER"))
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--web-url", default="http://localhost:8080")
    args = parser.parse_args()
    if os.geteuid() != 0 or not args.user or args.user == "root":
        parser.error("Run sudo python3 scripts/install_updater.py --user YOUR_ACCOUNT")
    if args.web_url not in ("http://localhost:8080", "http://127.0.0.1:8080"):
        parser.error("Health checks must use the local VERA web endpoint")
    account = pwd.getpwnam(args.user)
    checkout = args.checkout.resolve()
    if not (checkout / ".git").is_dir() or checkout.stat().st_uid != account.pw_uid:
        parser.error("Checkout must be an existing Git repository owned by the VERA account")
    import sys
    sys.path.insert(0, str(checkout))
    from src.updater.contract import load_manifest
    from src.updater.host import Host

    load_manifest(checkout)
    state_path = Path("/var/lib/vera-updater/state.json")
    if state_path.exists() and json.loads(state_path.read_text()).get("transaction"):
        parser.error("Resolve the active/recovery transaction before reinstalling the updater")
    runner_path = Path("/opt/vera-updater")
    if runner_path.is_symlink() or (runner_path.exists() and runner_path.stat().st_uid != 0):
        parser.error("Existing updater runner must be a root-owned directory, not a symlink")
    for name in ("vera-updater.timer", "vera-updater.service"):
        unit_path = Path("/etc/systemd/system") / name
        if unit_path.exists():
            subprocess.run(["systemctl", "stop", name], check=True)
    # Check again after stopping scheduled work, before replacing runner files.
    if state_path.exists() and json.loads(state_path.read_text()).get("transaction"):
        parser.error("Interrupted transaction detected; restart updater service to recover before reinstalling")
    # Install only prerequisites for the independent controller here. Application
    # package installation remains a release transaction.
    subprocess.run(["apt-get", "update"], check=True)
    subprocess.run(["apt-get", "install", "-y", "git", "python3-yaml", "python3-zmq"], check=True)
    policy = {
        "checkout": str(checkout), "user": account.pw_name, "uid": account.pw_uid,
        "gid": account.pw_gid, "group": grp.getgrgid(account.pw_gid).gr_name,
        "home": account.pw_dir, "web_url": args.web_url, "runner": "/opt/vera-updater",
    }
    host = Host(policy)
    units = host.units(load_manifest(checkout)["units"])
    core = next((u for u in units if u["name"] == "desktop-assistant-core.service"), None)
    if not core:
        parser.error("Install and configure VERA core services before installing the updater")
    policy["default_scope"] = core["scope"]
    config = Path("/etc/desktop-assistant")
    config.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = config / "updater-policy.json"
    destination.write_text(json.dumps(policy, indent=2) + "\n")
    destination.chmod(0o600)
    runner = Path(policy["runner"])
    runner.mkdir(mode=0o755, parents=True, exist_ok=True)
    shutil.copytree(checkout / "src/updater", runner / "src/updater",
                    ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
    (runner / "src/__init__.py").touch()
    shutil.copy2(checkout / "scripts/updater-git-askpass", runner / "git-askpass")
    (runner / "git-askpass").chmod(0o700)
    for path in runner.rglob("*"):
        if path.is_symlink():
            raise SystemExit("Refusing linked updater installation files")
        os.chown(path, 0, 0)
        path.chmod(0o755 if path.is_dir() else 0o700 if path.name == "git-askpass" else 0o644)
    for name in ("vera-updater.service", "vera-updater-check.service", "vera-updater.timer"):
        shutil.copy2(checkout / "services/systemd" / name, Path("/etc/systemd/system") / name)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "vera-updater.service", "vera-updater.timer"], check=True)
    print("Updater installed. Use vera update status and the authenticated System dashboard.")
    print("Automatic installation defaults to 02:00-04:00 local time.")
    print("If this repository is private, provision a root-only read-only GitHub token as documented.")


if __name__ == "__main__":
    main()
